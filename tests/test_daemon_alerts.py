"""The daemon: whom to wake, whom not, and what the inbox filters do.

The order of the checks in `alert_reason` is a contract about meaning, not an
implementation detail: "muted" must beat a keyword, quiet hours must beat
everything else, and our own alert bot must never wake the owner. The table of
cases below pins that down.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from conftest import FakeEvent, FakeMessage, FakeService, make_event
from telethon.tl import types

from tgagent import config


def _events() -> list[dict]:
    return [json.loads(x) for x in config.EVENTS_LOG.read_text().splitlines() if x.strip()]


def _actions() -> list[dict]:
    return [json.loads(x) for x in config.ACTIONS_LOG.read_text().splitlines() if x.strip()]


# ---------------------------------------------------------------- _chat_matches


@pytest.mark.parametrize(
    "patterns, expect",
    [
        ([], False),
        (["", "   "], False),
        (["555"], True),                 # by id, as a string
        ([555], True),                   # by id, as a number
        (["pete"], True),                # by title, case is ignored
        (["Pe"], True),                  # substring of the title
        (["556"], False),
        (["Basil"], False),
    ],
)
def test_chat_matches_a_pattern(daemon, patterns, expect):
    assert daemon._chat_matches(patterns, 555, "Pete") is expect


# ---------------------------------------------------------------- alert_reason


def _group(**over):
    base = {"chat": "Team", "chat_id": -100123, "chat_type": "group",
            "private": False, "from": "Mary", "from_id": 777}
    base.update(over)
    return make_event(**base)


def test_a_private_message_wakes_the_owner(daemon):
    assert daemon.alert_reason(make_event()) == "private"


def test_an_ordinary_group_message_does_not_wake_the_owner(daemon):
    assert daemon.alert_reason(_group()) is None


def test_a_mention_in_a_group_wakes_the_owner(daemon):
    assert daemon.alert_reason(_group(mentioned=True)) == "mention"


def test_a_keyword_wakes_the_owner_anywhere(daemon):
    daemon.rules["keywords"] = ["Invoice"]
    assert daemon.alert_reason(_group(text="an invoice arrived, please pay")) == "keyword"


def test_a_watched_chat_wakes_the_owner_on_any_message(daemon):
    daemon.rules["watch_chats"] = ["Team"]
    assert daemon.alert_reason(_group(text="anything at all")) == "watch"


def test_your_own_outgoing_message_does_not_wake_you(daemon):
    daemon.rules["keywords"] = ["invoice"]
    assert daemon.alert_reason(make_event(out=True, text="invoice")) is None


def test_a_message_from_our_own_bot_does_not_wake_the_owner(daemon):
    """Otherwise the alert would come back as an incoming message and wake the
    next alert — a loop."""
    daemon.self_bot_id = 4242
    daemon.rules["keywords"] = ["invoice"]
    assert daemon.alert_reason(make_event(from_id=4242, text="invoice")) is None


def test_a_bot_does_not_wake_the_owner_while_ignore_bots_is_on(daemon):
    assert daemon.alert_reason(make_event(from_bot=True)) is None
    daemon.rules["ignore_bots"] = False
    assert daemon.alert_reason(make_event(from_bot=True)) == "private"


def test_a_muted_chat_beats_a_keyword(daemon):
    """Order of the checks: mute before keywords. "Muted" means muted."""
    daemon.rules["keywords"] = ["invoice"]
    daemon.rules["mute_chats"] = ["Team"]
    assert daemon.alert_reason(_group(text="an invoice to pay")) is None


def test_a_muted_chat_beats_a_watched_chat(daemon):
    daemon.rules["watch_chats"] = ["Team"]
    daemon.rules["mute_chats"] = ["Team"]
    assert daemon.alert_reason(_group()) is None


def test_quiet_hours_beat_a_keyword(daemon, monkeypatch):
    daemon.rules["keywords"] = ["invoice"]
    monkeypatch.setattr(daemon, "in_quiet_hours", lambda now=None: True)
    assert daemon.alert_reason(make_event(text="invoice")) is None


def test_a_pause_and_disabled_rules_stay_silent(daemon):
    daemon.paused = True
    assert daemon.alert_reason(make_event()) is None
    daemon.paused = False
    daemon.rules["enabled"] = False
    assert daemon.alert_reason(make_event()) is None


def test_private_alerts_switched_off(daemon):
    daemon.rules["alert_on_private"] = False
    assert daemon.alert_reason(make_event()) is None
    daemon.rules["alert_on_mention"] = False
    assert daemon.alert_reason(_group(mentioned=True)) is None


def test_a_keyword_beats_a_watched_chat(daemon):
    """The reason of the alert must name the most specific match."""
    daemon.rules["keywords"] = ["invoice"]
    daemon.rules["watch_chats"] = ["Team"]
    assert daemon.alert_reason(_group(text="invoice")) == "keyword"


# ---------------------------------------------------------------- quiet hours


@pytest.mark.parametrize(
    "quiet, hour, expect",
    [
        (None, 3, False),
        ([], 3, False),
        ([23], 3, False),                 # an incomplete setting is ignored
        ([23, 8], 23, True),              # across midnight
        ([23, 8], 3, True),
        ([23, 8], 8, False),              # the right edge is not included
        ([23, 8], 22, False),
        ([9, 18], 9, True),               # an ordinary stretch
        ([9, 18], 18, False),
        ([9, 18], 3, False),
    ],
)
def test_quiet_hours(daemon, quiet, hour, expect):
    daemon.rules["quiet_hours"] = quiet
    now = datetime(2026, 8, 17, hour, 30)
    assert daemon.in_quiet_hours(now) is expect


# ---------------------------------------------------------------- waiting for a message


def _dm(**over):
    """A DM with the sender named outright. The cases below match on that name,
    so they must not depend on whichever name conftest happens to default to."""
    base = {"chat": "Pete", "from": "Pete"}
    base.update(over)
    return make_event(**base)


@pytest.mark.parametrize(
    "spec, ev, expect",
    [
        ({}, _dm(), True),
        ({}, _dm(out=True), False),                       # your own does not count
        ({"include_own": True}, _dm(out=True), True),
        ({"chat": "555"}, _dm(), True),                   # by id
        ({"chat": "@Pete"}, _dm(), True),                 # the @ is dropped
        ({"chat": "Pe"}, _dm(), True),                    # substring of the title
        ({"chat": "Basil"}, _dm(), False),
        ({"from_user": "777"}, _dm(), False),
        ({"from_user": "pete"}, _dm(), True),
        ({"keyword": "HEL"}, _dm(text="Hello"), True),
        ({"keyword": "invoice"}, _dm(text="Hello"), False),
        ({"private_only": True}, _dm(private=False), False),
        ({"private_only": True}, _dm(), True),
        ({"chat": "Pe", "keyword": "invoice"}, _dm(text="hello"), False),
    ],
)
def test_whom_tg_wait_is_waiting_for(daemon, spec, ev, expect):
    assert daemon._waiter_matches(spec, ev) is expect


# ---------------------------------------------------------------- inbox filters


@pytest.mark.parametrize(
    "rule, ev, expect",
    [
        ({"chat": "Team"}, _group(), True),
        ({"chat": "Pete"}, _group(), False),
        ({"from": "@mary"}, _group(), True),
        ({"from": "777"}, _group(), True),
        ({"from": "Basil"}, _group(), False),
        ({"keyword": ["invoice", "payment"]}, _group(text="PAYMENT needed"), True),
        ({"keyword": "invoice"}, _group(text="hello"), False),
        ({"type": "group"}, _group(), True),
        ({"type": "private"}, _group(), False),
        ({"type": "private"}, make_event(), True),
        ({"type": "bot"}, make_event(from_bot=True), True),
        ({"type": "channel"}, _group(chat_type="channel"), True),
        # conditions listed together are ANDed
        ({"chat": "Team", "keyword": "invoice"}, _group(text="invoice"), True),
        ({"chat": "Team", "keyword": "invoice"}, _group(text="hello"), False),
    ],
)
def test_filter_conditions(daemon, rule, ev, expect):
    assert daemon.auto_rule_matches(rule, ev) is expect


def test_an_old_event_without_a_chat_type_is_told_apart_by_private(daemon):
    ev = make_event()
    ev.pop("chat_type")
    assert daemon.auto_rule_matches({"type": "private"}, ev) is True
    assert daemon.auto_rule_matches({"type": "group"}, ev) is False


async def test_a_filter_carries_out_its_action_through_the_core(daemon):
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"name": "mail", "chat": "Team", "action": ["read", "archive"]}]

    fired = await daemon.run_auto_rules(_group(), "main")

    assert fired == [{"rule": "mail", "alert": False}]
    assert svc.calls == [("mark_read", {"chat": -100123}), ("archive", {"chat": -100123})]
    # an action of the agent, even one nobody asked for right now, has to land in the audit
    audit = _actions()
    assert [(r["action"], r["auto"], r["ok"]) for r in audit] == [
        ("mark_read", "mail", True), ("archive", "mail", True)
    ]


async def test_a_chat_wide_action_is_not_repeated(daemon):
    """Archiving a chat a second time is pointless, and in the audit it is noise."""
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"name": "archive", "chat": "Team", "action": "archive"}]
    await daemon.run_auto_rules(_group(message_id=1), "main")
    await daemon.run_auto_rules(_group(message_id=2), "main")
    assert svc.calls == [("archive", {"chat": -100123})]


async def test_an_action_on_a_message_is_repeated(daemon):
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"name": "to saved", "chat": "Team", "action": "save"}]
    await daemon.run_auto_rules(_group(message_id=1), "main")
    await daemon.run_auto_rules(_group(message_id=2), "main")
    assert svc.calls == [
        ("forward", {"from_chat": -100123, "message_ids": [1], "to_chat": "me"}),
        ("forward", {"from_chat": -100123, "message_ids": [2], "to_chat": "me"}),
    ]


async def test_a_rule_with_stop_cuts_the_pass_short(daemon):
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [
        {"name": "first", "chat": "Team", "action": "read", "stop": True},
        {"name": "second", "chat": "Team", "action": "archive"},
    ]
    fired = await daemon.run_auto_rules(_group(), "main")
    assert [f["rule"] for f in fired] == ["first"]
    assert [c[0] for c in svc.calls] == ["mark_read"]


async def test_a_disabled_rule_is_skipped(daemon):
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"chat": "Team", "action": "read", "enabled": False}]
    assert await daemon.run_auto_rules(_group(), "main") == []
    assert svc.calls == []


async def test_a_pause_stops_the_filters_too(daemon):
    """/pause turns off all of the automation, not half of it."""
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"chat": "Team", "action": "read"}]
    daemon.paused = True
    assert await daemon.run_auto_rules(_group(), "main") == []
    daemon.paused = False
    daemon.rules["enabled"] = False
    assert await daemon.run_auto_rules(_group(), "main") == []
    assert svc.calls == []


async def test_filters_do_not_touch_your_own_outgoing_messages(daemon):
    svc = FakeService()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"chat": "Team", "action": "read"}]
    assert await daemon.run_auto_rules(_group(out=True), "main") == []
    assert svc.calls == []


async def test_a_failed_action_lands_in_the_audit_and_does_not_break_the_others(daemon):
    class Broken(FakeService):
        async def mark_read(self, **kw):
            raise RuntimeError("server unavailable")

    svc = Broken()
    daemon.services["main"] = svc
    daemon.rules["auto"] = [{"name": "mail", "chat": "Team", "action": ["read", "archive"]}]
    fired = await daemon.run_auto_rules(_group(), "main")
    assert [f["rule"] for f in fired] == ["mail"]
    assert svc.calls == [("archive", {"chat": -100123})]
    audit = _actions()
    assert audit[0]["ok"] is False and "server unavailable" in audit[0]["error"]


# ---------------------------------------------------------------- the watcher end to end


def _incoming(text="hello", chat=None, sender=None, **msg_kw):
    user = sender or types.User(id=555, first_name="Pete")
    chat = chat if chat is not None else user
    msg = FakeMessage(id=10, message=text, **msg_kw)
    return FakeEvent(msg, chat, user, is_private=isinstance(chat, types.User))


async def test_a_filter_that_fired_suppresses_the_alert(daemon, bot):
    """The rule has already dealt with the message — there is no reason to wake
    the owner over it."""
    daemon.bot = bot
    daemon.services["main"] = FakeService()
    daemon.rules["auto"] = [{"name": "mail", "chat": "555", "action": "read"}]

    await daemon.on_new_message(_incoming(), "main")

    assert bot.sent == []
    logged = _events()[0]
    assert logged["auto"] == ["mail"]


async def test_the_alert_flag_gives_the_alert_back_to_a_rule_that_fired(daemon, bot):
    daemon.bot = bot
    daemon.services["main"] = FakeService()
    daemon.rules["auto"] = [{"name": "mail", "chat": "555", "action": "read", "alert": True}]

    await daemon.on_new_message(_incoming(), "main")

    assert len(bot.sent) == 1 and "Pete" in bot.sent[0]


async def test_the_alert_goes_out_and_the_event_is_written_to_the_log(daemon, bot):
    daemon.bot = bot
    await daemon.on_new_message(_incoming(text="hello"), "main")
    assert len(bot.sent) == 1
    assert "<b>DM</b>" in bot.sent[0] and "hello" in bot.sent[0]
    logged = _events()[0]
    assert logged["chat_id"] == 555 and logged["text"] == "hello" and logged["out"] is False


async def test_a_second_message_from_the_same_chat_is_held_back_by_the_interval(daemon, bot):
    daemon.bot = bot
    daemon.rules["min_interval_sec"] = 3600
    await daemon.on_new_message(_incoming(text="one"), "main")
    await daemon.on_new_message(_incoming(text="two"), "main")
    assert len(bot.sent) == 1
    # the alert is held back, the log is not: the event must stay visible
    assert len(_events()) == 2


async def test_an_event_that_did_not_alert_is_still_written_to_the_log(daemon, bot):
    daemon.bot = bot
    channel = types.Channel(id=1234567890, title="Team", photo=None, date=None, megagroup=True)
    await daemon.on_new_message(_incoming(text="chatter", chat=channel), "main")
    assert bot.sent == []
    logged = _events()[0]
    assert logged["chat"] == "Team" and logged["chat_type"] == "group"
    assert logged["link"] == "https://t.me/c/1234567890/10"


async def test_an_error_inside_the_watcher_does_not_kill_it(daemon, bot):
    """The watcher has to survive any malformed message: there is one of it for
    the whole stream."""
    daemon.bot = bot
    broken = FakeEvent(None, None, None)
    await daemon.on_new_message(broken, "main")     # must not throw outwards
    assert bot.sent == []
