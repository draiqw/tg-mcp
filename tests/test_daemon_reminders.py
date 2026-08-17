"""Reminders and the scheduled digest — what the daemon does on its own, without the agent."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeBot, FakeService, make_event

from tgagent import config
from tgagent.daemon import Daemon


def _write_events(rows: list[dict]) -> None:
    config.EVENTS_LOG.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    )


def _at(minutes_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


# ------------------------------------------------------------------ reminders


@pytest.fixture
def reminding(daemon, bot):
    daemon.bot = bot
    return daemon


async def test_reminder_is_created_and_lands_on_disk(reminding):
    res = await reminding.remind(text="call Pete", when="+2h")
    assert res["created"] is True and res["id"] == "r1"
    assert 119 < res["in_min"] < 121

    stored = json.loads(config.REMINDERS_FILE.read_text())
    assert stored["seq"] == 1
    assert stored["items"][0]["text"] == "call Pete"
    assert oct(config.REMINDERS_FILE.stat().st_mode & 0o777) == "0o600"


async def test_reminder_survives_a_restart(reminding):
    await reminding.remind(text="call Pete", when="+2h")
    await reminding.remind(text="and Mary", when="+3h")

    restarted = Daemon()                     # a new daemon process, the same disk
    restarted.load_reminders()

    assert [r["text"] for r in restarted.reminders] == ["call Pete", "and Mary"]
    assert restarted._reminder_seq == 2      # the next id will not collide with an old one


async def test_an_overdue_reminder_is_not_thrown_away_on_startup(reminding):
    """Waking someone up late is still better than not waking them at all."""
    config.REMINDERS_FILE.write_text(json.dumps({"seq": 5, "items": [
        {"id": "r5", "text": "was yesterday", "at": _at(minutes_ago=24 * 60)},
    ]}))
    restarted = Daemon()
    restarted.load_reminders()
    assert [r["id"] for r in restarted.reminders] == ["r5"]


async def test_a_broken_reminders_file_does_not_break_startup(reminding):
    config.REMINDERS_FILE.write_text("{this is not json")
    restarted = Daemon()
    restarted.load_reminders()
    assert restarted.reminders == []


async def test_listing_and_cancelling(reminding):
    await reminding.remind(text="second", when="+3h")
    await reminding.remind(text="first", when="+1h")

    items = (await reminding.remind(list=True))["reminders"]
    assert [r["text"] for r in items] == ["first", "second"]   # by due time, not by input

    assert (await reminding.remind(cancel="r2"))["cancelled"] is True
    assert [r["id"] for r in reminding.reminders] == ["r1"]
    assert json.loads(config.REMINDERS_FILE.read_text())["items"][0]["id"] == "r1"

    with pytest.raises(ValueError, match="r9"):
        await reminding.remind(cancel="r9")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({}, "text is required"),
        ({"text": "  "}, "text is required"),
        ({"text": "remind me"}, "when is required"),
        ({"text": "remind me", "when": "-1h"}, "only be set for the future"),
        ({"text": "remind me", "when": "+1h", "unless_reply": True}, "unless_reply"),
    ],
)
async def test_a_malformed_reminder_is_rejected(reminding, kwargs, message):
    with pytest.raises(ValueError, match=message):
        await reminding.remind(**kwargs)


async def test_without_a_bot_a_reminder_cannot_be_created(daemon):
    """It would have nowhere to fire, and a silent reminder is worse than a missing one."""
    with pytest.raises(RuntimeError, match="the bot is not configured"):
        await daemon.remind(text="call", when="+1h")


async def test_when_the_time_comes_the_reminder_is_sent_and_removed(reminding, bot):
    await reminding.remind(text="call Pete", when="+1h")
    reminding.reminders[0]["at"] = _at(minutes_ago=1)

    await reminding.check_reminders()

    assert len(bot.sent) == 1
    assert "call Pete" in bot.sent[0]
    assert reminding.reminders == []
    assert json.loads(config.REMINDERS_FILE.read_text())["items"] == []


async def test_a_send_that_failed_does_not_make_an_eternal_alarm_clock(reminding):
    class Broken(FakeBot):
        async def send(self, text, chat_id=None, silent=False):
            raise RuntimeError("Bot API unavailable")

    reminding.bot = Broken(configured=True)
    await reminding.remind(text="call", when="+1h")
    reminding.reminders[0]["at"] = _at(minutes_ago=1)

    await reminding.check_reminders()

    assert reminding.reminders == []      # taken off the queue before the send, not after


async def test_a_future_reminder_is_left_alone(reminding, bot):
    await reminding.remind(text="call", when="+2h")
    await reminding.check_reminders()
    assert bot.sent == [] and len(reminding.reminders) == 1


async def test_a_reply_cancels_a_reminder_with_a_condition(reminding):
    await reminding.remind(text="Pete has not replied", when="+2h", chat="Pete",
                           unless_reply=True)

    reminding.feed_waiters(make_event(chat="Pete", **{"from": "Pete"}, text="replied!"))

    assert reminding.reminders == []
    assert json.loads(config.REMINDERS_FILE.read_text())["items"] == []


@pytest.mark.parametrize(
    "ev, note",
    [
        (make_event(out=True), "one's own outgoing message does not count as a reply"),
        (make_event(kind="reaction"), "a reaction is not a reply: asked in text, waiting for text"),
        (make_event(chat="Mary", chat_id=777, from_id=777, **{"from": "Mary"}), "a different chat"),
    ],
)
async def test_a_reminder_is_cancelled_only_by_a_real_reply(reminding, ev, note):
    await reminding.remind(text="Pete has not replied", when="+2h", chat="555",
                           unless_reply=True)
    reminding.feed_waiters(ev)
    assert len(reminding.reminders) == 1, note


async def test_a_reminder_without_the_condition_is_not_cancelled_by_a_reply(reminding):
    await reminding.remind(text="call", when="+2h", chat="Pete")
    reminding.feed_waiters(make_event(chat="Pete", **{"from": "Pete"}, text="hi"))
    assert len(reminding.reminders) == 1


# ------------------------------------------------------- waiting for a message


async def test_a_waiter_wakes_up_on_its_own_message(daemon):
    task = asyncio.create_task(daemon.wait(chat="Pete", timeout=5))
    await asyncio.sleep(0)
    daemon.feed_waiters(make_event(chat="Pete", **{"from": "Pete"}, text="hi"))
    res = await task
    assert res["got"] is True and res["event"]["text"] == "hi"
    assert daemon.waiters == []


async def test_someone_elses_message_does_not_wake_the_waiter(daemon):
    task = asyncio.create_task(daemon.wait(chat="Pete", timeout=5))
    await asyncio.sleep(0)
    daemon.feed_waiters(make_event(chat="Mary", chat_id=777))
    assert not task.done()
    daemon.feed_waiters(make_event(chat="Pete", **{"from": "Pete"}))
    assert (await task)["got"] is True


# --------------------------------------------------------------- digest slots


def test_slots_are_counted_for_today_and_yesterday(daemon):
    now = datetime(2026, 8, 17, 21, 0)
    slots = daemon._digest_slots([(9, 0), (20, 0)], now)
    assert [s.strftime("%d %H:%M") for s in slots] == [
        "16 09:00", "16 20:00", "17 09:00", "17 20:00"
    ]


def test_the_period_is_counted_from_the_previous_slot(daemon):
    """For the first slot of the day the previous one is yesterday's evening slot,
    otherwise the morning digest would not cover the night. The slots here are
    local and naive — exactly as check_digest counts them."""
    due = datetime(2026, 8, 17, 9, 0)
    slots = daemon._digest_slots([(9, 0), (20, 0)], due)
    start = daemon._digest_period_start(due, slots)
    assert start.astimezone().strftime("%d %H:%M") == "16 20:00"


def test_the_period_continues_from_the_last_send(daemon):
    due = datetime(2026, 8, 17, 20, 0)
    sent_at = (due - timedelta(hours=3)).astimezone(UTC)
    daemon.digest_state = {"covered_since": sent_at.isoformat()}
    slots = daemon._digest_slots([(9, 0), (20, 0)], due)
    assert daemon._digest_period_start(due, slots) == sent_at


def test_the_period_goes_no_deeper_than_a_day_and_a_bit(daemon):
    """A digest after a long downtime must not turn into a weekly report."""
    due = datetime(2026, 8, 17, 20, 0)
    long_ago = (due - timedelta(days=7)).astimezone(UTC)
    daemon.digest_state = {"covered_since": long_ago.isoformat()}
    slots = daemon._digest_slots([(20, 0)], due)
    start = daemon._digest_period_start(due, slots)
    assert start == due.astimezone(UTC) - timedelta(hours=26)


def test_a_broken_digest_state_does_not_break_startup(daemon):
    config.DIGEST_FILE.write_text("not json")
    daemon.load_digest_state()
    assert daemon.digest_state == {}


# ------------------------------------------------------------ building the digest


async def test_an_empty_period_produces_no_digest(daemon):
    since = datetime.now(UTC) - timedelta(hours=6)
    assert await daemon.build_digest(since, datetime.now()) is None
    _write_events([])
    assert await daemon.build_digest(since, datetime.now()) is None


async def test_own_outgoing_and_own_bot_messages_do_not_go_into_the_digest(daemon):
    daemon.tg = FakeService()
    daemon.self_bot_id = 4242
    _write_events([
        make_event(at=_at(30), out=True, text="this one I wrote myself"),
        make_event(at=_at(29), from_id=4242, text="this is my own alert coming back"),
        make_event(at=_at(28), from_bot=True, text="an ad"),
    ])
    since = datetime.now(UTC) - timedelta(hours=1)
    assert await daemon.build_digest(since, datetime.now()) is None


async def test_the_digest_counts_chats_and_highlights_by_rules(daemon):
    daemon.tg = FakeService()
    daemon.rules["keywords"] = ["bill"]
    daemon.rules["mute_chats"] = ["Noise"]
    _write_events([
        make_event(at=_at(30), chat="Pete", **{"from": "Pete"}, text="hi"),
        make_event(at=_at(29), chat="Pete", **{"from": "Pete"}, text="a bill to pay arrived"),
        make_event(at=_at(28), chat="Noise", chat_id=-100999, text="bill", private=False),
        make_event(at=_at(27), chat="Noise", chat_id=-100999, text="more", private=False),
    ])

    body = await daemon.build_digest(datetime.now(UTC) - timedelta(hours=1),
                                     datetime.now())

    assert "Chats: 2 · messages: 4" in body
    assert "· Noise — 2" in body               # a muted chat still counts
    assert "word “bill”" in body
    assert body.count("word “bill”") == 1      # but it is not highlighted
    assert "Pete" in body


async def test_old_events_do_not_fall_into_the_period(daemon):
    daemon.tg = FakeService()
    _write_events([
        make_event(at=_at(minutes_ago=300), text="long ago"),
        make_event(at=_at(minutes_ago=10), text="fresh"),
    ])
    body = await daemon.build_digest(datetime.now(UTC) - timedelta(hours=1),
                                     datetime.now())
    assert "messages: 1" in body


async def test_a_broken_log_line_does_not_break_the_digest(daemon):
    daemon.tg = FakeService()
    config.EVENTS_LOG.write_text(
        "{a broken line\n\n" + json.dumps(make_event(at=_at(10), text="intact")) + "\n"
    )
    body = await daemon.build_digest(datetime.now(UTC) - timedelta(hours=1),
                                     datetime.now())
    assert "messages: 1" in body


async def test_reactions_are_counted_separately(daemon):
    daemon.tg = FakeService()
    _write_events([make_event(at=_at(10), kind="reaction", text="👍")])
    body = await daemon.build_digest(datetime.now(UTC) - timedelta(hours=1),
                                     datetime.now())
    assert "Reactions to your messages: 1" in body
    assert "messages: 0" in body


# -------------------------------------------------------- the digest schedule


def _slot_passed(minutes_ago: int = 30) -> str:
    return (datetime.now() - timedelta(minutes=minutes_ago)).strftime("%H:%M")


async def test_a_slot_is_served_only_once(daemon, bot):
    daemon.bot = bot
    daemon.tg = FakeService()
    daemon.rules["digest_at"] = [_slot_passed()]
    _write_events([make_event(at=_at(10), text="hi")])

    await daemon.check_digest()
    assert len(bot.sent) == 1 and bot.sent[0].startswith("<b>Digest</b>")
    assert daemon.digest_state["covered_since"]

    await daemon.check_digest()
    assert len(bot.sent) == 1     # the same slot is not served a second time


async def test_a_pause_closes_the_slot_but_does_not_eat_the_period(daemon, bot):
    """A skipped digest must not swallow the messages — they go into the next one."""
    daemon.bot = bot
    daemon.tg = FakeService()
    daemon.paused = True
    daemon.rules["digest_at"] = [_slot_passed()]
    _write_events([make_event(at=_at(10), text="hi")])

    await daemon.check_digest()

    assert bot.sent == []
    assert daemon.digest_state["last_slot"]
    assert "covered_since" not in daemon.digest_state


async def test_the_digest_state_survives_a_restart(daemon, bot):
    daemon.bot = bot
    daemon.tg = FakeService()
    daemon.rules["digest_at"] = [_slot_passed()]
    _write_events([make_event(at=_at(10), text="hi")])
    await daemon.check_digest()

    restarted = Daemon()
    restarted.bot = bot
    restarted.tg = FakeService()
    restarted.rules["digest_at"] = daemon.rules["digest_at"]
    restarted.load_digest_state()
    await restarted.check_digest()

    assert len(bot.sent) == 1     # a restart does not lead to a second digest for the slot


async def test_a_malformed_schedule_does_not_break_the_tick(daemon, bot):
    daemon.bot = bot
    daemon.rules["digest_at"] = ["at night"]
    await daemon.check_digest()
    assert bot.sent == [] and daemon.digest_state == {}


async def test_without_a_schedule_there_are_no_digests(daemon, bot):
    daemon.bot = bot
    await daemon.check_digest()
    assert bot.sent == [] and daemon.digest_state == {}
