#!/usr/bin/env python3
"""Live check of the reading tools against a running daemon.

Run: uv run python scripts/smoke.py
Writes nothing to Telegram: reads only. For each method prints ok, FAIL or the
reason it was skipped.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from collections.abc import Awaitable, Callable

import aiohttp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tgagent import config

LABEL_WIDTH = 22
BRIEF_LEN = 110
CALL_TIMEOUT_SEC = 180

# The chat every read runs against. Saved Messages is a deliberate choice: every
# account has it, and no read from there is visible to anyone else.
PROBE = "me"

# Method, params and — optionally — a suffix for the name. The suffix tells apart
# calls of the same method with different params, once kind alone no longer
# distinguishes them.
CASES: list[tuple] = [
    ("status", {}),
    ("accounts", {}),
    ("structure", {}),
    ("folders", {}),
    ("dialogs", {"limit": 5}),
    ("unread", {}),
    ("pending", {"limit": 5}),
    ("pending", {"direction": "mine", "min_age_hours": 24, "limit": 5}, "mine"),
    ("pending", {"direction": "both", "kind": "user", "limit": 5}, "both/user"),
    ("history", {"chat": PROBE, "limit": 3}),
    ("history_batch", {"chats": [PROBE], "limit": 2}),
    ("search", {"query": "http", "limit": 3}),
    ("mentions", {"limit": 3}),
    ("chat_info", {"chat": PROBE}),
    ("chat_info", {"chat": PROBE, "counters": False}, "no counters"),
    ("person", {"user": "me", "messages": 5, "chats": 5}),
    ("contacts", {"limit": 5}),
    ("drafts", {}),
    ("scheduled", {"chat": PROBE}),
    ("events", {"limit": 5}),
    ("stickers", {"scope": "sets"}),
    ("resolve_link", {"link": "https://t.me/telegram"}),
    ("media", {"chat": PROBE, "kind": "photo", "limit": 3}),
    ("media", {"chat": PROBE, "kind": "link", "limit": 3}),
    ("media", {"chat": PROBE, "kind": "voice", "limit": 3}),
    ("activity", {"since": "today", "limit_chats": 5}),
    ("activity", {"chat": PROBE, "limit_days": 7}, "by chat"),
    ("actions", {"limit": 5}),
    ("actions", {"since": "today", "limit": 5}, "for today"),
    ("remind", {"list": True}, "list"),
    ("index", {"action": "status"}, "status"),
    # read only: update costs money and sends the correspondence outside
    ("memory", {"action": "list"}, "list"),
    ("saved_tags", {}),
    ("stories", {}),
    ("sessions", {}),
    ("limits", {}),
    ("capabilities", {}),
    # With a chat one extra request appears that is absent otherwise — the rights
    # in that particular chat. Checked on Saved Messages: nobody asks for them there.
    ("capabilities", {"chat": PROBE}, "by chat"),
    ("contacts", {"kind": "top", "limit": 5}),
    ("contacts", {"kind": "birthdays", "limit": 5}),
    ("contacts", {"kind": "online", "limit": 5}),
    ("contacts", {"kind": "blocked", "limit": 3}),
    ("dialogs", {"kind": "inactive", "limit": 3}),
    ("mentions", {"kind": "reactions", "limit": 3}),
    ("search", {"chat": PROBE, "kind": "file", "limit": 3}),
    ("wait", {"keyword": "nothing-of-the-sort-will-arrive", "timeout": 5}),
]


async def call(session: aiohttp.ClientSession, method: str, **params):
    async with session.post(
        "http://localhost/call", json={"method": method, "params": params}
    ) as r:
        data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error"))
    return data["result"]


def brief(value, limit: int = BRIEF_LEN) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


class Report:
    """Printing and failure count. A skip does not count as a failure."""

    def __init__(self) -> None:
        self.failures = 0

    def ok(self, label: str, note: str) -> None:
        print(f"ok    {label:{LABEL_WIDTH}} {note}")

    def skip(self, label: str, why: str) -> None:
        print(f"skip  {label}: {why}")

    def fail(self, label: str, exc: object) -> None:
        self.failures += 1
        print(f"FAIL  {label:{LABEL_WIDTH}} {exc}")

    async def probe(self, label: str, fn: Callable[[], Awaitable[str | None]]) -> None:
        """A check that needs data the account may not have: a chat where I am an
        admin, a filled index, a voice message in the recent chats.

        Missing data is a skip, not a failure, so `fn` returns either a string with
        the result, or None: "there was nothing to check on".
        """
        try:
            note = await fn()
        except Exception as exc:
            self.fail(label, exc)
            return
        if note is None:
            return
        self.ok(label, note)


async def run_cases(s: aiohttp.ClientSession, report: Report) -> None:
    for case in CASES:
        method, params = case[0], case[1]
        note = case[2] if len(case) > 2 else params.get("kind")
        label = method + (f"({note})" if note else "")
        try:
            res = await call(s, method, **params)
        except Exception as exc:
            report.fail(label, exc)
            continue
        report.ok(label, brief(res))


async def run_optional(s: aiohttp.ClientSession, report: Report, dialogs: list, me: dict) -> None:
    async def invites() -> str | None:
        # Invite links are given out only by a chat where I am an admin.
        for d in dialogs:
            if d.get("type") not in ("group", "channel"):
                continue
            try:
                res = await call(s, "invites", chat=d["id"], limit=3)
            except Exception:
                continue
            return f"links: {res.get('total')}, admins: {len(res.get('admins') or [])}"
        report.skip("invites", "no chat where I have admin rights")
        return None

    async def similar() -> str | None:
        # Telegram returns similar channels only for channels.
        channel = next((d for d in dialogs if d.get("type") == "channel"), None)
        if channel is None:
            report.skip("chat_info(similar)", "no channels in the dialog list")
            return None
        res = await call(s, "chat_info", chat=channel["id"], similar=True)
        sim = res.get("similar") or {}
        return sim.get("error") or f"similar channels: {len(sim.get('items') or [])}"

    async def local_search() -> str | None:
        # Without a filled index there is nothing to search through.
        status = await call(s, "index", action="status")
        if not (status.get("chats") or []):
            report.skip("search(local)", "the local index is empty")
            return None
        res = await call(s, "search", query="a", engine="local", limit=3)
        found = f"found {res.get('total')} in {res.get('indexed_chats')} chats"
        res = await call(s, "search", query="a", engine="local", author="me", limit=3)
        return f"{found}; by author: {res.get('total')}"

    async def saved_from() -> str | None:
        # An empty answer is fine here: the path itself is what is checked, not the
        # presence of something forwarded from a particular chat.
        source = next((d for d in dialogs if d.get("id") != me["account"]["id"]), None)
        if source is None:
            report.skip("history(saved_from)", "there are no dialogs besides Saved Messages")
            return None
        res = await call(s, "history", chat="me", saved_from=source["id"], limit=3)
        return f"messages: {res.get('total')}"

    async def view() -> str | None:
        photos = await call(s, "media", chat=PROBE, kind="photo", limit=1)
        items = photos.get("items") or []
        if not items:
            report.skip("view", "no photos in Saved Messages")
            return None
        info = await call(
            s, "view", chat=PROBE, message_id=items[0]["message_id"], size="preview"
        )
        return f"{info.get('dimensions')} {info.get('bytes')} bytes"

    async def transcribe() -> str | None:
        # We look for a voice message across the last few chats: Saved Messages may
        # not have one.
        found = None
        for d in ([{"name": PROBE}] + dialogs)[:12]:
            chat = d.get("id") or d.get("name")
            for kind in ("voice", "round"):
                try:
                    res = await call(s, "media", chat=chat, kind=kind, limit=1)
                except Exception:
                    continue
                if res.get("items"):
                    found = (chat, res["items"][0]["message_id"])
                    break
            if found:
                break
        if found is None:
            report.skip("transcribe", "no voice messages found in the recent chats")
            return None
        res = await call(s, "transcribe", chat=found[0], message_ids=[found[1]])
        first = (res.get("items") or [{}])[0]
        return f"engine={first.get('engine')} {brief(first.get('text'), 60)}"

    async def summarize() -> str | None:
        # A summary — if a long post turns up in the recent chats.
        for d in dialogs[:15]:
            hist = await call(s, "history", chat=d.get("id") or d.get("name"), limit=15)
            long = [m for m in hist["messages"] if len(m.get("text") or "") > 800]
            if not long:
                continue
            res = await call(s, "summarize", chat=d["id"], message_ids=[long[0]["id"]])
            item = (res.get("items") or [{}])[0]
            got = item.get("summary") or item.get("error")
            return f"{len(long[0]['text'])} -> {len(got or '')} chars"
        report.skip("summarize", "no long posts in the recent chats")
        return None

    for label, fn in (
        ("invites", invites),
        ("chat_info(similar)", similar),
        ("search(local)", local_search),
        ("history(saved_from)", saved_from),
        ("view", view),
        ("transcribe", transcribe),
        ("summarize", summarize),
    ):
        await report.probe(label, fn)


async def main() -> int:
    if not config.SOCKET.exists():
        print("daemon is not running: uv run tg daemon start")
        return 1
    report = Report()
    conn = aiohttp.UnixConnector(path=str(config.SOCKET))
    async with aiohttp.ClientSession(
        connector=conn, timeout=aiohttp.ClientTimeout(total=CALL_TIMEOUT_SEC)
    ) as s:
        me = await call(s, "status")
        print("account:", me["account"]["name"], "| premium:", me["account"].get("premium"))
        dialogs = await call(s, "dialogs", limit=40)

        await run_cases(s, report)
        await run_optional(s, report, dialogs, me)

    print("\nfailures:", report.failures)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
