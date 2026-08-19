"""The core: message parsing, links, time and write limits.

Everything here is either a pure function or a method that a fake client is
enough for. No test connects to Telegram and none of them touches the session
file.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import (
    FakeClient,
    FakeConfigClient,
    FakeDialog,
    FakeMessage,
    json_tree,
    make_entity,
)
from telethon.tl import types

from tgagent import config, core
from tgagent.core import GuardError, RateGuard

# ---------------------------------------------------------------- links


def test_links_are_cut_by_utf16_offsets():
    """Telegram counts offsets in UTF-16, Python in code points.

    Without the conversion to surrogates an emoji above the BMP shifts the
    slicing and the link arrives cut off. The offsets below are exactly the ones
    Telegram would send: "Here " is 5 units, "🎉" is another two.
    """
    msg = FakeMessage(
        message="Here 🎉 https://example.com/a and a tail",
        entities=[make_entity("MessageEntityUrl", offset=8, length=21)],
    )
    assert core._links(msg, bare=True) == ["https://example.com/a"]


def test_a_link_behind_a_label_is_returned_together_with_the_label():
    msg = FakeMessage(
        message="Take 🎉 the contract, it is here",
        entities=[
            make_entity("MessageEntityTextUrl", offset=12, length=8,
                        url="https://example.com/contract"),
        ],
    )
    assert core._links(msg) == ["https://example.com/contract (contract)"]


def test_repeated_links_collapse_keeping_the_order():
    msg = FakeMessage(
        message="https://a.tld https://b.tld https://a.tld",
        entities=[
            make_entity("MessageEntityUrl", 0, 13),
            make_entity("MessageEntityUrl", 14, 13),
            make_entity("MessageEntityUrl", 28, 13),
        ],
    )
    assert core._links(msg, bare=True) == ["https://a.tld", "https://b.tld"]


def test_a_url_in_plain_sight_is_not_listed_a_second_time():
    """The reader gets the full text, and the url is written in it.

    Listing it again is the same characters twice. On a channel that posts links
    that duplication was a quarter of the answer, and every token of it is paid
    for by whoever asked what is new.
    """
    msg = FakeMessage(
        message="read https://example.com/a",
        entities=[make_entity("MessageEntityUrl", 5, 21)],
    )
    assert core._links(msg) is None
    assert core._links(msg, bare=True) == ["https://example.com/a"]


def test_a_hidden_link_is_listed_whatever_the_mode():
    """This one the text does not show, so it is the only new thing in the message."""
    msg = FakeMessage(
        message="the contract",
        entities=[make_entity("MessageEntityTextUrl", 4, 8,
                              url="https://example.com/contract")],
    )
    assert core._links(msg) == ["https://example.com/contract (contract)"]
    assert core._links(msg, bare=True) == ["https://example.com/contract (contract)"]


def test_an_invisible_label_is_not_carried():
    """Channels anchor a preview to zero-width characters; those read as `()`.

    Escaped into JSON a zero-width joiner costs six characters and shows nothing,
    which is the worst ratio in the whole answer.
    """
    msg = FakeMessage(
        message="\u200b\u200b text",
        entities=[make_entity("MessageEntityTextUrl", 0, 2,
                              url="https://example.com/photo")],
    )
    assert core._links(msg) == ["https://example.com/photo"]


def test_the_preview_does_not_repeat_a_url_the_reader_already_has():
    """The card is made from a link in the message — its url is rarely news.

    What the card does add is what was read off the page: the title, the site,
    the description. Preview urls are the long machine-generated kind, so this is
    the cheapest field to drop and the most expensive to keep.
    """
    page = SimpleNamespace(
        url="https://example.com/a", site_name="Example",
        title="A", description="about a", type="article",
    )
    msg = FakeMessage(message="read https://example.com/a", web_preview=page)
    assert core._web_preview(msg, []) == {
        "site": "Example", "title": "A", "description": "about a", "type": "article",
    }
    # Nothing passed means nothing is known to be a duplicate — the caller is
    # showing a cut-down text, and then the card is all there is.
    assert core._web_preview(msg)["url"] == "https://example.com/a"


def test_the_preview_keeps_a_url_that_is_nowhere_else():
    page = SimpleNamespace(url="https://example.com/hidden", title="H")
    msg = FakeMessage(message="see the file", web_preview=page)
    assert core._web_preview(msg, [])["url"] == "https://example.com/hidden"


def test_a_message_without_links():
    assert core._links(FakeMessage(message="just text")) is None
    assert core._links(FakeMessage(message="")) is None
    # other entities (bold, mention) do not count as links
    msg = FakeMessage(message="bold", entities=[make_entity("MessageEntityBold", 0, 4)])
    assert core._links(msg) is None


# ---------------------------------------------------------------- message link


def test_in_a_dm_there_is_no_message_link(service, user):
    """The person has an @username, but t.me/username/123 leads elsewhere."""
    assert service.message_link(FakeMessage(id=12), ent=user) is None


def test_in_a_channel_with_a_username_the_link_is_public(service):
    channel = types.Channel(id=7, title="News", photo=None, date=None,
                            broadcast=True, username="news")
    assert service.message_link(FakeMessage(id=12), ent=channel) == "https://t.me/news/12"


def test_in_a_supergroup_without_a_username_the_link_goes_through_c(service, supergroup):
    assert service.message_link(FakeMessage(id=12), ent=supergroup) == "https://t.me/c/1234567890/12"


def test_in_a_plain_group_there_is_no_link(service):
    chat = types.Chat(id=42, title="Old group", photo=None,
                      participants_count=3, date=None, version=1)
    assert service.message_link(FakeMessage(id=12), ent=chat) is None


def test_the_chat_is_taken_from_the_message_when_no_entity_is_passed(service, supergroup):
    msg = FakeMessage(id=5, chat=supergroup)
    assert service.message_link(msg) == "https://t.me/c/1234567890/5"


# ---------------------------------------------------------------- attachment kind


def test_a_round_note_is_told_apart_before_video():
    """video_note is video too, and the order of the checks here carries meaning."""
    marker = object()
    msg = FakeMessage(media=marker, video_note=marker, video=marker)
    assert core._media_kind(msg) == "round"


def test_a_gif_is_told_apart_before_video():
    marker = object()
    assert core._media_kind(FakeMessage(media=marker, gif=marker, video=marker)) == "gif"


def test_a_document_with_a_file_name():
    doc = types.Document(
        id=1, access_hash=0, file_reference=b"", date=None, mime_type="application/pdf",
        size=1, dc_id=1, attributes=[types.DocumentAttributeFilename("report.pdf")],
    )
    assert core._media_kind(FakeMessage(media=doc, document=doc)) == "document:report.pdf"


def test_without_an_attachment():
    assert core._media_kind(FakeMessage()) is None


# ---------------------------------------------------------------- message_dict


def test_parsing_an_incoming_message(service, user):
    msg = FakeMessage(
        id=7, message="hi", sender=user, mentioned=True,
        date=datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
    )
    # The name comes from the fixture: message_dict takes "from" out of the
    # sender, so the two must be one and the same string.
    row = service.message_dict(msg, chat_name=user.first_name)
    assert row == {
        "id": 7,
        # `Z`, not `+00:00`: the same instant, five characters shorter, and every
        # answer about a chat carries one of these per message.
        "date": "2026-08-17T09:30:00Z",
        "text": "hi",
        "from_id": 555,
        "from": user.first_name,
        "mentioned": True,
        "chat": user.first_name,
    }


def test_empty_fields_are_thrown_out_but_the_id_stays(service):
    """The id is always required: without it the message cannot be opened, nor replied to."""
    row = service.message_dict(FakeMessage(id=0, date=None))
    assert row == {"id": 0}


def test_an_own_message_is_signed_you(service, user):
    row = service.message_dict(FakeMessage(id=1, message="yep", out=True, sender=user))
    assert row["from"] == "you" and row["out"] is True


def test_an_attachment_without_text_is_described_by_a_word(service):
    marker = object()
    row = service.message_dict(FakeMessage(id=1, media=marker, photo=marker))
    assert row["text"] == "[photo]" and row["media"] == "photo"


def test_unlistened_is_only_for_incoming_messages(service):
    marker = object()
    incoming = FakeMessage(id=1, media=marker, voice=marker, media_unread=True)
    assert service.message_dict(incoming)["unlistened"] is True
    outgoing = FakeMessage(id=2, media=marker, voice=marker, media_unread=True, out=True)
    assert "unlistened" not in service.message_dict(outgoing)


def test_reactions_in_the_parsed_message(service):
    reaction = types.ReactionEmoji(emoticon="👍")
    results = [types.ReactionCount(reaction=reaction, count=3, chosen_order=0)]
    msg = FakeMessage(id=1, message="ok",
                      reactions=types.MessageReactions(results=results))
    assert service.message_dict(msg)["reactions"] == [
        {"emoji": "👍", "count": 3, "mine": True}
    ]


# ---------------------------------------------------------------- reactions


def test_an_emoji_reaction():
    assert core.reaction_of(types.ReactionEmoji(emoticon="🔥")) == "🔥"


def test_a_custom_reaction_is_a_document_id():
    assert core.reaction_of(types.ReactionCustomEmoji(document_id=12345)) == 12345


def test_an_empty_reaction():
    assert core.reaction_of(types.ReactionEmpty()) is None
    assert core.reaction_of(None) is None


# ---------------------------------------------------------------- time


def test_relative_time_forwards_and_backwards():
    now = datetime.now(UTC)
    assert timedelta(minutes=118) < core._parse_when("+2h") - now < timedelta(minutes=122)
    assert timedelta(hours=-6, minutes=2) > core._parse_when("-6h") - now
    assert core._parse_when("+30m") - now < timedelta(minutes=31)
    assert core._parse_when("+3d") - now > timedelta(days=2, hours=23)
    assert core._parse_when("+1.5h") - now > timedelta(minutes=89)


def test_unix_seconds():
    assert core._parse_when(1755424800) == datetime.fromtimestamp(1755424800, UTC)


def test_iso_with_a_zone_and_without_one():
    assert core._parse_when("2026-08-17T09:00:00+00:00") == datetime(
        2026, 8, 17, 9, 0, tzinfo=UTC
    )
    # a naive time is local: the person writes "at 9 in the morning" about themselves
    naive = core._parse_when("2026-08-17T09:00")
    assert naive.tzinfo is not None
    assert naive.replace(tzinfo=None) == datetime(2026, 8, 17, 9, 0)


@pytest.mark.parametrize("raw", ["tomorrow", "+2x", "+hour", "2h", "", "17.08.2026 09:00"])
def test_an_unreadable_time_is_an_error(raw):
    with pytest.raises(ValueError):
        core._parse_when(raw)


def test_the_day_in_utc():
    assert core._utc_day(datetime(2026, 8, 17, 23, 0, tzinfo=UTC)) == "2026-08-17"
    assert core._utc_day(None) is None


# ---------------------------------------------------------------- write limits


@pytest.fixture
def guard() -> RateGuard:
    return RateGuard(dict(config.LIMITS))


def test_the_sixteenth_chat_within_an_hour_runs_into_the_limit(guard):
    """Protection against fan-out mailing: fifteen chats an hour and not one more."""
    for i in range(15):
        guard.check_send(f"chat{i}")
        guard.record_send(f"chat{i}")
    guard.check_send("chat3")                   # a chat already seen — allowed
    with pytest.raises(GuardError, match="Mass-mailing guard"):
        guard.check_send("chat15")


def test_the_sixty_first_message_within_an_hour_runs_into_the_limit(guard):
    for _ in range(60):
        guard.check_send("chat0")
        guard.record_send("chat0")
    with pytest.raises(GuardError, match="Send guard"):
        guard.check_send("chat0")


def test_the_fifty_first_deletion_within_an_hour_runs_into_the_limit(guard):
    guard.check_delete(50)
    guard.record_delete(50)
    with pytest.raises(GuardError, match="Delete guard"):
        guard.check_delete(1)


def test_a_batch_deletion_counts_in_full(guard):
    guard.record_delete(49)
    guard.check_delete(1)
    with pytest.raises(GuardError):
        guard.check_delete(2)


def test_old_records_fall_out_of_the_window(guard, monkeypatch):
    """The window slides: an hour without sends lifts the restriction."""
    for i in range(15):
        guard.record_send(f"chat{i}")
    guard.sends = type(guard.sends)((t - 3601, c) for t, c in guard.sends)
    guard.check_send("chat15")          # the hour has passed — a sixteenth chat is fine again
    assert len(guard.sends) == 0        # and the old records fell out of the window


# ---------------------------------------------------------------- resolve


@pytest.fixture
def resolving(service, user, supergroup):
    dialogs = [
        FakeDialog(id=555, name=user.first_name, entity=user, is_user=True),
        FakeDialog(id=-100999, name="Team", entity=supergroup, is_group=True),
        FakeDialog(id=-100888, name="Design team", entity=supergroup, is_group=True),
        FakeDialog(id=-100777, name="Project archive", entity=supergroup,
                   is_group=True, archived=True),
    ]
    entities = {
        555: user, -100999: supergroup, -100888: supergroup, -100777: supergroup,
        "@petya": user,
    }
    service.client = FakeClient(entities=entities, dialogs=dialogs)
    return service


# "Избранное" stays Cyrillic on purpose: it is an alias the owner types, and
# core.SAVED_ALIASES matches it in every language regardless of TG_LANG.
@pytest.mark.parametrize("raw", ["me", "self", "saved", "Избранное", "  ME  "])
async def test_saved_messages_needs_no_call_to_the_server(resolving, raw):
    assert await resolving.resolve(raw) == "me"
    assert resolving.client.calls == []


async def test_a_number_as_a_string_is_an_id(resolving, user):
    assert await resolving.resolve("555") is user
    assert await resolving.resolve(-100999) is not None
    assert ("get_entity", 555) in resolving.client.calls


async def test_search_by_the_exact_title(resolving, supergroup):
    assert await resolving.resolve("team") is supergroup
    assert ("get_entity", -100999) in resolving.client.calls


async def test_an_ambiguous_title_is_an_error_and_not_a_guess(resolving):
    """Sending to the wrong chat is worse than not sending at all."""
    with pytest.raises(ValueError, match="the exact id"):
        await resolving.resolve("Tea")


async def test_an_archived_chat_is_found_by_title(resolving, supergroup):
    assert await resolving.resolve("Project archive") is supergroup


async def test_a_chat_that_does_not_exist(resolving):
    with pytest.raises(ValueError, match="neither in the dialog list"):
        await resolving.resolve("Uncle Fyodor")


async def test_an_empty_chat_is_an_error(resolving):
    with pytest.raises(ValueError, match="chat is required"):
        await resolving.resolve(None)


# ---------------------------------------------------------------- dialog kind


def test_the_dialog_kind_from_the_entity(user, supergroup):
    bot = types.User(id=1, first_name="Bot", bot=True)
    channel = types.Channel(id=2, title="News", photo=None, date=None, broadcast=True)
    old_group = types.Chat(id=3, title="Group", photo=None, participants_count=2,
                           date=None, version=1)
    kind = core.TelegramService.dialog_kind_of
    assert kind(user) == "user"
    assert kind(bot) == "bot"
    assert kind(supergroup) == "group"
    assert kind(channel) == "channel"
    assert kind(old_group) == "group"


def test_the_dialog_kind_from_the_dialog_flags(user):
    bot = types.User(id=1, first_name="Bot", bot=True)
    kind = core.TelegramService.dialog_kind
    assert kind(FakeDialog(1, user.first_name, entity=user, is_user=True)) == "user"
    assert kind(FakeDialog(1, "Bot", entity=bot, is_user=True)) == "bot"
    assert kind(FakeDialog(2, "Team", is_group=True)) == "group"
    assert kind(FakeDialog(3, "News", is_channel=True)) == "channel"


def test_the_dialog_row(service, user):
    d = FakeDialog(555, user.first_name, entity=user, is_user=True, unread_count=2,
                   date=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
                   message=FakeMessage(message="hi"))
    row = service.dialog_row(d)
    assert row["id"] == 555 and row["type"] == "user" and row["unread"] == 2
    assert row["link"] == "https://t.me/petya"
    assert row["last_text"] == "hi"
    assert row["last"] == "2026-08-17T09:00:00Z"


def test_the_link_to_a_dm(user):
    assert core.dm_link(user) == "https://t.me/petya"
    assert core.dm_link(types.User(id=42, first_name="No username")) == "tg://user?id=42"


# ---------------------------------------------------------------- account limits


def test_the_app_configuration_is_parsed_into_plain_python():
    tree = json_tree({"a": 10, "b": "text", "c": True, "d": None, "e": [1, {"f": 2.5}]})
    assert core._json_py(tree) == {
        "a": 10, "b": "text", "c": True, "d": None, "e": [1, {"f": 2.5}],
    }


def test_whole_numbers_in_the_configuration_do_not_stay_fractional():
    """Telegram sends every number as a float: "10.0 folders" reads as a bug."""
    assert core._json_py(json_tree({"n": 10})) == {"n": 10}
    assert isinstance(core._json_py(json_tree({"n": 10}))["n"], int)


async def test_the_limit_is_taken_by_whether_premium_is_there(service):
    raw = {
        "dialogs_pinned_limit_default": 5,
        "dialogs_pinned_limit_premium": 10,
        "transcribe_audio_trial_weekly_number": 0,
    }
    service.client = FakeConfigClient(raw)

    service.me = types.User(id=1, first_name="Someone", premium=False)
    plain = await service.limits()
    assert plain["premium"] is False
    assert plain["limits"]["dialogs_pinned_limit"]["value"] == 5

    service.me = types.User(id=1, first_name="Someone", premium=True)
    prem = await service.limits()
    assert prem["premium"] is True
    assert prem["limits"]["dialogs_pinned_limit"]["value"] == 10
    # Single values without a pair go separately and are not substituted.
    assert prem["single"]["transcribe_audio_trial_weekly_number"] == 0


async def test_a_limit_that_vanished_is_visible_and_does_not_keep_quiet(service):
    """Telegram is free to rename a key; "there is no limit" and "the limit did not
    arrive" are different things, and the second one is obliged to be noticeable."""
    service.me = types.User(id=1, first_name="Someone", premium=False)
    service.client = FakeConfigClient({"dialogs_pinned_limit_default": 5})
    out = await service.limits()
    assert "dialogs_pinned_limit" not in out["limits"]
    assert "dialogs_pinned_limit" in out["not_reported"]


async def test_full_mode_returns_every_pair_and_the_remaining_keys(service):
    service.me = types.User(id=1, first_name="Someone", premium=False)
    service.client = FakeConfigClient({
        "unknown_thing_limit_default": 1,
        "unknown_thing_limit_premium": 2,
        "some_flag": "premium",
        "domains": ["a.tld", "b.tld"],
    })
    out = await service.limits(full=True)
    assert out["all_pairs"]["unknown_thing_limit"]["premium"] == 2
    assert out["other"]["some_flag"] == "premium"
    # Nested values bloat the answer and have nothing to do with access.
    assert out["other"]["domains"] == "<list, 2>"
