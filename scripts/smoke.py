#!/usr/bin/env python3
"""Live check of the reading tools against a running daemon.

Run: uv run python scripts/smoke.py
Writes nothing to Telegram: reads only. Puts downloads into a temporary folder
and deletes it. For each method prints ok/the error and a short digest.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import pathlib

import aiohttp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tgagent import config  # noqa: E402


async def call(session: aiohttp.ClientSession, method: str, **params):
    async with session.post(f"http://localhost/call", json={"method": method, "params": params}) as r:
        data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error"))
    return data["result"]


def brief(value, limit: int = 110) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


async def main() -> int:
    if not config.SOCKET.exists():
        print("daemon is not running: uv run tg daemon start")
        return 1
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="tg-smoke-"))
    failures = 0
    conn = aiohttp.UnixConnector(path=str(config.SOCKET))
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=180)) as s:
        me = await call(s, "status")
        print("account:", me["account"]["name"], "| premium:", me["account"].get("premium"))

        # chat for the media checks: take the first channel with attachments
        dialogs = await call(s, "dialogs", limit=40)
        probe = "me"

        # the third element is a suffix for the name: it tells apart calls of one and
        # the same method with different params, once kind alone no longer distinguishes them.
        cases: list[tuple] = [
            ("status", {}),
            ("accounts", {}),
            ("structure", {}),
            ("folders", {}),
            ("dialogs", {"limit": 5}),
            ("unread", {}),
            ("pending", {"limit": 5}),
            ("pending", {"direction": "mine", "min_age_hours": 24, "limit": 5}, "mine"),
            ("pending", {"direction": "both", "kind": "user", "limit": 5}, "both/user"),
            ("history", {"chat": probe, "limit": 3}),
            ("history_batch", {"chats": [probe], "limit": 2}),
            ("search", {"query": "http", "limit": 3}),
            ("mentions", {"limit": 3}),
            ("chat_info", {"chat": probe}),
            ("chat_info", {"chat": probe, "counters": False}, "no counters"),
            ("person", {"user": "me", "messages": 5, "chats": 5}),
            ("contacts", {"limit": 5}),
            ("drafts", {}),
            ("scheduled", {"chat": probe}),
            ("events", {"limit": 5}),
            ("stickers", {"scope": "sets"}),
            ("resolve_link", {"link": "https://t.me/telegram"}),
            ("media", {"chat": probe, "kind": "photo", "limit": 3}),
            ("media", {"chat": probe, "kind": "link", "limit": 3}),
            ("media", {"chat": probe, "kind": "voice", "limit": 3}),
            ("activity", {"since": "today", "limit_chats": 5}),
            ("activity", {"chat": probe, "limit_days": 7}, "by chat"),
            ("actions", {"limit": 5}),
            ("actions", {"since": "today", "limit": 5}, "for today"),
            ("remind", {"list": True}, "list"),
            ("index", {"action": "status"}, "status"),
            ("saved_tags", {}),
            ("stories", {}),
            ("sessions", {}),
            ("contacts", {"kind": "top", "limit": 5}),
            ("contacts", {"kind": "birthdays", "limit": 5}),
            ("contacts", {"kind": "online", "limit": 5}),
            ("contacts", {"kind": "blocked", "limit": 3}),
            ("dialogs", {"kind": "inactive", "limit": 3}),
            ("mentions", {"kind": "reactions", "limit": 3}),
            ("search", {"chat": probe, "kind": "file", "limit": 3}),
            ("wait", {"keyword": "nothing-of-the-sort-will-arrive", "timeout": 5}),
        ]
        for case in cases:
            method, params = case[0], case[1]
            note = case[2] if len(case) > 2 else params.get("kind")
            label = method + (f"({note})" if note else "")
            try:
                res = await call(s, method, **params)
                print(f"ok    {label:22} {brief(res)}")
            except Exception as exc:
                failures += 1
                print(f"FAIL  {label:22} {exc}")

        # Next come the methods that need data the account may not have: a chat where
        # I am an admin, a filled index, a channel in the list. Missing such data is a
        # skip, not a failure, so every case is checked separately.

        # invite links: they are given out only by a chat where I am an admin
        try:
            shown = False
            for d in dialogs:
                if d.get("type") not in ("group", "channel"):
                    continue
                try:
                    res = await call(s, "invites", chat=d["id"], limit=3)
                except Exception:
                    continue
                print(f"ok    {'invites':22} links: {res.get('total')}, admins: {len(res.get('admins') or [])}")
                shown = True
                break
            if not shown:
                print("skip invites: no chat where I have admin rights")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'invites':22} {exc}")

        # Telegram returns similar channels only for channels
        try:
            channel = next((d for d in dialogs if d.get("type") == "channel"), None)
            if channel:
                res = await call(s, "chat_info", chat=channel["id"], similar=True)
                sim = res.get("similar") or {}
                got = sim.get("error") or f"similar channels: {len(sim.get('items') or [])}"
                print(f"ok    {'chat_info(similar)':22} {got}")
            else:
                print("skip chat_info(similar): no channels in the dialog list")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'chat_info(similar)':22} {exc}")

        # local search: without a filled index there is nothing to search through
        try:
            status = await call(s, "index", action="status")
            indexed = [c for c in (status.get("chats") or [])]
            if indexed:
                res = await call(s, "search", query="a", engine="local", limit=3)
                print(f"ok    {'search(local)':22} found {res.get('total')} in {res.get('indexed_chats')} chats")
                res = await call(s, "search", query="a", engine="local", author="me", limit=3)
                print(f"ok    {'search(local,author)':22} found {res.get('total')}")
            else:
                print("skip search(local): the local index is empty")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'search(local)':22} {exc}")

        # Saved Messages filtered by source: an empty answer here is a normal answer,
        # the path itself is what is checked, not the presence of something forwarded from a particular chat
        try:
            source = next((d for d in dialogs if d.get("id") != me["account"]["id"]), None)
            if source:
                res = await call(s, "history", chat="me", saved_from=source["id"], limit=3)
                print(f"ok    {'history(saved_from)':22} messages: {res.get('total')}")
            else:
                print("skip history(saved_from): there are no dialogs besides Saved Messages")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'history(saved_from)':22} {exc}")

        # viewing a picture and transcribing a voice message — only if there is something to do it on
        try:
            photos = await call(s, "media", chat=probe, kind="photo", limit=1)
            items = photos.get("items") or []
            if items:
                info = await call(s, "view", chat=probe, message_id=items[0]["message_id"], size="preview")
                print(f"ok    {'view':22} {info.get('dimensions')} {info.get('bytes')} bytes")
            else:
                print("skip view: no photos in Saved Messages")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'view':22} {exc}")

        # we look for a voice message across the last few chats: Saved Messages may not have one
        try:
            found: tuple[str, int] | None = None
            for d in ([{"name": probe}] + dialogs)[:12]:
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
            if found:
                res = await call(s, "transcribe", chat=found[0], message_ids=[found[1]])
                first = (res.get("items") or [{}])[0]
                print(f"ok    {'transcribe':22} engine={first.get('engine')} {brief(first.get('text'), 60)}")
            else:
                print("skip transcribe: no voice messages found in the recent chats")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'transcribe':22} {exc}")

        # a summary of a long post — if one turns up in the recent chats
        try:
            done = False
            for d in dialogs[:15]:
                hist = await call(s, "history", chat=d.get("id") or d.get("name"), limit=15)
                long = [m for m in hist["messages"] if len(m.get("text") or "") > 800]
                if not long:
                    continue
                res = await call(s, "summarize", chat=d["id"], message_ids=[long[0]["id"]])
                item = (res.get("items") or [{}])[0]
                got = item.get("summary") or item.get("error")
                print(f"ok    {'summarize':22} {len(long[0]['text'])} -> {len(got or '')} chars")
                done = True
                break
            if not done:
                print("skip summarize: no long posts in the recent chats")
        except Exception as exc:
            failures += 1
            print(f"FAIL  {'summarize':22} {exc}")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nfailures:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
