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
from datetime import datetime, timezone
from typing import Any

from aiohttp import web
from telethon import events, utils

from . import alerts, config
from .core import GuardError, TelegramService, entity_name

MAX_EVENT_LOG_BYTES = 20 * 1024 * 1024


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
        account: str = config.MAIN_ACCOUNT,
    ) -> None:
        if method not in WRITE_METHODS:
            return
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "account": account,
            "action": method,
            "params": {k: v for k, v in params.items() if k != "text"},
            "text": (params.get("text") or "")[:400] or None,
            "ok": error is None,
            "error": error,
        }
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

        qh = r.get("quiet_hours")
        if qh and len(qh) == 2:
            hour = datetime.now().hour
            start, end = int(qh[0]), int(qh[1])
            quiet = start <= hour or hour < end if start > end else start <= hour < end
            if quiet:
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
            reason = self.alert_reason(ev)
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
            elif cmd == "pause":
                self.paused = True
                await self.bot.send("Alerts are paused. /resume to bring them back.", chat_id)
            elif cmd == "resume":
                self.paused = False
                await self.bot.send("Alerts are active again.", chat_id)
            elif cmd == "rules":
                await self.bot.send(
                    "<pre>" + json.dumps(self.rules, ensure_ascii=False, indent=2) + "</pre>",
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
            "rules": self.rules,
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

    async def set_rules(self, patch: dict) -> dict:
        self.rules = config.save_rules({**self.rules, **patch})
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
            "bot_info": t.bot_info,
            "cache_clear": t.cache_clear,
            "send_sticker": t.send_sticker,
            "topic_create": t.topic_create,
            "topic_edit": t.topic_edit,
            "bot_edit": t.bot_edit,
            "message": t.message,
            "common_chats": t.common_chats,
            "resolve_link": t.resolve_link,
            "drafts": t.drafts,
            "scheduled": t.scheduled,
            "export": t.export,
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
}

HELP_TEXT = (
    "<b>Telegram agent</b>\n"
    "/status — state\n"
    "/unread — unread\n"
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
