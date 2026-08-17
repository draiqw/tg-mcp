"""Write confirmation: ask the owner before a writing call.

This is the owner's restriction over the agent, so we check all of it: that the
question is asked, that silence counts as a refusal, and that with the mode on
but no bot configured writing is simply forbidden.
"""

from __future__ import annotations

import json

import pytest
from conftest import FakeService
from telethon.tl import types

from tgagent import config
from tgagent.core import GuardError


def set_confirm(**values) -> None:
    """The confirmation mode lives on disk and is read on every writing call."""
    config.RULES_FILE.write_text(json.dumps(values, ensure_ascii=False))


@pytest.fixture
def asked(monkeypatch, daemon):
    """A substituted `ask`: records the question and answers with a preset reply."""
    box = {"reply": {"answered": True, "answer": "allow", "how": "button"}, "asked": []}

    async def fake_ask(question, options=None, timeout=300):
        box["asked"].append({"question": question, "options": options, "timeout": timeout})
        return box["reply"]

    monkeypatch.setattr(daemon, "ask", fake_ask)
    return box


@pytest.fixture
def svc():
    return FakeService(entities={"Petya": types.User(id=555, first_name="Petya")})


# ---------------------------------------------------------------- parsing the mode


@pytest.mark.parametrize(
    "raw, expect",
    [
        (None, "off"), ("", "off"), ("off", "off"), ("no", "off"), ("false", "off"),
        ("all", "all"), ("ALL", "all"), ("yes", "all"), ("1", "all"), ("on", "all"),
        ("outgoing", "outgoing"), ("out", "outgoing"), ("external", "outgoing"),
    ],
)
def test_confirmation_mode_is_parsed(daemon, raw, expect):
    assert daemon.confirm_mode({"confirm_writes": raw}) == expect


def test_unknown_mode_forbids_writing(daemon):
    """A typo in the setting must not quietly turn into "write without asking"."""
    with pytest.raises(GuardError, match="is not a mode I know"):
        daemon.confirm_mode({"confirm_writes": "sometimes"})


# ---------------------------------------------------------------- when we do not ask


async def test_mode_off_asks_nothing(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="off")
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert asked["asked"] == []


async def test_a_question_to_the_owner_needs_no_confirmation_itself(daemon, svc, asked, bot):
    """ask and alert are a conversation with the owner themselves; otherwise the
    question would loop."""
    daemon.bot = bot
    set_confirm(confirm_writes="all")
    for method in ("ask", "alert"):
        await daemon.confirm_write(svc, method, {"question": "?"})
    assert asked["asked"] == []


async def test_in_outgoing_mode_silent_methods_pass(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="outgoing")
    for method in ("mark_read", "mute", "archive", "draft", "folder_edit"):
        await daemon.confirm_write(svc, method, {"chat": "Petya"})
    assert asked["asked"] == []
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert len(asked["asked"]) == 1


