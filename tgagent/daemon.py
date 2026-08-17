"""Long-running daemon: the single owner of the Telegram session.

Responsibilities
  * hold one authorised Telethon client (session files do not tolerate two writers)
  * expose every operation over a unix socket for the MCP server to call
  * watch incoming messages, match them against rules, alert through the bot
  * accept simple commands sent to the bot
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import signal
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import web
from telethon import events, utils
from telethon.tl import types

from . import alerts, config
from .core import GuardError, TelegramService, entity_name
from .core import _day_start as day_start
from .core import _parse_when as parse_when
from .core import reaction_of as core_reaction_of

MAX_EVENT_LOG_BYTES = 20 * 1024 * 1024
REMINDER_TICK_SEC = 30
DIGEST_TICK_SEC = 30
# The daemon was down longer — a "since morning" digest arriving in the evening
# is not a digest any more but junk: the slot is marked passed and nothing is
# sent.
DIGEST_CATCHUP_SEC = 2 * 3600
# Cap on the period for the case where the daemon has been gone for days.
DIGEST_MAX_PERIOD_SEC = 26 * 3600
DIGEST_TOP_CHATS = 6
DIGEST_HIGHLIGHTS = 8
# Filter actions that change the whole chat rather than one message: repeating
# them on every next message of the same chat makes no sense.
AUTO_CHAT_LEVEL = {"archive", "mute", "folder"}


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, flush=True)


class Daemon:
    def __init__(self) -> None:
        # One process holds every signed-in account: a session does not tolerate
        # two writers, so each one has exactly one owner — this daemon.
        self.services: dict[str, TelegramService] = {}
        self.tg: TelegramService | None = None      # main one, for alerts and the bot
        self.bot = alerts.BotChannel()
        self.rules = config.load_rules()
        self.started_at = time.time()
        self.last_alert: dict[tuple[str, int], float] = {}
        self.alert_count = 0
        self.paused = False
        # Who is waiting for an incoming message (tg_wait) and for an answer from
        # the owner (tg_ask) right now.
        self.waiters: list[tuple[dict, asyncio.Future]] = []
        self.questions: dict[str, dict] = {}
        self._question_seq = 0
        # Reminders survive a restart, so they live on disk, not only here.
        self.reminders: list[dict] = []
        self._reminder_seq = 0
        # When the digest last went out — on disk as well: a daemon restart must
        # not lead to a second digest for the same slot.
        self.digest_state: dict = {}
        # Chat-level filter actions already carried out in this life of the daemon.
        self._auto_done: set[tuple[str, str, int]] = set()
        # Our own alert bot writes into this account too. Never alert on its
        # messages: the alert would arrive as a new incoming message and loop.
        token = config.bot_token()
        self.self_bot_id = int(token.split(":")[0]) if token and ":" in token else None

    # ---------- event log ----------

    def append_event(self, event: dict) -> None:
        try:
            if config.EVENTS_LOG.exists() and config.EVENTS_LOG.stat().st_size > MAX_EVENT_LOG_BYTES:
                config.EVENTS_LOG.rename(config.EVENTS_LOG.with_suffix(".jsonl.1"))
            with config.EVENTS_LOG.open("a") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception as exc:  # never let logging kill the watcher
            log(f"event log failed: {exc}")

    def append_action(
        self, method: str, params: dict, result: Any, error: str | None,
        account: str = config.MAIN_ACCOUNT, auto: str | None = None,
    ) -> None:
        if method not in WRITE_METHODS and method not in AUDIT_ONLY:
            return
        if method == "remind" and params.get("list"):
            return   # remind has one entry for all of it; listing is a read, not an action
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "action": method,
            "params": {k: v for k, v in params.items() if k != "text"},
            "text": (params.get("text") or "")[:400] or None,
            "ok": error is None,
            "error": error,
        }
        if auto:
            # A filter that fired is an action of the agent too, just one nobody
            # asked for right now; in the audit it is marked with the rule name.
            record["auto"] = auto
        try:
            with config.ACTIONS_LOG.open("a") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log(f"action log failed: {exc}")

    # ---------- rules ----------

    def _chat_matches(self, patterns: list, chat_id: int, chat_name: str) -> bool:
        for p in patterns or []:
            s = str(p).strip().lower()
            if not s:
                continue
            if s == str(chat_id) or s in (chat_name or "").lower():
                return True
        return False

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        """The owner's quiet hours. Shared by alerts and the digest: "do not wake
        me at night" must not mean "except for the 03:00 digest"."""
        qh = self.rules.get("quiet_hours")
        if not qh or len(qh) != 2:
            return False
        hour = (now or datetime.now()).hour
        start, end = int(qh[0]), int(qh[1])
        return start <= hour or hour < end if start > end else start <= hour < end

    def alert_reason(self, ev: dict) -> str | None:
        r = self.rules
        if self.paused or not r.get("enabled", True):
            return None
        if ev.get("out"):
            return None
        if self.self_bot_id and ev.get("from_id") == self.self_bot_id:
            return None  # alert-loop guard, not configurable on purpose
        if r.get("ignore_bots") and ev.get("from_bot"):
            return None
        if self._chat_matches(r.get("mute_chats", []), ev["chat_id"], ev.get("chat", "")):
            return None

        if self.in_quiet_hours():
            return None

        text = (ev.get("text") or "").lower()
        for kw in r.get("keywords", []):
            if kw and kw.lower() in text:
                return "keyword"
        if self._chat_matches(r.get("watch_chats", []), ev["chat_id"], ev.get("chat", "")):
            return "watch"
        if r.get("alert_on_mention") and ev.get("mentioned"):
            return "mention"
        if r.get("alert_on_private") and ev.get("private"):
            return "private"
        return None

    # ---------- inbox filters ----------

    def _sender_matches(self, patterns: list, ev: dict) -> bool:
        for p in patterns:
            s = str(p).strip().lower().lstrip("@")
            if not s:
                continue
            if s == str(ev.get("from_id")) or s in (ev.get("from") or "").lower():
                return True
        return False

    @staticmethod
    def _type_matches(kind: str, ev: dict) -> bool:
        """The dialog type as the owner names it: private/group/channel/bot.

        Taken from the type of the chat, not of the sender: bots write in groups
        too, and that does not stop the chat from being a group.
        """
        chat_type = ev.get("chat_type")
        if chat_type is None:   # an old event without the field — tell apart what we can
            chat_type = "user" if ev.get("private") else "group"
        return {
            "private": chat_type == "user",
            "bot": chat_type == "bot" or bool(ev.get("from_bot")),
            "group": chat_type == "group",
            "channel": chat_type == "channel",
        }[str(kind).strip().lower()]

    def auto_rule_matches(self, rule: dict, ev: dict) -> bool:
        """The same matcher as for alerts: conditions listed together are all ANDed."""
        chats = config.as_list(rule.get("chat"))
        if chats and not self._chat_matches(chats, ev["chat_id"], ev.get("chat", "")):
            return False
        senders = config.as_list(rule.get("from"))
        if senders and not self._sender_matches(senders, ev):
            return False
        words = [str(k).lower() for k in config.as_list(rule.get("keyword")) if str(k).strip()]
        if words:
            text = (ev.get("text") or "").lower()
            if not any(w in text for w in words):
                return False
        if rule.get("type") and not self._type_matches(rule["type"], ev):
            return False
        return True

    def _auto_call(self, svc: TelegramService, action: str, rule: dict, ev: dict):
        """Filter action -> core method. Nothing here happens outside the core:
        the same `_assert_write`, the same limits, the same audit entry."""
        chat = ev["chat_id"]
        if action == "read":
            return "mark_read", {"chat": chat}, svc.mark_read
        if action == "archive":
            return "archive", {"chat": chat}, svc.archive
        if action == "mute":
            params = {"chat": chat}
            if rule.get("hours"):
                params["hours"] = int(rule["hours"])
            return "mute", params, svc.mute
        if action == "folder":
            return "folder_edit", {"folder": rule["folder"], "add": [chat]}, svc.folder_edit
        if action == "save":
            # The only action that sends anything at all — and its recipient is
            # hardwired: Saved Messages. A rule cannot write to an outsider.
            return "forward", {
                "from_chat": chat, "message_ids": [ev["message_id"]], "to_chat": "me",
            }, svc.forward
        raise ValueError(f"unknown filter action: {action}")

    async def run_auto_action(
        self, svc: TelegramService, name: str, action: str, rule: dict, ev: dict
    ) -> bool:
        key = (name, action, int(ev["chat_id"]))
        if action in AUTO_CHAT_LEVEL and key in self._auto_done:
            return True
        method, params, fn = self._auto_call(svc, action, rule, ev)
        try:
            await fn(**params)
        except Exception as exc:
            # folder_edit cannot answer "already there": if the chat is in the
            # folder there is nothing to change, and that is a refusal, not a
            # failure. Everything else is an honest error and goes to the audit.
            # The fragment matched here is the message raised in core.folder_edit.
            if action == "folder" and "nothing to change" in str(exc):
                self._auto_done.add(key)
                return True
            self.append_action(method, params, None, str(exc), svc.account, auto=name)
            log(f"auto[{name}]: {action} not carried out: {exc}")
            return False
        self.append_action(method, params, None, None, svc.account, auto=name)
        if action in AUTO_CHAT_LEVEL:
            self._auto_done.add(key)
        return True

    async def run_auto_rules(self, ev: dict, account: str) -> list[dict]:
        """Inbox filters: the same condition as an alert, but instead of "wake the
        owner" — an action. Rules run top to bottom, every matching one fires; a
        rule with `stop` cuts the pass short.

        It lives in the daemon for the same reason as `wait` and `remind`: it needs
        the stream of incoming messages, not a separate call to Telegram.
        """
        items = self.rules.get("auto") or []
        if not items or ev.get("out"):
            return []
        if self.paused or not self.rules.get("enabled", True):
            return []   # /pause stops all of the automation, not half of it
        if self.self_bot_id and ev.get("from_id") == self.self_bot_id:
            return []   # a message from our own alert bot is not an incoming one
        svc = self.services.get(account)
        if svc is None:
            return []
        fired: list[dict] = []
        for i, rule in enumerate(items, 1):
            if not isinstance(rule, dict) or rule.get("enabled") is False:
                continue
            name = str(rule.get("name") or f"auto[{i}]")
            try:
                if not self.auto_rule_matches(rule, ev):
                    continue
                actions = config.as_list(rule.get("action"))
                for action in actions:
                    await self.run_auto_action(svc, name, str(action).lower(), rule, ev)
            except Exception as exc:
                log(f"auto[{name}]: rule skipped: {type(exc).__name__}: {exc}")
                continue
            log(f"auto[{name}]: fired on {ev.get('chat')} #{ev.get('message_id')}")
            fired.append({"rule": name, "alert": bool(rule.get("alert"))})
            if rule.get("stop"):
                break
        return fired

    # ---------- watcher ----------

    async def enrich_voice(self, ev: dict, svc: TelegramService, msg, ent) -> dict:
        """A voice message or a video note — transcribe it at once with Telegram's
        own engine.

        It is free, instant and does not require downloading the file, so the text
        goes straight into the alert: on the phone you see what was said, not
        "[voice]".
        """
        if not self.rules.get("transcribe_voice", True):
            return ev
        if not (getattr(msg, "voice", None) or getattr(msg, "video_note", None)):
            return ev
        try:
            ev["transcript"] = await svc._transcribe_telegram(ent, msg)
        except Exception as exc:
            ev["transcript_error"] = f"{type(exc).__name__}: {exc}"
        return ev

    async def on_new_message(self, event, account: str = config.MAIN_ACCOUNT) -> None:
        try:
            msg = event.message
            chat = await event.get_chat()
            sender = await event.get_sender()
            chat_id = utils.get_peer_id(chat)
            username = getattr(chat, "username", None)
            if username:
                link = f"https://t.me/{username}/{msg.id}"
            elif str(chat_id).startswith("-100"):
                link = f"https://t.me/c/{str(chat_id)[4:]}/{msg.id}"
            else:
                link = None

            ev = {
                "at": datetime.now(timezone.utc).isoformat(),
                "account": account,
                "chat": entity_name(chat),
                "chat_id": chat_id,
                "chat_type": TelegramService.dialog_kind_of(chat),
                "private": bool(event.is_private),
                "from": entity_name(sender) if sender else None,
                "from_id": getattr(sender, "id", None),
                "from_bot": bool(getattr(sender, "bot", False)),
                "message_id": msg.id,
                "text": (msg.message or "")[:1000],
                "media": bool(msg.media),
                "mentioned": bool(msg.mentioned),
                "out": bool(msg.out),
                "link": link,
            }
            self.feed_waiters(ev)
            # Filters run before the alert on purpose: if a rule has already moved
            # the chat to the archive or marked it read, there is no reason to wake
            # the owner for that same message — it is handled. The alert can be
            # brought back with the `alert` flag in the rule.
            fired = await self.run_auto_rules(ev, account)
            if fired:
                ev["auto"] = [f["rule"] for f in fired]
            reason = self.alert_reason(ev)
            if reason and any(not f["alert"] for f in fired):
                log(f"auto: alert '{reason}' for '{ev.get('chat')}' dropped by a filter")
                reason = None
            if reason:
                svc = self.services.get(account)
                if svc is not None:
                    ev = await self.enrich_voice(ev, svc, msg, chat)
            self.append_event(ev)
            if not reason:
                return
            now = time.time()
            throttle_key = (account, chat_id)   # the same chat can live in two accounts
            if now - self.last_alert.get(throttle_key, 0) < self.rules.get("min_interval_sec", 3):
                return
            self.last_alert[throttle_key] = now
            if self.bot.configured:
                await self.bot.send(alerts.format_alert(ev, reason))
                self.alert_count += 1
        except Exception:
            log("watcher error:\n" + traceback.format_exc())

    # ---------- waiting for the next message ----------

    @staticmethod
    def _waiter_matches(spec: dict, ev: dict) -> bool:
        if ev.get("out") and not spec.get("include_own"):
            return False
        chat = spec.get("chat")
        if chat is not None:
            needle = str(chat).strip().lower().lstrip("@")
            hay = {str(ev.get("chat_id")), (ev.get("chat") or "").lower()}
            if needle not in hay and needle not in (ev.get("chat") or "").lower():
                return False
        sender = spec.get("from_user")
        if sender is not None:
            needle = str(sender).strip().lower().lstrip("@")
            if needle != str(ev.get("from_id")) and needle not in (ev.get("from") or "").lower():
                return False
        keyword = spec.get("keyword")
        if keyword and keyword.lower() not in (ev.get("text") or "").lower():
            return False
        if spec.get("private_only") and not ev.get("private"):
            return False
        return True

    def feed_waiters(self, ev: dict) -> None:
        """Wake up whoever was waiting for exactly this message."""
        # "unless they reply" reminders are cancelled right here as well: this is
        # the same stream of incoming messages, no reason to listen to it twice.
        self.drop_answered_reminders(ev)
        if not self.waiters:
            return
        still: list[tuple[dict, asyncio.Future]] = []
        for spec, fut in self.waiters:
            if not fut.done() and self._waiter_matches(spec, ev):
                fut.set_result(ev)
            elif not fut.done():
                still.append((spec, fut))
        self.waiters = still

    async def wait(
        self,
        chat: Any = None,
        from_user: Any = None,
        keyword: str | None = None,
        timeout: int = 120,
        private_only: bool = False,
        include_own: bool = False,
    ) -> dict:
        """Wait for the next matching incoming message without polling the log.

        The daemon itself waits — the same process that already listens to
        Telegram, so waiting costs nothing and misses nothing.
        """
        timeout = max(5, min(int(timeout), 600))
        spec = {
            "chat": chat, "from_user": from_user, "keyword": keyword,
            "private_only": bool(private_only), "include_own": bool(include_own),
        }
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiters.append((spec, fut))
        started = time.time()
        try:
            ev = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            return {"got": True, "waited_sec": round(time.time() - started, 1), "event": ev}
        except asyncio.TimeoutError:
            return {
                "got": False,
                "timeout_sec": timeout,
                "note": "no matching message arrived in that time",
            }
        finally:
            self.waiters = [(s, f) for s, f in self.waiters if f is not fut]

    # ---------- reminders ----------

    def load_reminders(self) -> None:
        """Load reminders from disk. Overdue ones are not thrown away: if the
        daemon was down when they came due, waking the owner late is still better
        than never.
        """
        if not config.REMINDERS_FILE.exists():
            return
        try:
            stored = json.loads(config.REMINDERS_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log(f"reminders: could not read {config.REMINDERS_FILE}: {exc}")
            return
        self.reminders = [r for r in stored.get("items", []) if isinstance(r, dict)]
        self._reminder_seq = int(stored.get("seq") or 0)
        if self.reminders:
            log(f"reminders: restored {len(self.reminders)}")

    def save_reminders(self) -> None:
        try:
            config.REMINDERS_FILE.write_text(
                json.dumps(
                    {"seq": self._reminder_seq, "items": self.reminders},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            config.REMINDERS_FILE.chmod(0o600)
        except OSError as exc:
            log(f"reminders: could not write {config.REMINDERS_FILE}: {exc}")

    @staticmethod
    def _reminder_answered_by(rem: dict, ev: dict) -> bool:
        """Whether this incoming message counts as the reply that cancels a reminder."""
        if not rem.get("unless_reply") or ev.get("out"):
            return False
        if ev.get("kind") == "reaction":
            return False   # a reaction is not a reply: asked in text, waiting for text
        needle = str(rem.get("chat") or "").strip().lower().lstrip("@")
        if not needle:
            return False
        for hay in (
            str(ev.get("chat_id") or ""), (ev.get("chat") or "").lower(),
            str(ev.get("from_id") or ""), (ev.get("from") or "").lower(),
        ):
            if hay and (needle == hay or needle in hay):
                return True
        return False

    def drop_answered_reminders(self, ev: dict) -> None:
        """They replied before the deadline — the reminder is no longer needed, and
        there is no reason to wake anyone."""
        if not self.reminders:
            return
        keep = [r for r in self.reminders if not self._reminder_answered_by(r, ev)]
        if len(keep) != len(self.reminders):
            gone = [r["id"] for r in self.reminders if r not in keep]
            log(f"reminders: cancelled by a reply from {ev.get('chat')}: {', '.join(gone)}")
            self.reminders = keep
            self.save_reminders()

    def _reminder_view(self, rem: dict) -> dict:
        left = (parse_when(rem["at"]) - datetime.now(timezone.utc)).total_seconds()
        return {
            "id": rem["id"],
            "text": rem["text"],
            "at": rem["at"],
            "in_min": round(left / 60, 1),
            "chat": rem.get("chat"),
            "unless_reply": bool(rem.get("unless_reply")),
            "created_at": rem.get("created_at"),
        }

    async def fire_reminder(self, rem: dict) -> None:
        why = f"due {parse_when(rem['at']).astimezone().strftime('%H:%M %d.%m')}"
        if rem.get("unless_reply"):
            why += f", and no reply from “{rem.get('chat')}” ever came"
        await self.bot.send(
            f"<b>Reminder</b>\n\n{html.escape(str(rem['text'])[:1000])}\n\n"
            f"<i>{html.escape(why)}</i>"
        )

    async def check_reminders(self) -> None:
        """One tick: send everything that has come due."""
        if not self.reminders:
            return
        now = datetime.now(timezone.utc)
        due = [r for r in self.reminders if parse_when(r["at"]) <= now]
        if not due:
            return
        # Taken off the queue before sending: a Bot API failure must not turn a
        # reminder into an eternal alarm clock repeating every half minute.
        self.reminders = [r for r in self.reminders if r not in due]
        self.save_reminders()
        for rem in due:
            try:
                await self.fire_reminder(rem)
                log(f"reminders: {rem['id']} fired")
            except Exception as exc:
                log(f"reminders: {rem['id']} not delivered: {exc}")

    async def reminder_loop(self) -> None:
        """A tick every half minute: a reminder does not need second precision."""
        while True:
            try:
                await self.check_reminders()
            except asyncio.CancelledError:
                raise
            except Exception:
                log("reminder loop error:\n" + traceback.format_exc())
            await asyncio.sleep(REMINDER_TICK_SEC)

    async def remind(
        self,
        text: str | None = None,
        when: Any = None,
        chat: Any = None,
        unless_reply: bool = False,
        list: bool = False,
        cancel: str | None = None,
    ) -> dict:
        """A deferred reminder to the owner through the bot, with an "unless they
        reply" condition.

        It lives in the daemon, not in the core: firing means a message to the bot
        plus watching the stream of incoming messages, which is exactly what the
        daemon owns. Unlike `wait`, it holds nothing and survives a restart.
        """
        if list:
            return {
                "reminders": [self._reminder_view(r) for r in
                              sorted(self.reminders, key=lambda r: r["at"])]
            }
        if cancel:
            rest = [r for r in self.reminders if r["id"] != str(cancel)]
            if len(rest) == len(self.reminders):
                known = ", ".join(r["id"] for r in self.reminders) or "none at all"
                raise ValueError(f"There is no reminder {cancel!r}. Active ones: {known}")
            self.reminders = rest
            self.save_reminders()
            return {"cancelled": True, "id": str(cancel)}
        if not text or not str(text).strip():
            raise ValueError("text is required, or list=true, or cancel=<id>")
        if when is None:
            raise ValueError("when is required: +2h, +30m or 2026-08-18T09:00")
        if not self.bot.configured:
            raise RuntimeError("the bot is not configured: tg setup + tg link-bot")
        at = parse_when(when)
        if at <= datetime.now(timezone.utc):
            raise ValueError("a reminder can only be set for the future")
        if unless_reply and not chat:
            raise ValueError("unless_reply without chat is pointless: whose reply to wait for?")
        self._reminder_seq += 1
        rem = {
            "id": f"r{self._reminder_seq}",
            "text": str(text)[:1000],
            "at": at.astimezone(timezone.utc).isoformat(),
            "chat": str(chat) if chat is not None else None,
            "unless_reply": bool(unless_reply),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.reminders.append(rem)
        self.save_reminders()
        log(f"reminders: {rem['id']} for {rem['at']}")
        return {"created": True, **self._reminder_view(rem)}

    # ---------- scheduled digest ----------

    def load_digest_state(self) -> None:
        """When the digest last went out. On disk, because a daemon restart must
        not lead to a second digest for the same slot."""
        if not config.DIGEST_FILE.exists():
            return
        try:
            stored = json.loads(config.DIGEST_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log(f"digest: could not read {config.DIGEST_FILE}: {exc}")
            return
        if isinstance(stored, dict):
            self.digest_state = stored

    def save_digest_state(self) -> None:
        try:
            config.DIGEST_FILE.write_text(
                json.dumps(self.digest_state, ensure_ascii=False, indent=2)
            )
            config.DIGEST_FILE.chmod(0o600)
        except OSError as exc:
            log(f"digest: could not write {config.DIGEST_FILE}: {exc}")

    @staticmethod
    def _digest_slots(times: list[tuple[int, int]], now: datetime) -> list[datetime]:
        """Today's and yesterday's slots, ascending. Yesterday's are needed so that
        the first slot of the day has a predecessor: the period of the evening
        digest starts in the morning."""
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        out = [
            day.replace(hour=hour, minute=minute)
            for day in (today - timedelta(days=1), today)
            for hour, minute in times
        ]
        return sorted(out)

    def _digest_period_start(self, due: datetime, slots: list[datetime]) -> datetime:
        """From which moment to count. Normally from the last actual send, otherwise
        from the previous slot; in any case no deeper than a day and a bit, so that
        a digest after a long downtime does not turn into a weekly report."""
        floor = due.astimezone(timezone.utc) - timedelta(seconds=DIGEST_MAX_PERIOD_SEC)
        earlier = [s for s in slots if s < due]
        fallback = (earlier[-1] if earlier else due - timedelta(days=1)).astimezone(timezone.utc)
        stored = self.digest_state.get("covered_since")
        start = None
        if stored:
            try:
                start = datetime.fromisoformat(stored)
            except (TypeError, ValueError):
                start = None
        if start is None:
            start = fallback
        return max(start, floor)

    def _close_digest_slot(self, key: str, covered_until: datetime | None) -> None:
        """The slot is passed. `covered_until` moves only when the period is really
        closed: a digest skipped because of a pause or quiet hours must not eat the
        messages — they go into the next one."""
        self.digest_state["last_slot"] = key
        if covered_until is not None:
            self.digest_state["covered_since"] = covered_until.isoformat()
        self.save_digest_state()

    def read_events_since(self, since_iso: str) -> list[dict]:
        """Event-log rows from a moment onwards. Read as a stream and cut by time
        right away: `events.jsonl` grows to 20 MB, and there is no reason to hold
        all of it in memory for a digest covering half a day."""
        rows: list[dict] = []
        if not config.EVENTS_LOG.exists():
            return rows
        try:
            with config.EVENTS_LOG.open() as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Both strings are isoformat in UTC, so comparing strings here
                    # is the same as comparing dates, but without parsing a million
                    # of them.
                    if str(row.get("at") or "") >= since_iso:
                        rows.append(row)
        except OSError as exc:
            log(f"digest: event log not read: {exc}")
        return rows

    @staticmethod
    def _digest_label(ev: dict) -> str:
        name = ev.get("chat") or "?"
        account = ev.get("account")
        return f"{name} · {account}" if account and account != config.MAIN_ACCOUNT else name

    def _digest_hit(self, ev: dict) -> str | None:
        """Whether the message fell under keywords or watch_chats — with the same
        matcher as the alerts. Muted chats are not highlighted: "muted" means
        "muted", though they still count towards the totals."""
        r = self.rules
        if self._chat_matches(r.get("mute_chats", []), ev["chat_id"], ev.get("chat", "")):
            return None
        text = (ev.get("text") or "").lower()
        for kw in r.get("keywords", []):
            if kw and str(kw).lower() in text:
                return f"word “{kw}”"
        if self._chat_matches(r.get("watch_chats", []), ev["chat_id"], ev.get("chat", "")):
            return "watched chat"
        return None

    async def build_digest(self, since: datetime, until: datetime) -> str | None:
        """The digest for a period, or None if there is nothing to write about.

        The source is `events.jsonl`: it holds every incoming message, not only the
        ones that woke the owner, which makes the digest more honest than any
        selection built from alerts.
        """
        rows = self.read_events_since(since.isoformat())
        msgs: list[dict] = []
        reactions = 0
        for ev in rows:
            if ev.get("out"):
                continue
            if self.self_bot_id and ev.get("from_id") == self.self_bot_id:
                continue   # our own alerts come back as incoming messages
            if ev.get("kind") == "reaction":
                reactions += 1
                continue
            if self.rules.get("ignore_bots") and ev.get("from_bot"):
                continue
            msgs.append(ev)
        if not msgs and not reactions:
            return None

        counts = Counter(self._digest_label(ev) for ev in msgs)
        highlights = [(hit, ev) for ev in msgs if (hit := self._digest_hit(ev))]
        auto_fired = sum(1 for ev in rows if ev.get("auto"))

        unread_chats = unread_total = 0
        try:
            unread = await self.tg.dialogs(limit=200, unread_only=True, archived=None)
            unread_chats = len(unread)
            unread_total = sum(int(d.get("unread") or 0) for d in unread)
        except Exception as exc:
            log(f"digest: unread not counted: {exc}")

        period = (
            f"{since.astimezone().strftime('%d.%m %H:%M')}"
            f" — {until.strftime('%d.%m %H:%M')}"
        )
        lines = [f"<b>Digest</b> · {html.escape(period)}", ""]
        lines.append(f"Chats: {len(counts)} · messages: {len(msgs)}")
        if reactions:
            lines.append(f"Reactions to your messages: {reactions}")
        if auto_fired:
            lines.append(f"Filters fired: {auto_fired}")
        if unread_chats:
            lines.append(f"Unread: {unread_total} in {unread_chats} chats")
        if counts:
            lines += ["", "<b>Most of all</b>"]
            lines += [
                f"· {html.escape(name)} — {n}"
                for name, n in counts.most_common(DIGEST_TOP_CHATS)
            ]
        if highlights:
            lines += ["", "<b>By rules</b>"]
            for hit, ev in highlights[-DIGEST_HIGHLIGHTS:]:
                who = html.escape(ev.get("from") or "?")
                head = f"· {html.escape(ev.get('chat') or '?')} · {who} <i>({html.escape(hit)})</i>"
                body = (ev.get("text") or ev.get("transcript") or "")[:120]
                lines.append(head + (f"\n  {html.escape(body)}" if body else ""))
        return "\n".join(lines)

    async def check_digest(self) -> None:
        """One tick of the digest schedule."""
        try:
            times = config.parse_digest_times(self.rules.get("digest_at"))
        except ValueError as exc:
            log(f"digest: schedule not parsed, there will be no digests: {exc}")
            return
        if not times:
            return
        now = datetime.now()
        slots = self._digest_slots(times, now)
        passed = [s for s in slots if s <= now]
        if not passed:
            return
        due = passed[-1]
        key = due.strftime("%Y-%m-%d %H:%M")
        if self.digest_state.get("last_slot") == key:
            return   # this slot is already done, including by a past life of the daemon
        if (now - due).total_seconds() > DIGEST_CATCHUP_SEC:
            self._close_digest_slot(key, None)
            log(f"digest: slot {key} skipped — too much time has passed")
            return
        if self.paused or not self.rules.get("enabled", True):
            self._close_digest_slot(key, None)
            log(f"digest: slot {key} skipped — the automation is paused")
            return
        if self.in_quiet_hours(now):
            self._close_digest_slot(key, None)
            log(f"digest: slot {key} skipped — quiet hours")
            return
        if not self.bot.configured:
            return   # nowhere to send; once the bot is set up the next slot fires
        since = self._digest_period_start(due, slots)
        body = await self.build_digest(since, due)
        if body is None:
            self._close_digest_slot(key, due.astimezone(timezone.utc))
            log(f"digest: nothing happened in the period up to {key}, no digest sent")
            return
        await self.bot.send(body)
        self.digest_state["last_sent_at"] = datetime.now(timezone.utc).isoformat()
        self._close_digest_slot(key, due.astimezone(timezone.utc))
        log(f"digest: digest for slot {key} sent")

    async def digest_loop(self) -> None:
        """A tick as frequent as the reminder one: minute precision is enough here."""
        while True:
            try:
                await self.check_digest()
            except asyncio.CancelledError:
                raise
            except Exception:
                log("digest loop error:\n" + traceback.format_exc())
            await asyncio.sleep(DIGEST_TICK_SEC)

    # ---------- asking the owner ----------

    async def ask(
        self, question: str, options: list | None = None, timeout: int = 300
    ) -> dict:
        """Ask the owner through the bot and wait for an answer.

        Needed wherever the agent has no right to decide by itself: send or not,
        delete or not, which of the options to pick. The answer comes as a button
        press or as an ordinary message to the bot.
        """
        if not self.bot.configured:
            raise RuntimeError("the bot is not configured: tg setup + tg link-bot")
        timeout = max(10, min(int(timeout), 3600))
        opts = [str(o)[:60] for o in (options or ["yes", "no"])][:6]
        self._question_seq += 1
        qid = f"q{self._question_seq}"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.questions[qid] = {"future": fut, "options": opts}

        keyboard = {
            "inline_keyboard": [
                [{"text": o, "callback_data": f"{qid}:{i}"}] for i, o in enumerate(opts)
            ]
        }
        await self.bot.call(
            "sendMessage",
            chat_id=self.bot.chat_id,
            text=f"<b>A question from the agent</b>\n\n{html.escape(str(question)[:800])}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        try:
            answer = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            return {"answered": True, **answer}
        except asyncio.TimeoutError:
            return {
                "answered": False,
                "timeout_sec": timeout,
                "note": "the owner did not answer — treat that as permission refused",
            }
        finally:
            self.questions.pop(qid, None)

    def resolve_question(self, qid: str, answer: str, how: str) -> bool:
        item = self.questions.get(qid)
        if not item or item["future"].done():
            return False
        item["future"].set_result({"answer": answer, "how": how})
        return True

    def answer_pending_question(self, text: str) -> bool:
        """Plain text to the bot counts as an answer to the most recent question."""
        for qid in reversed(list(self.questions)):
            if self.resolve_question(qid, text, "text"):
                return True
        return False

    # ---------- write confirmation ----------

    def confirm_mode(self, settings: dict | None = None) -> str:
        """The confirmation mode, reduced to off/outgoing/all.

        Read from disk on every call: it is the owner's own restriction, and an
        edit of rules.json must take effect at once, without waiting for a daemon
        restart. A value we do not understand is no reason to write silently: we
        treat it as an error.
        """
        settings = settings if settings is not None else config.load_confirm()
        raw = str(settings.get("confirm_writes") or "off").strip().lower()
        if raw in ("", "off", "0", "false", "no", "none"):
            return "off"
        if raw in ("all", "1", "true", "yes", "on", "every"):
            return "all"
        if raw in ("outgoing", "out", "external", "send"):
            return "outgoing"
        raise GuardError(
            f"confirm_writes={raw!r} is not a mode I know, so writing is forbidden. "
            "Allowed: off, outgoing, all (data/rules.json)."
        )

    async def _confirm_target(self, svc: TelegramService, params: dict) -> dict:
        """Where exactly the action will go: the raw argument, the id and a human
        name.

        Different methods name the chat differently (chat, to_chat, user, peer),
        and for forward it is the recipient that matters, not the source — which is
        why to_chat comes first.
        """
        raw = None
        for key in ("to_chat", "chat", "peer", "user", "bot", "from_chat"):
            if params.get(key) is not None:
                raw = params[key]
                break
        where: dict[str, Any] = {"raw": raw, "id": None, "name": None, "saved": False}
        if raw is None:
            return where
        try:
            ent = await svc.resolve(raw)
        except Exception:
            return where   # ambiguity and typos are the method's own business
        if ent == "me":
            return {**where, "saved": True, "name": "Saved Messages"}
        try:
            obj = await svc.client.get_entity(ent)
            where.update(id=utils.get_peer_id(obj), name=entity_name(obj))
        except Exception:
            pass
        return where

    def _confirm_whitelisted(self, where: dict, whitelist: Any) -> bool:
        for p in whitelist or []:
            s = str(p).strip().lower()
            if not s:
                continue
            if where["saved"] and s in SAVED_ALIASES:
                return True
            if where["id"] is not None and s == str(where["id"]):
                return True
            if where["name"] and s in where["name"].lower():
                return True
            if where["raw"] is not None and s == str(where["raw"]).strip().lower():
                return True
        return False

    def _confirm_question(self, method: str, account: str, where: dict, params: dict) -> str:
        """The question to the owner: what exactly will go out, not just "allow a
        write?"."""
        lines = [f"The agent wants: {method}"]
        if account != config.MAIN_ACCOUNT:
            lines.append(f"account: {account}")
        target = where["name"] or (str(where["raw"]) if where["raw"] is not None else None)
        if target:
            suffix = f" (id {where['id']})" if where["id"] is not None else ""
            lines.append(f"chat: {target[:80]}{suffix}")
        for key in ("text", "caption", "question", "title"):
            body = params.get(key)
            if isinstance(body, str) and body.strip():
                cut = body[:CONFIRM_PREVIEW_LEN]
                lines.append(f"{key}: {cut}{'…' if len(body) > len(cut) else ''}")
                break
        rest = {
            k: v for k, v in params.items()
            if k not in ("to_chat", "chat", "peer", "user", "bot", "from_chat",
                         "text", "caption", "question", "title")
        }
        if rest:
            lines.append("more: " + json.dumps(rest, ensure_ascii=False, default=str)[:200])
        return "\n".join(lines)

    async def confirm_write(self, svc: TelegramService, method: str, params: dict) -> None:
        """Ask the owner before a writing call; a refusal is an error of that call.

        The wrapper lives here and not in the core: the core knows nothing about
        the bot and must not, and asking is only possible through the bot.
        """
        settings = config.load_confirm()
        mode = self.confirm_mode(settings)
        if mode == "off" or method in CONFIRM_EXEMPT:
            return
        flag = CONFIRM_CONDITIONAL.get(method)
        if flag is not None and not params.get(flag):
            return   # this call of that method is a read, there is nothing to ask about
        if mode == "outgoing" and method not in CONFIRM_OUTBOUND:
            return
        where = await self._confirm_target(svc, params)
        if self._confirm_whitelisted(where, settings.get("confirm_whitelist")):
            return
        # The whitelist is checked before the bot on purpose: a chat we were never
        # going to ask about must not run into an unconfigured channel.
        if not self.bot.configured:
            raise GuardError(
                "confirm_writes is on, but the bot is not configured "
                "(TG_BOT_TOKEN/TG_ALERT_CHAT_ID are empty): there is nobody to ask "
                "for permission, so writing is forbidden. Set the bot up "
                "(tg setup, tg link-bot) or switch confirm_writes off."
            )
        # The 110-second cap is not accidental: the MCP client waits for the daemon
        # no longer than 120, and permission granted later would lead to a send the
        # agent has already been told was a "network error".
        timeout = max(10, min(int(settings.get("confirm_timeout_sec") or 90), 110))
        reply = await self.ask(
            self._confirm_question(method, svc.account, where, params),
            options=["allow", "deny"],
            timeout=timeout,
        )
        if not reply.get("answered"):
            raise GuardError(
                f"the owner did not confirm: no answer within {timeout} s. "
                "Silence counts as a refusal, the action was not carried out."
            )
        answer = str(reply.get("answer") or "").strip().lower()
        if answer not in CONFIRM_YES:
            raise GuardError(
                f"the owner did not confirm: answered {answer!r}. "
                "The action was not carried out."
            )

    async def on_reaction(self, update, account: str = config.MAIN_ACCOUNT) -> None:
        """Someone added or removed a reaction.

        It comes as a separate raw update: a reaction is not a new message, so the
        ordinary watcher does not see it. Only reactions to our own messages are
        interesting — for other people's, Telegram sends them in batches in every
        chat.

        Our own reaction never gets here: Telegram sends no update for actions of
        this same session, just as it sends none for our own sent messages.
        """
        svc = self.services.get(account)
        if svc is None:
            return
        try:
            ent = await svc.client.get_entity(update.peer)
            msg = await svc.client.get_messages(ent, ids=update.msg_id)
            if msg is None or not msg.out:
                return
            recent = getattr(update.reactions, "recent_reactions", None) or []
            who, emoji = None, None
            for r in recent:
                if getattr(r, "my", False):
                    continue   # we do not show our own reaction back to ourselves
                emoji = core_reaction_of(r.reaction)
                try:
                    who = entity_name(await svc.client.get_entity(r.peer_id))
                except Exception:
                    who = None
                break
            counts = [
                {"emoji": core_reaction_of(i.reaction), "count": i.count}
                for i in getattr(update.reactions, "results", [])
            ]
            if not counts:
                return   # the reaction was removed, not added
            chat_id = utils.get_peer_id(ent)
            ev = {
                "at": datetime.now(timezone.utc).isoformat(),
                "account": account,
                "kind": "reaction",
                "chat": entity_name(ent),
                "chat_id": chat_id,
                "private": isinstance(ent, types.User),
                "from": who,
                "message_id": update.msg_id,
                "text": (msg.message or "")[:200],
                "emoji": emoji,
                "reactions": counts,
                "link": svc.message_link(msg, ent),
            }
            log(f"reaction: {ev['chat']} #{update.msg_id} {emoji} from {who or '?'}")
            self.feed_waiters(ev)
            self.append_event(ev)
            if (
                self.rules.get("alert_on_reaction")
                and not self.paused
                and self.bot.configured
            ):
                await self.bot.send(alerts.format_reaction(ev))
                self.alert_count += 1
        except Exception:
            log("reaction watcher error:\n" + traceback.format_exc())

    async def on_own_message(self, event, account: str = config.MAIN_ACCOUNT) -> None:
        """Our own sent message: only for tg_wait(include_own=true)."""
        if not self.waiters:
            return
        try:
            msg = event.message
            chat = await event.get_chat()
            self.feed_waiters(
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "account": account,
                    "chat": entity_name(chat),
                    "chat_id": utils.get_peer_id(chat),
                    "private": bool(event.is_private),
                    "from": "you",
                    "message_id": msg.id,
                    "text": (msg.message or "")[:1000],
                    "media": bool(msg.media),
                    "out": True,
                }
            )
        except Exception:
            log("own-message waiter error:\n" + traceback.format_exc())

    # ---------- bot command channel ----------

    async def bot_loop(self) -> None:
        if not self.bot.token:
            log("bot: no token configured, command channel disabled")
            return
        log("bot: command channel started")
        while True:
            try:
                for upd in await self.bot.poll():
                    cb = upd.get("callback_query")
                    if cb:
                        await self.handle_bot_callback(cb)
                        continue
                    msg = upd.get("message") or {}
                    chat_id = str(msg.get("chat", {}).get("id"))
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue
                    if self.bot.chat_id and chat_id != str(self.bot.chat_id):
                        log(f"bot: ignoring message from unknown chat {chat_id}")
                        continue
                    # An answer to a question the agent asked outranks commands: the
                    # owner writes plain text, not a /command.
                    if not text.startswith("/") and self.answer_pending_question(text):
                        continue
                    await self.handle_bot_command(text, chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log(f"bot loop error: {exc}")
                await asyncio.sleep(5)

    async def handle_bot_callback(self, cb: dict) -> None:
        """A button press under a question from the agent."""
        data = str(cb.get("data") or "")
        chat_id = str((cb.get("message") or {}).get("chat", {}).get("id"))
        if self.bot.chat_id and chat_id != str(self.bot.chat_id):
            return
        qid, _, idx = data.partition(":")
        item = self.questions.get(qid)
        answer = None
        if item and idx.isdigit() and int(idx) < len(item["options"]):
            answer = item["options"][int(idx)]
            self.resolve_question(qid, answer, "button")
        try:
            await self.bot.call(
                "answerCallbackQuery",
                callback_query_id=cb.get("id"),
                text=f"Taken: {answer}" if answer else "This question is no longer current",
            )
        except Exception as exc:
            log(f"bot: answerCallbackQuery failed: {exc}")

    async def handle_bot_command(self, text: str, chat_id: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().lstrip("/").split("@")[0]
        arg = arg.strip()
        try:
            if cmd in ("start", "help"):
                await self.bot.send(HELP_TEXT, chat_id)
            elif cmd == "status":
                st = await self.status()
                await self.bot.send(
                    f"<b>Status</b>\nAccount: {st['account']['name']}\n"
                    f"Uptime: {st['uptime_min']} min\nAlerts: {st['alerts_sent']}\n"
                    f"Rules: {'paused' if self.paused else 'active'}",
                    chat_id,
                )
            elif cmd == "unread":
                rows = await self.tg.unread_summary(limit_chats=10, per_chat=2)
                if not rows:
                    await self.bot.send("Nothing unread.", chat_id)
                else:
                    parts = [
                        f"<b>{r['chat']}</b> ({r['unread']})\n"
                        + "\n".join("· " + (m.get("text") or "")[:120] for m in r["messages"])
                        for r in rows
                    ]
                    await self.bot.send("\n\n".join(parts), chat_id)
            elif cmd == "actions":
                rows = await self.get_actions(limit=10)
                if not rows:
                    await self.bot.send("The agent did nothing.", chat_id)
                else:
                    lines = []
                    for r in reversed(rows):   # newest first: this is a digest, not a log
                        when = parse_when(r["at"]).astimezone().strftime("%d.%m %H:%M")
                        target = (r.get("params") or {}).get("chat")
                        head = f"{when} · <b>{html.escape(r.get('action') or '?')}</b>"
                        if target:
                            head += f" · {html.escape(str(target)[:40])}"
                        if not r.get("ok"):
                            head += f" · error: {html.escape(str(r.get('error'))[:80])}"
                        body = (r.get("text") or "")[:100]
                        lines.append(head + (f"\n{html.escape(body)}" if body else ""))
                    await self.bot.send("<b>Recent actions</b>\n\n" + "\n".join(lines), chat_id)
            elif cmd == "pause":
                self.paused = True
                await self.bot.send("Alerts are paused. /resume to bring them back.", chat_id)
            elif cmd == "resume":
                self.paused = False
                await self.bot.send("Alerts are active again.", chat_id)
            elif cmd == "rules":
                await self.bot.send(
                    "<pre>" + json.dumps(self.rules_view(), ensure_ascii=False, indent=2) + "</pre>",
                    chat_id,
                )
            elif cmd == "mute" and arg:
                self.rules.setdefault("mute_chats", []).append(arg)
                config.save_rules(self.rules)
                await self.bot.send(f"No more alerts about: {arg}", chat_id)
            elif cmd == "watch" and arg:
                self.rules.setdefault("watch_chats", []).append(arg)
                config.save_rules(self.rules)
                await self.bot.send(f"Watching: {arg}", chat_id)
            else:
                await self.bot.send(HELP_TEXT, chat_id)
        except Exception as exc:
            await self.bot.send(f"Error: {exc}", chat_id)

    # ---------- accounts ----------

    def service(self, account: str | None = None) -> TelegramService:
        """The service of the requested account; without a label — the main one."""
        if not self.services:
            raise RuntimeError("No account is signed in: `uv run tg login`")
        if account is None:
            return self.tg
        label = config.normalize_account(account)
        svc = self.services.get(label)
        if svc is None:
            raise ValueError(
                f"Account {label!r} is not signed in. Available: {', '.join(self.services)}. "
                f"To add: uv run tg login --account {label}"
            )
        return svc

    async def accounts(self) -> dict:
        return {
            "default": self.tg.account if self.tg else None,
            "accounts": [svc.whoami_dict() for svc in self.services.values()],
        }

    # ---------- rpc surface ----------

    async def status(self) -> dict:
        return {
            "account": self.tg.whoami_dict(),
            "accounts": [svc.whoami_dict() for svc in self.services.values()],
            "uptime_min": round((time.time() - self.started_at) / 60, 1),
            "alerts_sent": self.alert_count,
            "paused": self.paused,
            "bot_configured": self.bot.configured,
            "write_allowed": config.allow_write(),
            "rules": self.rules_view(),
            "digest": self.digest_state or None,
            "pid": os.getpid(),
        }

    async def get_events(self, limit: int = 50, since: str | None = None) -> list[dict]:
        if not config.EVENTS_LOG.exists():
            return []
        rows = []
        with config.EVENTS_LOG.open() as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if since:
            rows = [r for r in rows if r.get("at", "") >= since]
        return rows[-limit:]

    async def get_actions(
        self,
        limit: int = 50,
        since: Any = None,
        method: str | None = None,
        chat: Any = None,
    ) -> list[dict]:
        """What the agent did: the log of writing calls, newest at the end.

        This is the other half of `events`: there you see what happened in Telegram,
        here what the agent did in it. Needed for reporting to the owner ("what did
        you send") and for a post-mortem after a failure: failed calls are recorded
        too, with the text of the error.
        """
        if not config.ACTIONS_LOG.exists():
            return []
        # "today" is understood here the same way as in activity and export: the
        # owner has one word for "since the start of the day", and this log must not
        # be the single exception.
        start = None
        if since:
            start = day_start() if str(since).lower() in ("today", "сегодня") else parse_when(since)
        needle = str(chat).strip().lower().lstrip("@") if chat is not None else None
        rows: list[dict] = []
        with config.ACTIONS_LOG.open() as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if start is not None:
                    try:
                        if datetime.fromisoformat(row.get("at", "")) < start:
                            continue
                    except ValueError:
                        continue
                if method and row.get("action") != method:
                    continue
                if needle is not None:
                    target = str((row.get("params") or {}).get("chat") or "").lower()
                    if needle not in target:
                        continue
                rows.append(row)
        return rows[-limit:]

    def rules_view(self) -> dict:
        """The rules as shown: confirm_* always from disk, not from the daemon's
        memory.

        Otherwise, after the file was edited by hand, status would keep showing the
        old "off" even though the mode is already in force.
        """
        return {**self.rules, **config.load_confirm()}

    async def set_rules(self, patch: dict) -> dict:
        # The write-confirmation mode is not edited through this method: the agent
        # must not be able to lift a restriction off itself — for the same reason
        # the limits are not configured over MCP. The refusal is explicit rather
        # than silent, so that the agent does not think it switched the mode off.
        blocked = sorted(k for k in patch if k.startswith("confirm_"))
        if blocked:
            raise ValueError(
                "tg_rules cannot change " + ", ".join(blocked)
                + ": the write-confirmation mode is edited by hand only, "
                + "in data/rules.json."
            )
        self.rules = config.save_rules({**self.rules, **patch})
        # The rules have been rewritten — the memory of which chat-level actions are
        # already done is no longer relevant: the rule may have become a different one.
        self._auto_done.clear()
        return self.rules

    async def send_alert(self, text: str) -> dict:
        await self.bot.send(text)
        self.alert_count += 1
        return {"alerted": True}

    def dispatch_table(self, svc: TelegramService | None = None) -> dict[str, Any]:
        t = svc or self.tg
        return {
            "status": self.status,
            "accounts": self.accounts,
            "whoami": lambda: asyncio.sleep(0, result=t.whoami_dict()),
            "dialogs": t.dialogs,
            "folders": t.folders,
            "structure": t.structure,
            "unread": t.unread_summary,
            "pending": t.pending,
            "history": t.history,
            "history_batch": t.history_batch,
            "media": t.media,
            "download_many": t.download_many,
            "search": t.search,
            "mentions": t.mentions,
            "chat_info": t.chat_info,
            "participants": t.participants,
            "contacts": t.contacts,
            "download": t.download,
            "view": t.view,
            "transcribe": t.transcribe,
            "translate": t.translate,
            "stickers": t.stickers,
            "stories": t.stories,
            "sessions": t.sessions,
            "summarize": t.summarize,
            "saved_tags": t.saved_tags,
            "wait": self.wait,
            "ask": self.ask,
            "topics": t.topics,
            "admin_log": t.admin_log,
            "invites": t.invites,
            "bot_info": t.bot_info,
            "cache_clear": t.cache_clear,
            "send_sticker": t.send_sticker,
            "topic_create": t.topic_create,
            "topic_edit": t.topic_edit,
            "bot_edit": t.bot_edit,
            "message": t.message,
            "common_chats": t.common_chats,
            "person": t.person,
            "resolve_link": t.resolve_link,
            "drafts": t.drafts,
            "scheduled": t.scheduled,
            "export": t.export,
            "index": t.index,
            "activity": t.activity,
            "click": t.click,
            "send": t.send,
            "send_file": t.send_file,
            "edit": t.edit,
            "delete": t.delete,
            "forward": t.forward,
            "mark_read": t.mark_read,
            "mute": t.mute,
            "archive": t.archive,
            "pin": t.pin_dialog,
            "pin_message": t.pin_message,
            "draft": t.draft,
            "schedule": t.schedule,
            "react": t.react,
            "poll": t.poll,
            "send_location": t.send_location,
            "block": t.block,
            "contact_edit": t.contact_edit,
            "create_group": t.create_group,
            "invite": t.invite,
            "moderate": t.moderate,
            "chat_edit": t.chat_edit,
            "leave": t.leave,
            "folder_edit": t.folder_edit,
            "notify": t.notify,
            "events": self.get_events,
            "actions": self.get_actions,
            "remind": self.remind,
            "rules": self.set_rules,
            "alert": self.send_alert,
        }

    # ---------- http over unix socket ----------

    async def handle_call(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid JSON"}, status=400)
        method = payload.get("method")
        params = dict(payload.get("params") or {})
        account = payload.get("account") or params.pop("account", None)
        try:
            svc = self.service(account)
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        fn = self.dispatch_table(svc).get(method)
        if fn is None:
            return web.json_response({"ok": False, "error": f"unknown method {method}"}, status=404)
        try:
            if method in WRITE_METHODS:
                # We ask before the call, not inside the core: a refusal must stay an
                # error of the call and land in actions.jsonl by the same path as a
                # call that ran into a limit.
                await self.confirm_write(svc, method, params)
            result = await fn(**params)
            self.append_action(method, params, result, None, svc.account)
            return web.json_response({"ok": True, "result": result})
        except (GuardError, ValueError) as exc:
            self.append_action(method, params, None, str(exc), svc.account)
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        except Exception as exc:
            log(f"call {method} failed:\n{traceback.format_exc()}")
            self.append_action(method, params, None, str(exc), svc.account)
            return web.json_response({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status=500)

    async def run(self) -> None:
        config.ensure_dirs()
        self.load_reminders()
        self.load_digest_state()
        labels = config.list_accounts()
        if not labels:
            raise RuntimeError("There is no session at all. Sign in: uv run tg login")
        for label in labels:
            svc = TelegramService(label)
            me = await svc.start()
            self.services[label] = svc
            if self.tg is None or label == config.MAIN_ACCOUNT:
                self.tg = svc
            log(f"telegram[{label}]: signed in as {me['name']} (id {me['id']})")
            svc.client.add_event_handler(
                lambda event, _label=label: self.on_new_message(event, _label),
                events.NewMessage(incoming=True),
            )
            # Outgoing messages go neither to the log nor to the alerts, but tg_wait
            # can wait for them too ("wait until the owner answers by hand"), so we
            # only wake the waiters. It fires on a message from the owner's phone,
            # but not on a message sent by this same session: Telegram answers our
            # own send with a short update that never becomes an event.
            svc.client.add_event_handler(
                lambda event, _label=label: self.on_own_message(event, _label),
                events.NewMessage(outgoing=True, incoming=False),
            )
            # A reaction is not a message, it does not arrive as an ordinary event.
            # An events.Raw handler receives the update itself, not a wrapping event:
            # expecting event.original_update here means silently catching an
            # AttributeError inside Telethon and seeing no reaction at all.
            svc.client.add_event_handler(
                lambda update, _label=label: self.on_reaction(update, _label),
                events.Raw(types.UpdateMessageReactions),
            )

        app = web.Application()
        app.router.add_post("/call", self.handle_call)
        runner = web.AppRunner(app)
        await runner.setup()
        if config.SOCKET.exists():
            config.SOCKET.unlink()
        site = web.UnixSite(runner, str(config.SOCKET))
        await site.start()
        os.chmod(config.SOCKET, 0o600)
        config.PID_FILE.write_text(str(os.getpid()))
        log(f"rpc: listening on {config.SOCKET}")

        bot_task = asyncio.create_task(self.bot_loop())
        reminder_task = asyncio.create_task(self.reminder_loop())
        digest_task = asyncio.create_task(self.digest_loop())
        if self.bot.configured:
            try:
                await self.bot.send("The agent is connected to Telegram. /help — what I can do.")
            except Exception as exc:
                log(f"bot: startup ping failed: {exc}")

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)

        try:
            await stop.wait()
        finally:
            log("shutting down")
            bot_task.cancel()
            reminder_task.cancel()
            digest_task.cancel()
            await self.bot.close()
            await runner.cleanup()
            for svc in self.services.values():
                await svc.stop()
            config.SOCKET.unlink(missing_ok=True)
            config.PID_FILE.unlink(missing_ok=True)


WRITE_METHODS = {
    "send", "send_file", "send_location", "edit", "delete", "forward",
    "mark_read", "mute", "archive", "pin", "alert", "rules",
    # added together with the extended set: everything that changes the state of the
    # account has to land in the audit, otherwise the log loses its point
    "draft", "schedule", "scheduled", "react", "poll", "pin_message", "click",
    "block", "contact_edit", "create_group", "invite", "moderate", "chat_edit",
    "leave", "folder_edit", "send_sticker", "topic_create", "topic_edit", "bot_edit",
    # stories can be marked as seen, a session can be revoked: writes as well
    "stories", "sessions", "ask", "notify",
    # a reminder does not touch the account, but it survives a restart and wakes the
    # owner by itself — creating and cancelling one silently is not allowed
    "remind",
}

# Into the log only, but not under confirmation. Indexing does not change a single
# bit in the account itself and shows nothing to anyone — there is nothing to ask
# permission for. But it puts the conversation on disk in a parseable form, and
# `drop` sweeps it back off, and the owner must see both events in `actions.jsonl`.
AUDIT_ONLY = {"index"}

# ---------- write confirmation mode ----------

# What other people see, or what cannot be rolled back. In "outgoing" mode the
# question is asked only about these; mark_read, mute, archive, pin, draft,
# folder_edit and notify are silent, reversible and live inside their own account,
# so they pass through.
CONFIRM_OUTBOUND = {
    "send", "send_file", "send_location", "send_sticker", "poll", "schedule",
    "forward", "edit", "delete", "react", "click", "pin_message",
    "block", "invite", "moderate", "create_group", "chat_edit", "leave",
    "topic_create", "topic_edit", "bot_edit", "contact_edit",
    "sessions", "stories", "scheduled",
}

# There is nobody to ask, or asking makes no sense: both methods are a conversation
# with the owner themselves in their own bot, and confirming a question with a
# question would loop.
CONFIRM_EXEMPT = {"ask", "alert"}

# Methods that count as writing only because of one argument: without it the call
# is a read (stories are viewed more often than they are marked as seen).
CONFIRM_CONDITIONAL = {
    "stories": "mark_read",
    "scheduled": "cancel_ids",
    "sessions": "terminate",
}

CONFIRM_PREVIEW_LEN = 350
CONFIRM_YES = {"allow", "yes", "y", "ok", "okay", "+", "go", "go ahead", "do it"}
SAVED_ALIASES = {"me", "self", "saved", "saved messages", "избранное"}

HELP_TEXT = (
    "<b>Telegram agent</b>\n"
    "/status — state\n"
    "/unread — unread\n"
    "/actions — what the agent did\n"
    "/rules — current alert rules\n"
    "/watch &lt;chat&gt; — alert on every message of a chat\n"
    "/mute &lt;chat&gt; — do not alert about a chat\n"
    "/pause, /resume — switch alerts off/on"
)


def main() -> None:
    try:
        asyncio.run(Daemon().run())
    except KeyboardInterrupt:
        pass
    except Exception:
        log("fatal:\n" + traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