async def test_in_all_mode_even_silent_methods_are_asked_about(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    await daemon.confirm_write(svc, "mark_read", {"chat": "Petya"})
    assert len(asked["asked"]) == 1


async def test_a_read_disguised_as_a_write_is_not_asked_about(daemon, svc, asked, bot):
    """`stories` counts as writing only because of a single argument."""
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    await daemon.confirm_write(svc, "stories", {"chat": "Petya"})
    assert asked["asked"] == []
    await daemon.confirm_write(svc, "stories", {"chat": "Petya", "mark_read": True})
    assert len(asked["asked"]) == 1


# ---------------------------------------------------------------- the whitelist


async def test_saved_messages_are_whitelisted_by_default(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all")
    await daemon.confirm_write(svc, "send", {"chat": "me", "text": "a note"})
    assert asked["asked"] == []


async def test_whitelist_works_by_name_and_by_id(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=["petya"])
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    set_confirm(confirm_writes="all", confirm_whitelist=["555"])
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert asked["asked"] == []


async def test_whitelist_is_checked_before_the_bot(daemon, svc, asked):
    """A chat we were never going to ask about must not run into an unconfigured
    channel."""
    set_confirm(confirm_writes="all", confirm_whitelist=["me"])
    await daemon.confirm_write(svc, "send", {"chat": "me", "text": "a note"})
    assert daemon.bot.configured is False


async def test_someone_elses_chat_is_not_covered_by_the_whitelist(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=["me"])
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert len(asked["asked"]) == 1


# ---------------------------------------------------------------- the question itself


async def test_mode_on_without_a_bot_forbids_writing(daemon, svc, asked):
    """There is nobody to ask for permission — which means writing is forbidden."""
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    with pytest.raises(GuardError, match="the bot is not configured"):
        await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert asked["asked"] == []


async def test_consent_lets_the_call_through(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    asked["reply"] = {"answered": True, "answer": "Allow"}
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert len(asked["asked"]) == 1


@pytest.mark.parametrize("answer", ["yes", "ok", "okay", "y", "+", "go", "go ahead", "do it"])
async def test_short_forms_of_consent(daemon, svc, asked, bot, answer):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    asked["reply"] = {"answered": True, "answer": answer}
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})


@pytest.mark.parametrize("answer", ["deny", "no", "not now", "", "later"])
async def test_a_refusal_breaks_off_the_call(daemon, svc, asked, bot, answer):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    asked["reply"] = {"answered": True, "answer": answer}
    with pytest.raises(GuardError, match="did not confirm"):
        await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})


async def test_silence_is_not_consent(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    asked["reply"] = {"answered": False, "timeout_sec": 90}
    with pytest.raises(GuardError, match="no answer within"):
        await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})


async def test_the_wait_is_no_longer_than_the_client_waits(daemon, svc, asked, bot):
    """The MCP client waits for the daemon no longer than 120 s: permission granted
    later would lead to a send the agent has already been told was a "network
    error"."""
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[], confirm_timeout_sec=600)
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert asked["asked"][0]["timeout"] == 110
    set_confirm(confirm_writes="all", confirm_whitelist=[], confirm_timeout_sec=1)
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "hi"})
    assert asked["asked"][1]["timeout"] == 10


async def test_the_question_shows_where_and_what_will_go_out(daemon, svc, asked, bot):
    """"Allow a write?" without the details is something the owner would confirm
    without looking."""
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    await daemon.confirm_write(
        svc, "send", {"chat": "Petya", "text": "transferring the money", "silent": True}
    )
    question = asked["asked"][0]["question"]
    assert "The agent wants: send" in question
    assert "chat: Petya (id 555)" in question
    assert "text: transferring the money" in question
    assert "silent" in question
    assert asked["asked"][0]["options"] == ["allow", "deny"]


async def test_the_question_shows_a_non_main_account(daemon, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    work = FakeService(account="work", entities={"Petya": types.User(id=555, first_name="Petya")})
    await daemon.confirm_write(work, "send", {"chat": "Petya", "text": "hi"})
    assert "account: work" in asked["asked"][0]["question"]


async def test_a_long_text_is_cut_in_the_question(daemon, svc, asked, bot):
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    await daemon.confirm_write(svc, "send", {"chat": "Petya", "text": "a" * 1000})
    question = asked["asked"][0]["question"]
    assert "…" in question and len(question) < 600


async def test_for_a_forward_the_recipient_is_asked_about(daemon, asked, bot):
    """For forward it is the recipient that matters, not the source."""
    daemon.bot = bot
    set_confirm(confirm_writes="all", confirm_whitelist=[])
    svc = FakeService(entities={
        "Petya": types.User(id=555, first_name="Petya"),
        "Masha": types.User(id=777, first_name="Masha"),
    })
    await daemon.confirm_write(
        svc, "forward", {"from_chat": "Petya", "to_chat": "Masha", "message_ids": [1]}
    )
    assert "chat: Masha (id 777)" in asked["asked"][0]["question"]
