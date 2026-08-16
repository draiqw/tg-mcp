"""Telethon-backed operations over the user's own Telegram account.

Everything here runs inside the daemon process, which is the single owner of the
session file. Results are plain JSON-serialisable dicts so the MCP layer can hand
them straight to the model.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telethon import TelegramClient, functions, types, utils
from telethon.errors import FloodWaitError
from telethon.helpers import add_surrogate, del_surrogate

from . import config


class GuardError(RuntimeError):
    """Raised when a write action hits a safety limit."""


class RateGuard:
    """Rolling-hour limits so a runaway loop cannot spam or wipe chats."""

    def __init__(self, limits: dict[str, int]):
        self.limits = limits
        self.sends: deque[tuple[float, str]] = deque()
        self.deletes: deque[float] = deque()

    def _trim(self) -> None:
        cutoff = time.time() - 3600
        while self.sends and self.sends[0][0] < cutoff:
            self.sends.popleft()
        while self.deletes and self.deletes[0] < cutoff:
            self.deletes.popleft()

    def check_send(self, chat_key: str) -> None:
        self._trim()
        if len(self.sends) >= self.limits["max_sends_per_hour"]:
            raise GuardError(
                f"Send limit reached ({self.limits['max_sends_per_hour']}/hour). "
                "Raise LIMITS in config.py if this is intentional."
            )
        chats = {c for _, c in self.sends} | {chat_key}
        if len(chats) > self.limits["max_distinct_chats_per_hour"]:
            raise GuardError(
                f"Refusing to message a {len(chats)}th distinct chat this hour "
                "(mass-messaging guard). Do it manually or raise the limit."
            )

    def record_send(self, chat_key: str) -> None:
        self.sends.append((time.time(), chat_key))

    def check_delete(self, count: int) -> None:
        self._trim()
        if len(self.deletes) + count > self.limits["max_deletes_per_hour"]:
            raise GuardError("Delete limit reached (50/hour).")

    def record_delete(self, count: int) -> None:
        now = time.time()
        for _ in range(count):
            self.deletes.append(now)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _media_kind(msg) -> str | None:
    if not msg.media:
        return None
    if msg.photo:
        return "photo"
    if msg.video_note:
        return "round"       # a round note is video too, so it is checked first
    if msg.gif:
        return "gif"
    if msg.video:
        return "video"
    if msg.voice:
        return "voice"
    if msg.audio:
        return "audio"
    if msg.sticker:
        return "sticker"
    if msg.document:
        name = None
        for attr in getattr(msg.document, "attributes", []):
            name = getattr(attr, "file_name", None) or name
        return f"document:{name}" if name else "document"
    if msg.web_preview:
        return "link_preview"
    if msg.poll:
        return "poll"
    if msg.contact:
        return "contact"
    if msg.geo:
        return "location"
    return type(msg.media).__name__


MEDIA_FILTERS = {
    "photo": types.InputMessagesFilterPhotos,
    "video": types.InputMessagesFilterVideo,
    "media": types.InputMessagesFilterPhotoVideo,
    "file": types.InputMessagesFilterDocument,
    "music": types.InputMessagesFilterMusic,
    "voice": types.InputMessagesFilterVoice,
    "round": types.InputMessagesFilterRoundVideo,
    "gif": types.InputMessagesFilterGif,
    "link": types.InputMessagesFilterUrl,
    "pinned": types.InputMessagesFilterPinned,
    "geo": types.InputMessagesFilterGeo,
    "contact": types.InputMessagesFilterContacts,
}


def _user_status(user) -> str | None:
    st = getattr(user, "status", None)
    if st is None:
        return None
    name = type(st).__name__
    if name == "UserStatusOnline":
        return "online"
    if name == "UserStatusOffline":
        return f"last seen {_iso(st.was_online)}"
    return {
        "UserStatusRecently": "recently",
        "UserStatusLastWeek": "within a week",
        "UserStatusLastMonth": "within a month",
    }.get(name)


def _participant_role(user) -> str | None:
    p = getattr(user, "participant", None)
    if p is None:
        return None
    name = type(p).__name__
    rank = getattr(p, "rank", None)
    if "Creator" in name:
        return rank or "owner"
    if "Admin" in name:
        return rank or "admin"
    if "Banned" in name:
        return "restricted"
    if "Left" in name:
        return "left"
    return None


def dm_link(user) -> str | None:
    """A link to the DM with a person."""
    username = getattr(user, "username", None)
    if username:
        return f"https://t.me/{username}"
    uid = getattr(user, "id", None)
    return f"tg://user?id={uid}" if uid else None


def entity_name(entity) -> str:
    try:
        return utils.get_display_name(entity) or str(getattr(entity, "id", "?"))
    except Exception:
        return str(getattr(entity, "id", "?"))


def _links(msg) -> list[str] | None:
    """Links from the text: both bare urls and the ones hidden behind a label.

    Telegram counts entity offsets in UTF-16, while Python slices a string by
    code points: without converting to surrogates any emoji above the BMP shifts
    the slicing and the link arrives cut off.
    """
    text = add_surrogate(msg.message or "")

    def cut(e) -> str:
        return del_surrogate(text[e.offset : e.offset + e.length])

    urls = []
    for e in msg.entities or []:
        name = type(e).__name__
        if name == "MessageEntityUrl":
            urls.append(cut(e))
        elif name == "MessageEntityTextUrl":
            label = cut(e)
            urls.append(f"{e.url} ({label})" if label else e.url)
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out or None


def _web_preview(msg) -> dict | None:
    """The link preview card — what Telegram shows under the message."""
    page = getattr(msg, "web_preview", None)
    if page is None or not getattr(page, "url", None):
        return None
    desc = getattr(page, "description", None)
    row = {
        "url": page.url,
        "site": getattr(page, "site_name", None),
        "title": getattr(page, "title", None),
        "description": (desc[:300] if desc else None),
        "type": getattr(page, "type", None),
    }
    return {k: v for k, v in row.items() if v}


def _reactions(msg) -> list[dict] | None:
    """Who reacted and how: [{'emoji': '👍', 'count': 3, 'mine': True}]."""
    res = getattr(getattr(msg, "reactions", None), "results", None)
    if not res:
        return None
    rows = []
    for item in res:
        emoji = getattr(item.reaction, "emoticon", None) or getattr(
            item.reaction, "document_id", None
        )
        row = {"emoji": emoji, "count": item.count}
        if getattr(item, "chosen_order", None) is not None:
            row["mine"] = True
        rows.append(row)
    return rows or None


def reaction_of(obj) -> str | None:
    """The reaction emoji; for custom ones the document id instead."""
    return getattr(obj, "emoticon", None) or getattr(obj, "document_id", None)


def _input_reaction(value: Any):
    """A plain emoji or a custom emoji id (Premium) — into a reaction object."""
    text = str(value)
    if text.isdigit():
        return types.ReactionCustomEmoji(document_id=int(text))
    return types.ReactionEmoji(emoticon=text)


def _buttons(msg) -> list[dict] | None:
    """Buttons under a bot message, with the coordinates for pressing them."""
    try:
        rows = msg.buttons
    except Exception:
        return None
    if not rows:
        return None
    out = []
    for i, row in enumerate(rows):
        for j, b in enumerate(row):
            out.append(
                {
                    "row": i,
                    "col": j,
                    "text": b.text,
                    "url": getattr(b, "url", None),
                }
            )
    return [{k: v for k, v in b.items() if v is not None} for b in out]


def _preview_thumb(msg, max_width: int = 1280) -> tuple[int, str | None]:
    """Which image size to take for showing it to the model.

    A photo original can be several megabytes — there is no point dragging it
    into the context. We take the largest variant no wider than max_width; for
    video and documents that is simply the biggest preview (thumb=-1).
    """
    sizes = getattr(getattr(msg, "photo", None), "sizes", None)
    if not sizes:
        return -1, None
    best, best_w = None, 0
    for i, s in enumerate(sizes):
        w = getattr(s, "w", None)
        if w is None:            # stripped/cropped sizes without dimensions: skip
            continue
        if w <= max_width and w > best_w:
            best, best_w = i, w
    if best is None:
        return -1, None
    s = sizes[best]
    return best, f"{getattr(s, 'w', '?')}x{getattr(s, 'h', '?')}"


def _day_start() -> datetime:
    """The start of today in local time, but expressed in UTC.

    "Today" for a person is their own midnight, not midnight in Greenwich.
    """
    local_midnight = datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc)


def _parse_when(when: Any) -> datetime:
    """ISO time, unix seconds or a relative '+30m' / '+2h' / '+3d'."""
    if isinstance(when, (int, float)):
        return datetime.fromtimestamp(float(when), timezone.utc)
    raw = str(when).strip()
    if raw.startswith("+"):
        unit = raw[-1].lower()
        mult = {"m": 60, "h": 3600, "d": 86400}.get(unit)
        try:
            amount = float(raw[1:-1])
        except ValueError:
            mult = None
        if not mult:
            raise ValueError("Relative time is written as +30m, +2h, +3d")
        return datetime.now(timezone.utc) + timedelta(seconds=amount * mult)
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"Could not read the time {when!r}: need ISO (2026-08-17T09:00) or +2h"
        ) from exc
    # A naive time is read as local — a person writes "at 9 am" about themselves.
    return dt if dt.tzinfo else dt.astimezone()


class TelegramService:
    # The limit counter is one per process, not per account: otherwise the
    # guarantee "the agent will not mail the whole contact list" could be
    # sidestepped by signing in with a second session.
    _shared_guard = RateGuard(config.LIMITS)

    def __init__(self, account: str | None = None) -> None:
        config.ensure_dirs()
        api_id, api_hash = config.api_credentials()
        self.account = config.normalize_account(account)
        self.client = TelegramClient(
            str(config.session_path(self.account)),
            api_id,
            api_hash,
            device_model="claude-tg-agent",
            system_version="macOS",
            app_version="tgagent 0.1",
        )
        self.guard = TelegramService._shared_guard
        self.me = None
        self._dialog_cache: list[dict[str, Any]] = []
        self._dialog_cache_at = 0.0

    # ---------- lifecycle ----------

    async def start(self) -> dict:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError(
                "Session is not authorized. Run `uv run tg login` in your terminal."
            )
        self.me = await self.client.get_me()
        return self.whoami_dict()

    async def stop(self) -> None:
        await self.client.disconnect()

    def whoami_dict(self) -> dict:
        m = self.me
        return {
            "account": self.account,
            "id": m.id,
            "name": entity_name(m),
            "username": m.username,
            "phone": f"***{m.phone[-4:]}" if m.phone else None,
            "premium": bool(getattr(m, "premium", False)),
        }

    # ---------- entity resolution ----------

    async def resolve(self, chat: Any):
        """Accept an id, @username, t.me link, 'me'/'saved', or a chat title."""
        if chat is None:
            raise ValueError("chat is required")
        if isinstance(chat, int):
            return await self.client.get_entity(chat)

        raw = str(chat).strip()
        low = raw.lower()
        if low in ("me", "self", "saved", "saved messages", "избранное"):
            return "me"
        if raw.lstrip("-").isdigit():
            return await self.client.get_entity(int(raw))
        if raw.startswith("@") or "t.me/" in low or low.startswith("+"):
            return await self.client.get_entity(raw)

        # Fall back to a title match over the dialog list.
        dialogs = await self._dialogs_index()
        exact = [d for d in dialogs if d["name"].lower() == low]
        pool = exact or [d for d in dialogs if low in d["name"].lower()]
        if not pool:
            try:
                return await self.client.get_entity(raw)
            except Exception as exc:
                raise ValueError(f"No chat matching {raw!r} ({exc})") from exc
        if len(pool) > 1 and not exact:
            names = ", ".join(f"{d['name']} (id {d['id']})" for d in pool[:8])
            raise ValueError(
                f"{len(pool)} chats match {raw!r}: {names}. Pass the exact id."
            )
        return await self.client.get_entity(pool[0]["id"])

    async def _dialogs_index(self, max_age: float = 60.0) -> list[dict]:
        """Names of all chats, archive included — otherwise an archived chat is
        not findable by title."""
        if self._dialog_cache and time.time() - self._dialog_cache_at < max_age:
            return self._dialog_cache
        index = []
        for archived in (False, True):
            async for d in self.client.iter_dialogs(limit=1000, archived=archived):
                index.append({"id": d.id, "name": d.name or "", "archived": archived})
        self._dialog_cache = index
        self._dialog_cache_at = time.time()
        return index

    # ---------- serialisation ----------

    def message_dict(self, msg, chat_name: str | None = None) -> dict:
        sender = msg.sender
        out = {
            "id": msg.id,
            "date": _iso(msg.date),
            "text": msg.message or "",
            "out": bool(msg.out),
            "from_id": getattr(sender, "id", None) or getattr(msg.from_id, "user_id", None),
            "from": "you" if msg.out else (entity_name(sender) if sender else None),
            "reply_to": getattr(msg.reply_to, "reply_to_msg_id", None),
            "media": _media_kind(msg),
            "mentioned": bool(msg.mentioned),
            "edited": _iso(msg.edit_date),
        }
        if chat_name:
            out["chat"] = chat_name
        if msg.fwd_from:
            out["forwarded"] = True
        out["reactions"] = _reactions(msg)
        out["links"] = _links(msg)
        out["preview"] = _web_preview(msg)
        if not out["text"] and out["media"]:
            out["text"] = f"[{out['media']}]"
        return {k: v for k, v in out.items() if v not in (None, False, "")} | {"id": msg.id}

    def message_link(self, msg, ent=None) -> str | None:
        """A link to the message itself — the one that opens in a Telegram client."""
        chat = ent if ent is not None else getattr(msg, "chat", None)
        # A message link exists only in channels and supergroups. A person has an
        # @username too, but t.me/username/123 leads elsewhere: in a DM there is
        # no message link at all.
        if not (getattr(chat, "broadcast", False) or getattr(chat, "megagroup", False)):
            return None
        username = getattr(chat, "username", None)
        if username:
            return f"https://t.me/{username}/{msg.id}"
        try:
            cid = utils.get_peer_id(chat) if chat is not None else None
        except Exception:
            cid = None
        if cid is not None and str(cid).startswith("-100"):
            return f"https://t.me/c/{str(cid)[4:]}/{msg.id}"
        return None  # in a DM and a plain group no message link exists

    # ---------- read operations ----------

    @staticmethod
    def dialog_kind(d) -> str:
        """user / bot / group / channel — the way Telegram itself tells them apart."""
        if d.is_user:
            return "bot" if getattr(d.entity, "bot", False) else "user"
        if d.is_group:
            return "group"
        if d.is_channel:
            return "channel"
        return "group"

    def dialog_row(self, d) -> dict:
        ent = d.entity
        username = getattr(ent, "username", None)
        return {
            "id": d.id,
            "name": d.name,
            "type": self.dialog_kind(d),
            "unread": d.unread_count,
            "mentions": d.unread_mentions_count,
            "pinned": bool(d.pinned),
            "archived": bool(d.archived),
            "username": username,
            "link": f"https://t.me/{username}" if username else None,
            "members": getattr(ent, "participants_count", None),
            "last": _iso(d.date),
            "last_text": (d.message.message or _media_kind(d.message) or "")[:160]
            if d.message
            else "",
        }

    async def dialogs(
        self,
        limit: int = 30,
        unread_only: bool = False,
        archived: bool | None = False,
        query: str | None = None,
        kind: str | None = None,
    ) -> list[dict]:
        """archived: False is the main list, True the archive, None both folders.

        kind="inactive" is a separate slice: groups and channels where nothing
        has happened for a long time. That is what Telegram itself offers when
        cleaning up subscriptions.
        """
        if kind == "inactive":
            res = await self.client(functions.channels.GetInactiveChannelsRequest())
            rows = []
            for ch, ts in zip(res.chats, res.dates):
                rows.append(
                    {
                        "id": utils.get_peer_id(ch),
                        "name": entity_name(ch),
                        "type": "channel" if getattr(ch, "broadcast", False) else "group",
                        "members": getattr(ch, "participants_count", None),
                        "last_activity": _iso(datetime.fromtimestamp(ts, timezone.utc)),
                    }
                )
            rows.sort(key=lambda r: r["last_activity"] or "")
            return rows[:limit]

        rows: list[dict] = []
        wide = unread_only or query or kind
        scan_limit = 1000 if wide else limit + 20
        async for d in self.client.iter_dialogs(limit=scan_limit, archived=archived):
            # Telethon mixes pinned dialogs into the answer regardless of the
            # folder, so we split archive and main list by the dialog's own flag.
            if archived is not None and bool(d.archived) != bool(archived):
                continue
            if unread_only and not d.unread_count and not d.unread_mentions_count:
                continue
            if query and query.lower() not in (d.name or "").lower():
                continue
            if kind and self.dialog_kind(d) != kind:
                continue
            rows.append(self.dialog_row(d))
            if len(rows) >= limit:
                break
        return rows

    # ---------- structure: folders, archive, breakdown ----------

    async def folders(self) -> list[dict]:
        """Telegram folders and what lies in each of them."""
        res = await self.client(functions.messages.GetDialogFiltersRequest())
        filters = getattr(res, "filters", res)
        index = {d["id"]: d["name"] for d in await self._dialogs_index()}

        async def peers(items) -> list[dict]:
            rows = []
            for p in items or []:
                try:
                    pid = utils.get_peer_id(p)
                except Exception:
                    continue
                name = index.get(pid)
                if name is None:
                    # A chat from the folder that is not in the dialog list (a
                    # hidden one, say, or one with no conversation) — ask
                    # Telegram directly.
                    try:
                        name = entity_name(await self.client.get_entity(p))
                        index[pid] = name
                    except Exception:
                        name = None
                rows.append({"id": pid, "name": name})
            return rows

        out = []
        for f in filters:
            if isinstance(f, types.DialogFilterDefault):
                out.append({"id": 0, "title": "All chats", "kind": "default"})
                continue
            title = getattr(f.title, "text", f.title)
            row = {
                "id": f.id,
                "title": title,
                "emoji": getattr(f, "emoticon", None),
                "kind": "shared"
                if isinstance(f, types.DialogFilterChatlist)
                else "custom",
                "pinned": await peers(getattr(f, "pinned_peers", [])),
                "chats": await peers(getattr(f, "include_peers", [])),
                "excluded": await peers(getattr(f, "exclude_peers", [])),
                "auto_include": [
                    k
                    for k in ("contacts", "non_contacts", "groups", "broadcasts", "bots")
                    if getattr(f, k, False)
                ],
                "auto_exclude": [
                    k
                    for k in ("exclude_muted", "exclude_read", "exclude_archived")
                    if getattr(f, k, False)
                ],
            }
            row["total"] = len(row["pinned"]) + len(row["chats"])
            out.append(row)
        return out

    async def structure(self, sample: int = 0) -> dict:
        """A map of the account: how much of what, what is archived, which folders."""
        main: list[dict] = []
        archive: list[dict] = []
        for archived, bucket in ((False, main), (True, archive)):
            async for d in self.client.iter_dialogs(limit=1000, archived=archived):
                if bool(d.archived) != archived:
                    continue
                bucket.append(self.dialog_row(d))

        def breakdown(rows: list[dict]) -> dict:
            out: dict[str, int] = {}
            for r in rows:
                out[r["type"]] = out.get(r["type"], 0) + 1
            return out

        result = {
            "account": self.whoami_dict(),
            "main": {
                "total": len(main),
                "by_type": breakdown(main),
                "unread_chats": sum(1 for r in main if r["unread"]),
                "unread_messages": sum(r["unread"] for r in main),
                "pinned": [r["name"] for r in main if r["pinned"]],
            },
            "archive": {"total": len(archive), "by_type": breakdown(archive)},
            "folders": await self.folders(),
        }
        if sample:
            result["main"]["chats"] = main[:sample]
            result["archive"]["chats"] = archive[:sample]
        return result

    async def unread_summary(
        self, limit_chats: int = 20, per_chat: int = 5, archived: bool | None = None
    ) -> list[dict]:
        """By default looks at both the main list and the archive — unread matters
        everywhere."""
        out = []
        for d in await self.dialogs(limit=limit_chats, unread_only=True, archived=archived):
            ent = await self.client.get_entity(d["id"])
            msgs = []
            async for m in self.client.iter_messages(ent, limit=min(per_chat, max(d["unread"], 1))):
                if m.out:
                    continue
                msgs.append(self.message_dict(m))
            out.append(
                {
                    "chat": d["name"],
                    "id": d["id"],
                    "type": d["type"],
                    "archived": d["archived"],
                    "unread": d["unread"],
                    "mentions": d["mentions"],
                    "messages": list(reversed(msgs)),
                }
            )
        return out

    async def history(
        self,
        chat: Any,
        limit: int = 40,
        before_id: int | None = None,
        from_user: Any = None,
        search: str | None = None,
        topic: int | None = None,
    ) -> dict:
        ent = await self.resolve(chat)
        name = entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages"
        kwargs: dict[str, Any] = {"limit": limit}
        if before_id:
            kwargs["offset_id"] = before_id
        if search:
            kwargs["search"] = search
        if from_user:
            kwargs["from_user"] = await self.resolve(from_user)
        if topic:
            kwargs["reply_to"] = int(topic)   # a forum topic is read as a thread
        msgs = [self.message_dict(m) async for m in self.client.iter_messages(ent, **kwargs)]
        return {"chat": name, "messages": list(reversed(msgs))}

    async def saved_tags(self) -> list[dict]:
        """Saved Messages tags — the very labels that mark up saved messages."""
        res = await self.client(functions.messages.GetSavedReactionTagsRequest(hash=0))
        rows = []
        for t in getattr(res, "tags", []):
            rows.append(
                {
                    "title": t.title,
                    "emoji": getattr(t.reaction, "emoticon", None),
                    "custom_emoji_id": getattr(t.reaction, "document_id", None),
                    "count": t.count,
                }
            )
        return rows

    async def search(
        self,
        query: str = "",
        chat: Any = None,
        limit: int = 30,
        kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        tag: str | None = None,
    ) -> list[dict]:
        """Search through the conversation: across all chats or one, with filters.

        kind is the attachment type (the same tabs as in tg_media), since/until
        are ISO dates, tag is a Saved Messages label (`chat="me"`), as in
        Telegram itself.
        """
        ent = await self.resolve(chat) if chat else None
        kwargs: dict[str, Any] = {"search": query, "limit": limit}
        if kind:
            if kind not in MEDIA_FILTERS:
                raise ValueError(f"kind must be one of: {', '.join(sorted(MEDIA_FILTERS))}")
            kwargs["filter"] = MEDIA_FILTERS[kind]
        if until:
            kwargs["offset_date"] = _parse_when(until)
        since_dt = _parse_when(since) if since else None

        if tag:
            if ent not in ("me", None) and utils.get_peer_id(await self.client.get_entity(ent)) != (await self.client.get_me()).id:
                raise ValueError("tags live only in Saved Messages: chat=\"me\"")
            tags = await self.saved_tags()
            needle = str(tag).strip().lower()
            match = next(
                (
                    t
                    for t in tags
                    # A label can be given a word, or left as it is — then it is
                    # recognised by the emoji or by the custom emoji id.
                    if (t["title"] or "").lower() == needle
                    or (t["emoji"] or "") == tag
                    or str(t["custom_emoji_id"] or "") == needle
                ),
                None,
            )
            if match is None:
                known = ", ".join(
                    t["title"] or t["emoji"] or str(t["custom_emoji_id"]) for t in tags
                ) or "there are none"
                raise ValueError(f"there is no tag {tag!r}. There is: {known}")
            reaction = (
                types.ReactionCustomEmoji(document_id=match["custom_emoji_id"])
                if match["custom_emoji_id"]
                else types.ReactionEmoji(emoticon=match["emoji"])
            )
            me = await self.client.get_input_entity("me")
            res = await self.client(
                functions.messages.SearchRequest(
                    peer=me, q=query or "", filter=kwargs.get("filter") or types.InputMessagesFilterEmpty(),
                    min_date=None, max_date=None, offset_id=0, add_offset=0,
                    limit=limit, max_id=0, min_id=0, hash=0, saved_reaction=[reaction],
                )
            )
            return [self.message_dict(m) for m in getattr(res, "messages", [])]

        rows = []
        async for m in self.client.iter_messages(ent, **kwargs):
            if since_dt and m.date and m.date < since_dt:
                break
            chat_name = None
            if ent is None:
                try:
                    chat_name = entity_name(await m.get_chat())
                except Exception:
                    chat_name = None
            rows.append(self.message_dict(m, chat_name))
        return rows

    async def mentions(self, limit: int = 20, kind: str = "mentions") -> list[dict]:
        """kind=mentions is where you were called, kind=reactions is what was
        reacted to."""
        if kind not in ("mentions", "reactions"):
            raise ValueError("kind: mentions or reactions")
        rows = []

        if kind == "reactions":
            # Telegram does not hand out unread reactions as one list: we ask per
            # dialog, among those where anything at all is unread.
            for d in await self.dialogs(limit=60, unread_only=True, archived=None):
                try:
                    ent = await self.client.get_entity(d["id"])
                    res = await self.client(
                        functions.messages.GetUnreadReactionsRequest(
                            peer=ent, offset_id=0, add_offset=0,
                            limit=min(limit, 20), max_id=0, min_id=0,
                        )
                    )
                except Exception:
                    continue
                for m in getattr(res, "messages", []):
                    row = self.message_dict(m, d["name"])
                    row["reactions"] = row.get("reactions")
                    rows.append(row)
                if len(rows) >= limit:
                    break
            return rows[:limit]

        for d in await self.dialogs(limit=50, unread_only=True):
            if not d["mentions"]:
                continue
            ent = await self.client.get_entity(d["id"])
            async for m in self.client.iter_messages(
                ent, limit=d["mentions"], filter=types.InputMessagesFilterMyMentions
            ):
                rows.append(self.message_dict(m, d["name"]))
            if len(rows) >= limit:
                break
        return rows[:limit]

    async def chat_info(self, chat: Any) -> dict:
        ent = await self.client.get_entity(await self.resolve(chat))
        info = {
            "id": utils.get_peer_id(ent),
            "name": entity_name(ent),
            "username": getattr(ent, "username", None),
            "type": type(ent).__name__,
            "bot": bool(getattr(ent, "bot", False)),
            "verified": bool(getattr(ent, "verified", False)),
            "participants": getattr(ent, "participants_count", None),
            "phone": getattr(ent, "phone", None),
        }
        try:
            full = await self.client(functions.users.GetFullUserRequest(ent))
            info["about"] = full.full_user.about
        except Exception:
            pass
        return {k: v for k, v in info.items() if v is not None}

    async def participants(self, chat: Any, limit: int = 50, query: str | None = None) -> list[dict]:
        """Chat participants with DM links and metadata."""
        ent = await self.resolve(chat)
        rows = []
        async for u in self.client.iter_participants(ent, limit=limit, search=query or ""):
            row = {
                "id": u.id,
                "name": entity_name(u),
                "username": f"@{u.username}" if u.username else None,
                "link": dm_link(u),
                "phone": u.phone,
                "bot": bool(u.bot),
                "premium": bool(getattr(u, "premium", False)),
                "verified": bool(getattr(u, "verified", False)),
                "deleted": bool(getattr(u, "deleted", False)),
                "contact": bool(getattr(u, "contact", False)),
                "role": _participant_role(u),
                "last_seen": _user_status(u),
                "me": bool(getattr(u, "is_self", False)),
            }
            rows.append({k: v for k, v in row.items() if v not in (None, False)})
        return rows

    CONTACT_KINDS = ("all", "birthdays", "top", "online", "blocked")

    async def contacts(
        self, query: str | None = None, limit: int = 50, kind: str = "all"
    ) -> list[dict]:
        """Contacts and slices over them: birthdays, whom you write to most often, who is online, the blocklist."""
        if kind not in self.CONTACT_KINDS:
            raise ValueError(f"kind: {', '.join(self.CONTACT_KINDS)}")

        if kind == "birthdays":
            res = await self.client(functions.contacts.GetBirthdaysRequest())
            users = {u.id: u for u in res.users}
            rows = []
            for row in res.contacts:
                u = users.get(row.contact_id)
                b = row.birthday
                rows.append(
                    {
                        "id": row.contact_id,
                        "name": entity_name(u) if u else None,
                        "username": getattr(u, "username", None),
                        "birthday": f"{b.day:02d}.{b.month:02d}" + (f".{b.year}" if b.year else ""),
                        "day": b.day,
                        "month": b.month,
                        "year": b.year,
                    }
                )
            rows.sort(key=lambda r: (r["month"], r["day"]))
            return rows[:limit]

        if kind == "top":
            res = await self.client(
                functions.contacts.GetTopPeersRequest(
                    correspondents=True, bots_pm=False, bots_inline=False,
                    phone_calls=False, forward_users=False, forward_chats=False,
                    groups=True, channels=True, offset=0, limit=limit, hash=0,
                )
            )
            names = {"TopPeerCategoryCorrespondents": "people",
                     "TopPeerCategoryGroups": "groups",
                     "TopPeerCategoryChannels": "channels"}
            rows = []
            for cat in getattr(res, "categories", []):
                label = names.get(type(cat.category).__name__, type(cat.category).__name__)
                for p in cat.peers[:limit]:
                    try:
                        ent = await self.client.get_entity(p.peer)
                    except Exception:
                        continue
                    rows.append(
                        {
                            "id": utils.get_peer_id(ent),
                            "name": entity_name(ent),
                            "username": getattr(ent, "username", None),
                            "category": label,
                            "rating": round(p.rating, 2),
                        }
                    )
            return rows[:limit]

        if kind == "online":
            statuses = await self.client(functions.contacts.GetStatusesRequest())
            rows = []
            for s in statuses:
                if not isinstance(s.status, types.UserStatusOnline):
                    continue
                try:
                    u = await self.client.get_entity(s.user_id)
                except Exception:
                    continue
                rows.append(
                    {
                        "id": s.user_id,
                        "name": entity_name(u),
                        "username": getattr(u, "username", None),
                        "until": _iso(getattr(s.status, "expires", None)),
                    }
                )
                if len(rows) >= limit:
                    break
            return rows

        if kind == "blocked":
            res = await self.client(
                functions.contacts.GetBlockedRequest(offset=0, limit=min(limit, 100))
            )
            return [
                {"id": u.id, "name": entity_name(u), "username": u.username}
                for u in res.users
            ]

        res = await self.client(functions.contacts.GetContactsRequest(hash=0))
        rows = []
        for u in res.users:
            name = entity_name(u)
            if query and query.lower() not in (name + " " + (u.username or "")).lower():
                continue
            rows.append({"id": u.id, "name": name, "username": u.username, "phone": u.phone})
            if len(rows) >= limit:
                break
        return rows

    async def history_batch(
        self, chats: list, limit: int = 20, search: str | None = None
    ) -> list[dict]:
        """Read several chats at once in a single call."""
        if not isinstance(chats, list) or not chats:
            raise ValueError("chats must be a non-empty list")
        if len(chats) > 25:
            raise ValueError("no more than 25 chats at a time")
        out = []
        for chat in chats:
            try:
                out.append(await self.history(chat, limit=limit, search=search))
            except Exception as exc:
                out.append({"chat": str(chat), "error": f"{type(exc).__name__}: {exc}"})
        return out

    async def media(
        self, chat: Any, kind: str = "media", limit: int = 30, before_id: int | None = None
    ) -> dict:
        """Attachment tabs, as in Telegram: photos, video, files, music, voice, links."""
        if kind not in MEDIA_FILTERS:
            raise ValueError(
                f"kind must be one of: {', '.join(sorted(MEDIA_FILTERS))}"
            )
        ent = await self.resolve(chat)
        name = entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages"
        kwargs: dict[str, Any] = {"limit": limit, "filter": MEDIA_FILTERS[kind]}
        if before_id:
            kwargs["offset_id"] = before_id
        rows = []
        total_bytes = 0
        async for m in self.client.iter_messages(ent, **kwargs):
            f = m.file
            row = {
                "message_id": m.id,
                "date": _iso(m.date),
                "from": "you" if m.out else (entity_name(m.sender) if m.sender else None),
                "kind": _media_kind(m),
                "name": getattr(f, "name", None),
                "size": getattr(f, "size", None),
                "mime": getattr(f, "mime_type", None),
                "ext": getattr(f, "ext", None),
                "duration": getattr(f, "duration", None),
                "title": getattr(f, "title", None),
                "performer": getattr(f, "performer", None),
                "caption": (m.message or "")[:200] or None,
            }
            if kind == "link":
                row["urls"] = _links(m)
                row["preview"] = _web_preview(m)
            total_bytes += row["size"] or 0
            rows.append({k: v for k, v in row.items() if v is not None})
        return {
            "chat": name,
            "kind": kind,
            "count": len(rows),
            "total_bytes": total_bytes,
            "items": rows,
        }

    async def download_many(
        self, chat: Any, message_ids: list, dest: str | None = None
    ) -> dict:
        """Download several attachments at once (take the ids from media)."""
        ids = [int(i) for i in message_ids]
        if len(ids) > 50:
            raise ValueError("no more than 50 files at a time")
        ent = await self.resolve(chat)
        target = Path(dest).expanduser() if dest else config.DOWNLOADS
        target.mkdir(parents=True, exist_ok=True)
        msgs = await self.client.get_messages(ent, ids=ids)
        saved, failed, total = [], [], 0
        for msg in msgs:
            if msg is None or not msg.media:
                failed.append({"message_id": None, "error": "no media"})
                continue
            try:
                path = await self.client.download_media(msg, file=str(target))
                size = Path(path).stat().st_size if path else 0
                total += size
                saved.append({"message_id": msg.id, "path": path, "bytes": size})
            except Exception as exc:
                failed.append({"message_id": msg.id, "error": str(exc)})
        return {
            "saved": len(saved),
            "failed": len(failed),
            "total_bytes": total,
            "dir": str(target),
            "files": saved,
            "errors": failed or None,
        }

    async def download(self, chat: Any, message_id: int, dest: str | None = None) -> dict:
        ent = await self.resolve(chat)
        msg = await self.client.get_messages(ent, ids=message_id)
        if not msg or not msg.media:
            raise ValueError("Message has no downloadable media")
        target = Path(dest).expanduser() if dest else config.DOWNLOADS
        path = await self.client.download_media(msg, file=str(target))
        return {"path": path, "bytes": Path(path).stat().st_size if path else 0}

    # ---------- one message in detail ----------

    async def message(
        self, chat: Any, message_id: int, context: int = 0, replies: int = 0
    ) -> dict:
        """The whole message: reactions, buttons, who read it, neighbours, replies."""
        ent = await self.resolve(chat)
        mid = int(message_id)
        msg = await self.client.get_messages(ent, ids=mid)
        if msg is None:
            raise ValueError(f"There is no message {mid} in this chat")
        out = self.message_dict(msg)
        out["buttons"] = _buttons(msg)

        peer = await self.client.get_input_entity(ent)
        try:
            res = await self.client(
                functions.messages.GetMessageReadParticipantsRequest(peer=peer, msg_id=mid)
            )
            out["read_by"] = len(res)
        except Exception:
            pass  # available only in small groups and for the first days — normal

        if out.get("reactions"):
            # Who exactly reacted. In large channels Telegram does not hand out
            # the list (can_see_list), so only the counters remain.
            try:
                res = await self.client(
                    functions.messages.GetMessageReactionsListRequest(
                        peer=peer, id=mid, limit=30
                    )
                )
                names = {utils.get_peer_id(u): entity_name(u)
                         for u in list(res.users) + list(getattr(res, "chats", []))}
                out["reacted_by"] = [
                    {
                        "who": names.get(utils.get_peer_id(r.peer_id)),
                        "emoji": reaction_of(r.reaction),
                        "at": _iso(r.date),
                    }
                    for r in res.reactions
                ]
            except Exception:
                pass

        if getattr(msg, "out", False):
            # Whether the peer read my message. Works only in a DM, only on
            # fresh messages and only if they have not hidden read marks.
            try:
                res = await self.client(
                    functions.messages.GetOutboxReadDateRequest(peer=peer, msg_id=mid)
                )
                out["read_at"] = _iso(res.date)
            except Exception as exc:
                out["read_at"] = None
                out["read_at_note"] = str(exc).split(" (caused")[0]

        if context:
            before = await self.client.get_messages(ent, limit=context, offset_id=mid)
            after = await self.client.get_messages(
                ent, limit=context, min_id=mid, reverse=True
            )
            out["before"] = [self.message_dict(m) for m in reversed(before)]
            out["after"] = [self.message_dict(m) for m in after]
        if replies:
            try:
                thread = await self.client.get_messages(ent, limit=replies, reply_to=mid)
                out["replies"] = [self.message_dict(m) for m in thread]
            except Exception as exc:
                out["replies_error"] = str(exc)
        return {k: v for k, v in out.items() if v is not None}

    async def resolve_link(self, link: str) -> dict:
        """What is behind this link: a person, a channel, an invite, a sticker
        pack, a message.

        Opens nothing and joins nothing — only looks at where it leads.
        """
        raw = str(link).strip()
        low = raw.lower()
        if not ("t.me/" in low or low.startswith("@") or "telegram.me/" in low):
            return {
                "kind": "external",
                "url": raw,
                "note": "an external link, Telegram knows nothing about it — "
                        "look at the content with a web tool, and only at the "
                        "owner's request",
            }

        tail = raw.split("t.me/", 1)[-1].split("telegram.me/", 1)[-1].lstrip("@")
        tail = tail.split("?", 1)[0].strip("/")
        parts = [p for p in tail.split("/") if p]

        # An invite to a private chat: t.me/+hash or t.me/joinchat/hash
        if parts and (parts[0].startswith("+") or parts[0] == "joinchat"):
            invite_hash = parts[1] if parts[0] == "joinchat" and len(parts) > 1 else parts[0][1:]
            res = await self.client(
                functions.messages.CheckChatInviteRequest(hash=invite_hash)
            )
            if isinstance(res, types.ChatInviteAlready):
                chat = res.chat
                return {
                    "kind": "invite",
                    "already_member": True,
                    "id": utils.get_peer_id(chat),
                    "title": entity_name(chat),
                    "members": getattr(chat, "participants_count", None),
                }
            return {
                "kind": "invite",
                "already_member": False,
                "title": getattr(res, "title", None),
                "members": getattr(res, "participants_count", None),
                "about": getattr(res, "about", None),
                "channel": bool(getattr(res, "broadcast", False)),
                "request_needed": bool(getattr(res, "request_needed", False)),
                "scam": bool(getattr(res, "scam", False)),
                "note": "joining only at the owner's explicit request",
            }

        if parts and parts[0] == "addstickers":
            return {"kind": "sticker_set", "short_name": parts[1] if len(parts) > 1 else None}

        if not parts:
            raise ValueError(f"Could not parse the link {link!r}")

        message_id = None
        if len(parts) > 1 and parts[-1].isdigit():
            message_id = int(parts[-1])
        target = parts[0]
        if target == "c" and len(parts) > 1:            # t.me/c/<internal_id>/<msg>
            target = int("-100" + parts[1])

        entity = await self.client.get_entity(target)
        username = getattr(entity, "username", None)
        out = {
            "kind": self.dialog_kind_of(entity),
            "id": utils.get_peer_id(entity),
            "title": entity_name(entity),
            "username": username,
            "members": getattr(entity, "participants_count", None),
            "verified": bool(getattr(entity, "verified", False)),
            "scam": bool(getattr(entity, "scam", False)),
            "bot": bool(getattr(entity, "bot", False)),
        }
        if out["members"] is None and isinstance(entity, types.Channel):
            try:                              # the member count lives only in full
                full = await self.client(
                    functions.channels.GetFullChannelRequest(channel=entity)
                )
                out["members"] = full.full_chat.participants_count
                about = getattr(full.full_chat, "about", None)
                if about:
                    out["about"] = about[:300]
            except Exception:
                pass
        if message_id:
            out["message_id"] = message_id
            try:
                msg = await self.client.get_messages(entity, ids=message_id)
                if msg is not None:
                    out["message"] = self.message_dict(msg)
            except Exception as exc:
                out["message_error"] = str(exc)
        return {k: v for k, v in out.items() if v not in (None, False)}

    @staticmethod
    def dialog_kind_of(entity) -> str:
        if isinstance(entity, types.User):
            return "bot" if getattr(entity, "bot", False) else "user"
        if isinstance(entity, types.Channel):
            return "channel" if getattr(entity, "broadcast", False) else "group"
        return "group"

    async def common_chats(self, user: Any, limit: int = 50) -> list[dict]:
        """Where you are members together — groups and channels shared with a person."""
        ent = await self.resolve(user)
        inp = utils.get_input_user(await self.client.get_input_entity(ent))
        res = await self.client(
            functions.messages.GetCommonChatsRequest(user_id=inp, max_id=0, limit=limit)
        )
        rows = []
        for ch in res.chats:
            username = getattr(ch, "username", None)
            rows.append(
                {
                    "id": utils.get_peer_id(ch),
                    "name": entity_name(ch),
                    "type": "channel" if getattr(ch, "broadcast", False) else "group",
                    "members": getattr(ch, "participants_count", None),
                    "link": f"https://t.me/{username}" if username else None,
                }
            )
        return rows

    async def drafts(self) -> list[dict]:
        """All unsent drafts across the account."""
        rows = []
        for d in await self.client.get_drafts():
            if d.is_empty:
                continue
            ent = d.entity
            rows.append(
                {
                    "chat": entity_name(ent) if ent else None,
                    "chat_id": utils.get_peer_id(ent) if ent else None,
                    "text": d.text,
                    "date": _iso(d.date),
                    "reply_to": d.reply_to_msg_id,
                }
            )
        return rows

    async def scheduled(
        self, chat: Any, limit: int = 30, cancel_ids: list | None = None
    ) -> dict:
        """The scheduled messages of a chat; with cancel_ids it cancels them."""
        ent = await self.resolve(chat)
        if cancel_ids:
            self._assert_write()
            ids = [int(i) for i in cancel_ids]
            peer = await self.client.get_input_entity(ent)
            await self.client(
                functions.messages.DeleteScheduledMessagesRequest(peer=peer, id=ids)
            )
            return {"cancelled": len(ids), "message_ids": ids}
        msgs = await self.client.get_messages(ent, limit=limit, scheduled=True)
        return {
            "count": len(msgs),
            "items": [
                {**self.message_dict(m), "scheduled_for": _iso(m.date)} for m in msgs
            ],
        }

    async def activity(
        self,
        since: str = "today",
        until: str | None = None,
        limit_chats: int = 100,
        kind: str | None = None,
        include_own: bool = True,
        per_chat: int = 0,
    ) -> dict:
        """Where any conversation happened over a period: today, over a day, over
        any stretch.

        Answers "which chats did I talk in today" — unlike `unread`, chats that
        were read get in here too, and so do those where only you wrote.
        """
        start = _day_start() if str(since).lower() in ("today", "сегодня") else _parse_when(since)
        end = _parse_when(until) if until else None
        rows: list[dict] = []
        scanned = 0
        async for d in self.client.iter_dialogs(limit=400, archived=None):
            scanned += 1
            last = getattr(d, "date", None)
            if last is None or last < start:
                # Dialogs come in descending order of the last message date, but
                # pinned ones float to the top regardless of it — hence continue,
                # not break.
                continue
            if end and last > end:
                continue
            if kind and self.dialog_kind(d) != kind:
                continue
            incoming = outgoing = 0
            first_at = None
            sample: list[dict] = []
            async for m in self.client.iter_messages(d.entity, limit=300):
                if m.date < start:
                    break
                if end and m.date > end:
                    continue
                if m.out:
                    outgoing += 1
                else:
                    incoming += 1
                first_at = m.date
                if per_chat and len(sample) < per_chat:
                    sample.append(self.message_dict(m))
            if not (incoming or outgoing):
                continue
            if not include_own and not incoming:
                continue
            row = {
                "id": d.id,
                "chat": d.name,
                "type": self.dialog_kind(d),
                "archived": bool(d.archived),
                "messages": incoming + outgoing,
                "incoming": incoming,
                "outgoing": outgoing,
                "unread": d.unread_count,
                "first_at": _iso(first_at),
                "last_at": _iso(last),
            }
            if sample:
                row["sample"] = list(reversed(sample))
            rows.append(row)
            if len(rows) >= limit_chats:
                break
        rows.sort(key=lambda r: r["last_at"] or "", reverse=True)
        return {
            "since": _iso(start),
            "until": _iso(end) if end else None,
            "chats": len(rows),
            "messages": sum(r["messages"] for r in rows),
            "incoming": sum(r["incoming"] for r in rows),
            "outgoing": sum(r["outgoing"] for r in rows),
            "scanned_dialogs": scanned,
            "items": rows,
        }

    async def export(
        self,
        chat: Any = None,
        limit: int = 1000,
        format: str = "json",
        dest: str | None = None,
        chats: list | None = None,
        since: str | None = None,
        until: str | None = None,
        media: bool = False,
        media_max_mb: int = 50,
    ) -> dict:
        """Export a conversation to a file: json to parse, markdown/text to read.

        `chats` exports several chats at once, `since="today"` only a period,
        `media=True` additionally downloads attachments and puts the file path
        and a link to the message itself into every message.
        """
        if format not in ("json", "markdown", "text"):
            raise ValueError("format: json, markdown or text")
        if chats:
            targets = list(chats)
            if len(targets) > 25:
                raise ValueError("no more than 25 chats at a time")
            out = []
            for one in targets:
                try:
                    out.append(
                        await self.export(
                            chat=one, limit=limit, format=format, dest=dest,
                            since=since, until=until, media=media,
                            media_max_mb=media_max_mb,
                        )
                    )
                except Exception as exc:
                    out.append({"chat": str(one), "error": f"{type(exc).__name__}: {exc}"})
            return {
                "chats": len(out),
                "messages": sum(r.get("messages", 0) for r in out if isinstance(r, dict)),
                "files": sum(r.get("files", 0) for r in out if isinstance(r, dict)),
                "items": out,
            }
        if chat is None:
            raise ValueError("chat or chats is required")

        limit = max(1, min(int(limit), 5000))
        start = None
        if since:
            start = _day_start() if str(since).lower() in ("today", "сегодня") else _parse_when(since)
        end = _parse_when(until) if until else None
        ent = await self.resolve(chat)
        entity = None if ent == "me" else await self.client.get_entity(ent)
        name = "Saved Messages" if ent == "me" else entity_name(entity)

        target = Path(dest).expanduser() if dest else config.DOWNLOADS
        target.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in name if c.isalnum() or c in " -_").strip()[:60] or "chat"
        media_dir = target / f"{safe} media"

        rows = []
        files = 0
        skipped: list[dict] = []
        async for m in self.client.iter_messages(ent, limit=limit):
            if start and m.date < start:
                break
            if end and m.date > end:
                continue
            row = self.message_dict(m)
            link = self.message_link(m, entity)
            if link:
                row["link"] = link
            if m.media is not None:
                f = m.file
                info = {
                    "kind": _media_kind(m),
                    "name": getattr(f, "name", None),
                    "size": getattr(f, "size", None),
                    "mime": getattr(f, "mime_type", None),
                }
                if media:
                    size_mb = (getattr(f, "size", 0) or 0) / (1024 * 1024)
                    if size_mb > media_max_mb:
                        info["skipped"] = f"larger than {media_max_mb} MB"
                        skipped.append({"message_id": m.id, "size_mb": round(size_mb, 1)})
                    else:
                        media_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            path = await self.client.download_media(m, file=str(media_dir))
                            if path:
                                info["path"] = str(path)
                                files += 1
                        except Exception as exc:
                            info["download_error"] = f"{type(exc).__name__}: {exc}"
                row["file"] = {k: v for k, v in info.items() if v is not None}
            rows.append(row)
        rows.reverse()  # chronological order reads better

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        ext = {"json": "json", "markdown": "md", "text": "txt"}[format]
        path = target / f"{safe} {stamp}.{ext}"

        if format == "json":
            body = json.dumps(
                {"chat": name, "exported_at": _iso(datetime.now(timezone.utc)),
                 "since": _iso(start) if start else None,
                 "count": len(rows), "files": files, "messages": rows},
                ensure_ascii=False, indent=2,
            )
        else:
            lines = [f"# {name}", ""] if format == "markdown" else [name, ""]
            for r in rows:
                who = r.get("from") or "?"
                stamp_line = (r.get("date") or "")[:16].replace("T", " ")
                text = r.get("text", "")
                extra = []
                f_info = r.get("file") or {}
                if f_info.get("path"):
                    extra.append(f"file: {f_info['path']}")
                elif f_info:
                    extra.append(f"attachment: {f_info.get('kind')}")
                for url in r.get("links") or []:
                    extra.append(f"link: {url}")
                if r.get("link"):
                    extra.append(r["link"])
                tail = ("\n" + "\n".join(extra)) if extra else ""
                lines.append(
                    f"**{who}** · {stamp_line}\n{text}{tail}\n" if format == "markdown"
                    else f"[{stamp_line}] {who}: {text}{tail}"
                )
            body = "\n".join(lines)
        path.write_text(body, encoding="utf-8")
        out = {
            "path": str(path),
            "chat": name,
            "messages": len(rows),
            "files": files,
            "media_dir": str(media_dir) if files else None,
            "skipped_large": skipped or None,
            "bytes": path.stat().st_size,
            "format": format,
        }
        return {k: v for k, v in out.items() if v is not None}

    # ---------- looking at media and listening to it ----------

    async def view(
        self,
        chat: Any,
        message_id: int | None = None,
        size: str = "preview",
        story_id: int | None = None,
    ) -> dict:
        """Hand over a picture in a way that lets it be shown to the model.

        `preview` takes the ready Telegram preview (small, cheap in context),
        `full` takes the original. For video and documents a preview frame is
        always handed over, if there is one. With `story_id` a story is looked
        at, not a message.
        """
        if size not in ("preview", "full"):
            raise ValueError("size: preview or full")
        if story_id is not None:
            return await self._view_story(chat, int(story_id))
        if message_id is None:
            raise ValueError("message_id or story_id is required")
        ent = await self.resolve(chat)
        msg = await self.client.get_messages(ent, ids=int(message_id))
        if msg is None:
            raise ValueError(f"There is no message {message_id} in this chat")
        if not msg.media:
            raise ValueError("The message carries no media")
        kind = _media_kind(msg)
        target = config.DOWNLOADS / "view"
        target.mkdir(parents=True, exist_ok=True)

        path, dims = None, None
        if kind == "photo" and size == "full":
            path = await self.client.download_media(msg, file=str(target))
        else:
            thumb, dims = _preview_thumb(msg)
            try:
                path = await self.client.download_media(msg, file=str(target), thumb=thumb)
            except Exception:
                path = None
            if path is None and kind in ("photo", "sticker"):
                path = await self.client.download_media(msg, file=str(target))
        if path is None:
            raise ValueError(
                f"This message ({kind}) has no picture to show — "
                "download the file with tg_download"
            )
        p = Path(path)
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(p.suffix.lower(), "image/jpeg")
        return {
            "path": str(p),
            "mime": mime,
            "kind": kind,
            "size": size,
            "dimensions": dims,
            "bytes": p.stat().st_size,
            "caption": (msg.message or "")[:300] or None,
            "date": _iso(msg.date),
        }

    async def _view_story(self, peer: Any, story_id: int) -> dict:
        """Show a story as a picture. The view is not marked in Telegram."""
        ent = await self.client.get_entity(await self.resolve(peer))
        res = await self.client(
            functions.stories.GetStoriesByIDRequest(peer=ent, id=[int(story_id)])
        )
        items = getattr(res, "stories", [])
        if not items:
            raise ValueError(f"this person has no story {story_id} (it may have expired)")
        story = items[0]
        media = getattr(story, "media", None)
        if media is None:
            raise ValueError("the story carries no media")
        target = config.DOWNLOADS / "stories"
        target.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(media, file=str(target))
        if path is None:
            raise ValueError("could not download the story media")
        p = Path(path)
        mime = {
            ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(p.suffix.lower())
        if mime is None:
            raise ValueError(
                f"this is a video story ({p.suffix}), it cannot be shown as a picture — "
                f"the file lies in {p}"
            )
        row = self._story_row(story, entity_name(ent))
        row.update({"path": str(p), "mime": mime, "bytes": p.stat().st_size})
        return row

    async def _messages_for_audio(
        self, chat: Any, message_ids: list | None, kind: str, limit: int
    ) -> tuple[Any, list]:
        ent = await self.resolve(chat)
        if message_ids:
            ids = [int(i) for i in message_ids]
            if len(ids) > 20:
                raise ValueError("no more than 20 messages at a time")
            msgs = await self.client.get_messages(ent, ids=ids)
            return ent, [m for m in msgs if m is not None]
        if kind not in MEDIA_FILTERS:
            raise ValueError(f"kind: {', '.join(sorted(MEDIA_FILTERS))}")
        msgs = []
        async for m in self.client.iter_messages(
            ent, limit=min(int(limit), 20), filter=MEDIA_FILTERS[kind]
        ):
            msgs.append(m)
        return ent, msgs

    async def _transcribe_telegram(self, ent, msg) -> str:
        """The built-in Telegram transcription. Voice messages and round notes only,
        Premium is needed."""
        peer = await self.client.get_input_entity(ent)
        res = await self.client(
            functions.messages.TranscribeAudioRequest(peer=peer, msg_id=msg.id)
        )
        text = res.text or ""
        for _ in range(10):                      # a transcript does not arrive instantly
            if not res.pending:
                break
            await asyncio.sleep(1.5)
            res = await self.client(
                functions.messages.TranscribeAudioRequest(peer=peer, msg_id=msg.id)
            )
            text = res.text or text
        if res.pending:
            raise RuntimeError("Telegram is still computing the transcript, try later")
        if not text.strip():
            raise RuntimeError("Telegram returned an empty transcript")
        return text

    async def _audio_file(self, msg, max_mb: int) -> Path:
        size = getattr(msg.file, "size", None) or 0
        if size > max_mb * 1024 * 1024:
            raise RuntimeError(
                f"The file is {round(size / 1024 / 1024, 1)} MB, over the {max_mb} MB limit"
            )
        target = config.DOWNLOADS / "audio"
        target.mkdir(parents=True, exist_ok=True)
        path = await self.client.download_media(msg, file=str(target))
        if not path:
            raise RuntimeError("could not download the audio")
        return Path(path)

    async def _transcribe_groq(self, path: Path, language: str | None) -> str:
        key = config.groq_key()
        if not key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Add the key to ~/tg-agent/.env "
                "(console.groq.com/keys)"
            )
        settings = config.whisper_settings()
        import aiohttp

        form = aiohttp.FormData()
        form.add_field("model", settings["groq_model"])
        form.add_field("response_format", "text")
        if language:
            form.add_field("language", language)
        form.add_field(
            "file", path.read_bytes(), filename=path.name,
            content_type="application/octet-stream",
        )
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=300)
        ) as sess:
            async with sess.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                data=form,
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"Groq {resp.status}: {body[:300]}")
        return body.strip()

    def _transcribe_local_sync(self, path: Path, language: str | None) -> tuple[str, str]:
        """The local model. Blocking, so it is called in a separate thread."""
        model = config.whisper_settings()["local_model"]
        try:
            import mlx_whisper                                   # Apple Silicon
        except ImportError:
            mlx_whisper = None
        if mlx_whisper is not None:
            res = mlx_whisper.transcribe(
                str(path), path_or_hf_repo=model, language=language
            )
            return (res.get("text") or "").strip(), f"mlx:{model}"
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise RuntimeError(
                "The local model is not installed. Install one of these:\n"
                "  uv sync --extra local-whisper        (mlx-whisper, fast on Apple Silicon)\n"
                "  uv pip install faster-whisper        (everywhere)"
            ) from None
        name = model.split("/")[-1].replace("whisper-", "")
        wm = WhisperModel(name, device="cpu", compute_type="int8")
        segments, _ = wm.transcribe(str(path), language=language)
        return " ".join(s.text.strip() for s in segments).strip(), f"faster-whisper:{name}"

    async def transcribe(
        self,
        chat: Any,
        message_ids: list | None = None,
        kind: str = "voice",
        limit: int = 5,
        engine: str = "auto",
        language: str | None = None,
        keep_files: bool = False,
    ) -> dict:
        """Transcribe voice messages, round notes, music and video into text.

        engine: auto (Telegram → Groq → local), telegram, groq, local.
        """
        settings = config.whisper_settings()
        engine = (engine or settings["engine"] or "auto").lower()
        if engine not in ("auto", "telegram", "groq", "local"):
            raise ValueError("engine: auto, telegram, groq or local")
        ent, msgs = await self._messages_for_audio(chat, message_ids, kind, limit)
        if not msgs:
            return {"count": 0, "items": [], "note": f"there is no '{kind}' in this chat"}

        rows = []
        for msg in msgs:
            mkind = _media_kind(msg)
            row = {
                "message_id": msg.id,
                "date": _iso(msg.date),
                "from": "you" if msg.out else (entity_name(msg.sender) if msg.sender else None),
                "kind": mkind,
                "duration": getattr(msg.file, "duration", None),
            }
            if not msg.file or not (mkind in ("voice", "round", "audio", "video", "gif")
                                    or (msg.file.mime_type or "").startswith(("audio/", "video/"))):
                row["error"] = "this is neither audio nor video"
                rows.append(row)
                continue

            # Engine order: the built-in one works only for voice and round notes.
            chain = [engine]
            if engine == "auto":
                chain = (["telegram"] if mkind in ("voice", "round") else []) + ["groq", "local"]
            errors = {}
            path = None
            try:
                for candidate in chain:
                    try:
                        if candidate == "telegram":
                            row["text"] = await self._transcribe_telegram(ent, msg)
                        else:
                            if path is None:
                                path = await self._audio_file(msg, settings["max_upload_mb"])
                            if candidate == "groq":
                                row["text"] = await self._transcribe_groq(path, language)
                            else:
                                text, used = await asyncio.to_thread(
                                    self._transcribe_local_sync, path, language
                                )
                                row["text"] = text
                                candidate = used
                        row["engine"] = candidate
                        break
                    except Exception as exc:
                        errors[candidate] = f"{type(exc).__name__}: {exc}"
                if "text" not in row:
                    row["error"] = "; ".join(f"{k}: {v}" for k, v in errors.items())
                elif errors:
                    row["fallback_from"] = errors
            finally:
                if path and not keep_files:
                    path.unlink(missing_ok=True)
            rows.append(row)
        return {
            "chat": entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages",
            "count": len(rows),
            "items": rows,
        }

    async def translate(
        self, to_lang: str, chat: Any = None, message_ids: list | None = None,
        text: str | None = None,
    ) -> dict:
        """Translation by Telegram itself: either chat messages or arbitrary text."""
        if text:
            res = await self.client(
                functions.messages.TranslateTextRequest(
                    to_lang=to_lang,
                    text=[types.TextWithEntities(text=text, entities=[])],
                )
            )
            return {"to": to_lang, "items": [{"text": r.text} for r in res.result]}
        if not (chat and message_ids):
            raise ValueError("text is required, or chat + message_ids")
        ent = await self.resolve(chat)
        ids = [int(i) for i in message_ids]
        if len(ids) > 20:
            raise ValueError("no more than 20 messages at a time")
        res = await self.client(
            functions.messages.TranslateTextRequest(
                to_lang=to_lang,
                peer=await self.client.get_input_entity(ent),
                id=ids,
            )
        )
        return {
            "to": to_lang,
            "items": [
                {"message_id": mid, "text": r.text}
                for mid, r in zip(ids, res.result)
            ],
        }

    async def summarize(
        self, chat: Any, message_ids: list, to_lang: str | None = None
    ) -> dict:
        """A retelling of a long message by Telegram, not by our own tokens.

        The server compresses a post into a few sentences itself and, if asked,
        straight into another language. It works one message per TL call, so the
        list goes through a loop.
        """
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        ids = [int(i) for i in (message_ids or [])]
        if not ids:
            raise ValueError("message_ids is required")
        if len(ids) > 10:
            raise ValueError("no more than 10 messages at a time")
        items = []
        for mid in ids:
            row: dict[str, Any] = {"message_id": mid}
            try:
                res = await self.client(
                    functions.messages.SummarizeTextRequest(
                        peer=peer, id=mid, to_lang=to_lang
                    )
                )
                row["summary"] = getattr(res, "text", None)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            items.append(row)
        return {"chat": entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages",
                "to": to_lang, "items": items}

    # ---------- stories ----------

    def _story_row(self, story, peer_name: str | None = None) -> dict:
        media = getattr(story, "media", None)
        kind = None
        if media is not None:
            name = type(media).__name__
            kind = {"MessageMediaPhoto": "photo", "MessageMediaDocument": "video"}.get(name, name)
        row = {
            "story_id": story.id,
            "from": peer_name,
            "date": _iso(getattr(story, "date", None)),
            "expires": _iso(getattr(story, "expire_date", None)),
            "caption": getattr(story, "caption", None),
            "kind": kind,
            "pinned": bool(getattr(story, "pinned", False)),
            "close_friends": bool(getattr(story, "close_friends", False)),
            "mine": bool(getattr(story, "out", False)),
            "views": getattr(getattr(story, "views", None), "views_count", None),
            "my_reaction": getattr(getattr(story, "sent_reaction", None), "emoticon", None),
        }
        return {k: v for k, v in row.items() if v not in (None, False)}

    async def stories(
        self,
        peer: Any = None,
        mark_read: bool = False,
        download: bool = False,
        limit: int = 20,
    ) -> dict:
        """Stories: without peer the common feed, with peer one person's stories.

        `mark_read` marks them as viewed (the person will see that you watched),
        which is why it is off by default. `download` puts the media on disk.
        """
        out: dict[str, Any] = {}
        rows: list[dict] = []
        targets: list[tuple[Any, str, list]] = []

        if peer is None:
            res = await self.client(functions.stories.GetAllStoriesRequest())
            for ps in getattr(res, "peer_stories", [])[:limit]:
                try:
                    ent = await self.client.get_entity(ps.peer)
                    name = entity_name(ent)
                except Exception:
                    ent, name = ps.peer, None
                targets.append((ent, name, list(ps.stories)))
            out["peers"] = len(targets)
        else:
            ent = await self.client.get_entity(await self.resolve(peer))
            res = await self.client(
                functions.stories.GetPeerStoriesRequest(peer=ent)
            )
            targets.append((ent, entity_name(ent), list(res.stories.stories)))

        for ent, name, items in targets:
            for story in items[:limit]:
                row = self._story_row(story, name)
                if download and getattr(story, "media", None) is not None:
                    target = config.DOWNLOADS / "stories"
                    target.mkdir(parents=True, exist_ok=True)
                    try:
                        path = await self.client.download_media(story.media, file=str(target))
                        row["path"] = str(path) if path else None
                    except Exception as exc:
                        row["download_error"] = f"{type(exc).__name__}: {exc}"
                rows.append(row)
            if mark_read and items:
                self._assert_write()
                try:
                    await self.client(
                        functions.stories.ReadStoriesRequest(
                            peer=ent, max_id=max(s.id for s in items)
                        )
                    )
                except Exception:
                    pass

        out["count"] = len(rows)
        out["items"] = rows
        out["marked_read"] = bool(mark_read)
        return out

    # ---------- devices and sessions ----------

    async def sessions(self, terminate: int | None = None) -> dict:
        """The devices where the account is open. With `terminate` it revokes one
        session.

        Revoking is an irreversible action on the account itself, so it goes
        through the same write guard and lands in the action log.
        """
        if terminate is not None:
            self._assert_write()
            res = await self.client(
                functions.account.ResetAuthorizationRequest(hash=int(terminate))
            )
            return {"terminated": bool(res), "session": int(terminate)}
        res = await self.client(functions.account.GetAuthorizationsRequest())
        rows = []
        for a in res.authorizations:
            rows.append(
                {
                    "session": a.hash,           # 0 = the current one, it cannot be revoked
                    "current": bool(a.current),
                    "device": a.device_model,
                    "platform": a.platform,
                    "system": a.system_version,
                    "app": f"{a.app_name} {a.app_version}".strip(),
                    "official": bool(a.official_app),
                    "ip": a.ip,
                    "country": a.country,
                    "created": _iso(a.date_created),
                    "last_active": _iso(a.date_active),
                    "calls_allowed": not bool(getattr(a, "call_requests_disabled", False)),
                }
            )
        rows.sort(key=lambda r: (not r["current"], r["last_active"] or ""), reverse=False)
        return {
            "count": len(rows),
            "auto_terminate_days": getattr(res, "authorization_ttl_days", None),
            "sessions": rows,
        }

    # ---------- stickers, gifs, forums, admin log ----------

    STICKER_SCOPES = ("sets", "set", "faved", "recent", "gifs")

    @staticmethod
    def _doc_row(doc, i: int) -> dict:
        emoji, name = None, None
        for attr in getattr(doc, "attributes", []):
            emoji = getattr(attr, "alt", None) or emoji
            name = getattr(attr, "file_name", None) or name
        return {
            "index": i,
            "id": doc.id,
            "emoji": emoji,
            "name": name,
            "mime": getattr(doc, "mime_type", None),
            "size": getattr(doc, "size", None),
        }

    async def _sticker_docs(self, scope: str, set: str | None):
        """The documents of one pack, of the favorites, the recent ones or the
        saved gifs."""
        if scope == "set":
            if not set:
                raise ValueError("the short_name of the pack is needed in the set parameter")
            res = await self.client(
                functions.messages.GetStickerSetRequest(
                    stickerset=types.InputStickerSetShortName(short_name=str(set)), hash=0
                )
            )
            return res.documents, res.set
        raw = await self.client(
            {
                "faved": functions.messages.GetFavedStickersRequest,
                "recent": functions.messages.GetRecentStickersRequest,
                "gifs": functions.messages.GetSavedGifsRequest,
            }[scope](hash=0)
        )
        return (getattr(raw, "stickers", None) or getattr(raw, "gifs", [])), None

    async def stickers(self, scope: str = "sets", set: str | None = None, limit: int = 60) -> dict:
        """The account sticker packs, favorite and recent stickers, saved gifs."""
        if scope not in self.STICKER_SCOPES:
            raise ValueError(f"scope: {', '.join(self.STICKER_SCOPES)}")
        if scope == "sets":
            res = await self.client(functions.messages.GetAllStickersRequest(hash=0))
            return {
                "count": len(res.sets),
                "sets": [
                    {
                        "title": s.title,
                        "short_name": s.short_name,
                        "count": s.count,
                        "animated": bool(getattr(s, "animated", False)),
                        "link": f"https://t.me/addstickers/{s.short_name}",
                    }
                    for s in res.sets[:limit]
                ],
            }
        docs, meta = await self._sticker_docs(scope, set)
        out = {
            "scope": scope,
            "count": len(docs),
            "items": [self._doc_row(d, i) for i, d in enumerate(docs[:limit])],
        }
        if meta is not None:
            out["set"] = meta.title
            out["short_name"] = meta.short_name
        return out

    async def _pick_document(self, scope: str, set: str | None, index: int, emoji: str | None):
        docs, _ = await self._sticker_docs(scope, set)
        if not docs:
            raise ValueError(f"There is nothing in {scope}")
        if emoji:
            for doc in docs:
                if self._doc_row(doc, 0)["emoji"] == emoji:
                    return doc
            raise ValueError(f"There is no sticker with the emoji {emoji} here")
        if not 0 <= index < len(docs):
            raise ValueError(f"index is out of range, there are {len(docs)} in total")
        return docs[index]

    async def topics(self, chat: Any, limit: int = 50, query: str | None = None) -> dict:
        """Forum topics: id, title, unread, whether it is closed."""
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        res = await self.client(
            functions.messages.GetForumTopicsRequest(
                peer=peer, offset_date=None, offset_id=0, offset_topic=0,
                limit=limit, q=query,
            )
        )
        rows = []
        for tpc in res.topics:
            if not isinstance(tpc, types.ForumTopic):
                continue
            rows.append(
                {
                    "id": tpc.id,
                    "title": tpc.title,
                    "unread": tpc.unread_count,
                    "mentions": tpc.unread_mentions_count,
                    "closed": bool(tpc.closed),
                    "pinned": bool(tpc.pinned),
                    "hidden": bool(getattr(tpc, "hidden", False)),
                    "top_message": tpc.top_message,
                    "created": _iso(tpc.date),
                }
            )
        return {"chat": entity_name(await self.client.get_entity(ent)), "count": len(rows), "topics": rows}

    async def admin_log(
        self, chat: Any, limit: int = 50, query: str = "", admins: list | None = None
    ) -> dict:
        """The admin log: who did what in a group or a channel."""
        ent = await self.resolve(chat)
        channel = await self.client.get_input_entity(ent)
        admin_inputs = None
        if admins:
            admin_inputs = [
                utils.get_input_user(await self.client.get_input_entity(await self.resolve(a)))
                for a in admins
            ]
        res = await self.client(
            functions.channels.GetAdminLogRequest(
                channel=channel, q=query or "", max_id=0, min_id=0,
                limit=limit, events_filter=None, admins=admin_inputs,
            )
        )
        users = {u.id: entity_name(u) for u in res.users}
        rows = []
        for ev in res.events:
            action = type(ev.action).__name__.replace("ChannelAdminLogEventAction", "")
            row = {
                "id": ev.id,
                "date": _iso(ev.date),
                "by": users.get(utils.get_peer_id(ev.user_id) if not isinstance(ev.user_id, int) else ev.user_id)
                or str(ev.user_id),
                "action": action,
            }
            for field in ("new_value", "prev_value", "message", "new_title"):
                val = getattr(ev.action, field, None)
                if isinstance(val, str) and val:
                    row[field] = val[:200]
            msg = getattr(ev.action, "message", None)
            if msg is not None and hasattr(msg, "message"):
                row["message"] = (msg.message or "")[:200]
            rows.append(row)
        return {"chat": entity_name(await self.client.get_entity(ent)), "count": len(rows), "events": rows}

    async def bot_info(self, bot: Any, lang_code: str = "") -> dict:
        """What your bot has on record: name, description, short description, commands."""
        ent = await self.resolve(bot)
        inp = utils.get_input_user(await self.client.get_input_entity(ent))
        res = await self.client(
            functions.bots.GetBotInfoRequest(bot=inp, lang_code=lang_code or "")
        )
        out = {
            "bot": entity_name(await self.client.get_entity(ent)),
            "name": getattr(res, "name", None),
            "about": getattr(res, "about", None),
            "description": getattr(res, "description", None),
        }
        token = config.bot_token()
        if token and str(getattr(await self.client.get_entity(ent), "id", "")) == token.split(":")[0]:
            from .alerts import BotChannel

            channel = BotChannel(token=token)
            try:
                out["commands"] = await channel.call("getMyCommands")
            finally:
                await channel.close()
        return out

    async def cache_clear(self, downloads: bool = False) -> dict:
        """Drop the cache of chat names (and, if wanted, the downloads folder)."""
        cached = len(self._dialog_cache)
        self._dialog_cache = []
        self._dialog_cache_at = 0.0
        removed = 0
        if downloads:
            for p in config.DOWNLOADS.glob("*"):
                if p.is_file():
                    p.unlink()
                    removed += 1
        return {"dialog_index_dropped": cached, "downloads_removed": removed}

    # ---------- write operations ----------

    def _assert_write(self) -> None:
        if not config.allow_write():
            raise GuardError("Write actions are disabled (TG_ALLOW_WRITE=0 in .env).")

    async def _send_key(self, ent) -> str:
        """The chat key for the anti-spam counter."""
        if ent == "me":
            return "me"
        return str(utils.get_peer_id(await self.client.get_entity(ent)))

    async def send(
        self,
        chat: Any,
        text: str,
        reply_to: int | None = None,
        silent: bool = False,
        link_preview: bool = True,
    ) -> dict:
        self._assert_write()
        if len(text) > config.LIMITS["max_text_len"]:
            raise GuardError("Message longer than 4096 characters; split it.")
        ent = await self.resolve(chat)
        key = str(utils.get_peer_id(await self.client.get_entity(ent)) if ent != "me" else "me")
        self.guard.check_send(key)
        try:
            msg = await self.client.send_message(
                ent, text, reply_to=reply_to, silent=silent, link_preview=link_preview
            )
        except FloodWaitError as exc:
            raise GuardError(f"Telegram flood-wait: retry in {exc.seconds}s") from exc
        self.guard.record_send(key)
        return {"sent": True, "chat": key, "message_id": msg.id, "date": _iso(msg.date)}

    async def send_file(
        self,
        chat: Any,
        path: Any,
        caption: str = "",
        voice: bool = False,
        silent: bool = False,
    ) -> dict:
        """One file, an album (a list of paths) or a voice message (voice=True)."""
        self._assert_write()
        raw = [path] if isinstance(path, (str, Path)) else list(path)
        if not raw:
            raise ValueError("at least one file is required")
        files = []
        for item in raw:
            p = Path(str(item)).expanduser()
            if not p.exists():
                raise ValueError(f"File not found: {p}")
            files.append(p)
        ent = await self.resolve(chat)
        key = await self._send_key(ent)
        self.guard.check_send(key)
        sent = await self.client.send_file(
            ent,
            [str(p) for p in files] if len(files) > 1 else str(files[0]),
            caption=caption,
            voice_note=voice,
            silent=silent,
        )
        self.guard.record_send(key)
        msgs = sent if isinstance(sent, list) else [sent]
        return {
            "sent": True,
            "message_ids": [m.id for m in msgs],
            "files": [p.name for p in files],
            "album": len(files) > 1,
        }

    async def send_location(self, chat: Any, latitude: float, longitude: float) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        key = await self._send_key(ent)
        self.guard.check_send(key)
        media = types.InputMediaGeoPoint(
            geo_point=types.InputGeoPoint(lat=float(latitude), long=float(longitude))
        )
        msg = await self.client.send_file(ent, media)
        self.guard.record_send(key)
        return {"sent": True, "message_id": msg.id, "lat": latitude, "lon": longitude}

    async def schedule(
        self, chat: Any, text: str, when: Any, reply_to: int | None = None
    ) -> dict:
        """Scheduled sending: when is an ISO time or +30m / +2h / +3d."""
        self._assert_write()
        if len(text) > config.LIMITS["max_text_len"]:
            raise GuardError("Message longer than 4096 characters; split it.")
        at = _parse_when(when)
        if at <= datetime.now(timezone.utc) + timedelta(seconds=10):
            raise ValueError("The earliest one can schedule is 10 seconds ahead")
        ent = await self.resolve(chat)
        key = await self._send_key(ent)
        self.guard.check_send(key)
        try:
            msg = await self.client.send_message(ent, text, schedule=at, reply_to=reply_to)
        except FloodWaitError as exc:
            raise GuardError(f"Telegram flood-wait: retry in {exc.seconds}s") from exc
        self.guard.record_send(key)
        return {"scheduled": True, "message_id": msg.id, "at": _iso(at), "chat": key}

    async def draft(
        self, chat: Any, text: str | None = None, reply_to: int | None = None,
        clear: bool = False,
    ) -> dict:
        """A draft: the agent writes it, you send it yourself from Telegram."""
        self._assert_write()
        ent = await self.resolve(chat)
        d = await self.client.get_drafts(ent)
        if clear:
            await d.delete()
            return {"cleared": True}
        if text is None:
            raise ValueError("text is required, or clear=true")
        if len(text) > config.LIMITS["max_text_len"]:
            raise GuardError("Draft longer than 4096 characters; split it.")
        await d.set_message(text, reply_to=reply_to)
        return {
            "saved": True,
            "text": text[:200],
            "note": "the draft is visible in Telegram, send it by hand",
        }

    async def react(
        self, chat: Any, message_id: int, emoji: Any = None, big: bool = False
    ) -> dict:
        """Put a reaction; without emoji it removes your own.

        `emoji` is either the character itself, or a custom emoji id (Premium), or
        a list: Telegram Premium allows several reactions on one message.
        """
        self._assert_write()
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        wanted = [] if emoji is None else (emoji if isinstance(emoji, list) else [emoji])
        if len(wanted) > 3:
            raise ValueError("Telegram allows no more than three reactions on a message")
        try:
            await self.client(
                functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message_id),
                    reaction=[_input_reaction(e) for e in wanted],
                    big=big,
                    add_to_recent=True,
                )
            )
        except Exception as exc:
            if "REACTION_INVALID" in str(exc):
                allowed = await self.reactions_allowed(chat)
                raise ValueError(
                    f"such a reaction cannot be put here. Allowed: {allowed}"
                ) from exc
            raise
        return {
            "message_id": int(message_id),
            "reaction": wanted or None,
            "removed": not wanted,
        }

    async def reactions_allowed(self, chat: Any) -> str:
        """Which reactions this chat allows — for a comprehensible error."""
        try:
            ent = await self.resolve(chat)
            full = await self.client(
                functions.channels.GetFullChannelRequest(await self.client.get_entity(ent))
            )
            av = getattr(full.full_chat, "available_reactions", None)
            if isinstance(av, types.ChatReactionsAll):
                return "any"
            if isinstance(av, types.ChatReactionsSome):
                return ", ".join(str(reaction_of(r)) for r in av.reactions) or "none"
            if isinstance(av, types.ChatReactionsNone):
                return "none, reactions are switched off in this chat"
        except Exception:
            pass
        return "unknown"

    async def pin_message(
        self, chat: Any, message_id: int, unpin: bool = False, notify: bool = False
    ) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        if unpin:
            await self.client.unpin_message(ent, int(message_id))
        else:
            await self.client.pin_message(ent, int(message_id), notify=notify)
        return {"pinned": not unpin, "message_id": int(message_id)}

    async def poll(
        self,
        chat: Any,
        question: str,
        options: list,
        multiple: bool = False,
        quiz_answer: int | None = None,
        anonymous: bool = True,
    ) -> dict:
        """A poll. quiz_answer is the index of the right option, then it is a quiz."""
        self._assert_write()
        opts = [str(o) for o in (options or [])]
        if not 2 <= len(opts) <= 10:
            raise ValueError("A poll takes 2 to 10 options")
        if quiz_answer is not None and not 0 <= int(quiz_answer) < len(opts):
            raise ValueError("quiz_answer is out of the range of options")
        ent = await self.resolve(chat)
        key = await self._send_key(ent)
        self.guard.check_send(key)
        poll = types.Poll(
            id=0,
            hash=0,
            question=types.TextWithEntities(text=question, entities=[]),
            answers=[
                types.PollAnswer(
                    text=types.TextWithEntities(text=o, entities=[]), option=bytes([i])
                )
                for i, o in enumerate(opts)
            ],
            multiple_choice=bool(multiple) and quiz_answer is None,
            public_voters=not anonymous,
            quiz=quiz_answer is not None,
        )
        media = types.InputMediaPoll(
            poll=poll,
            correct_answers=[bytes([int(quiz_answer)])] if quiz_answer is not None else None,
        )
        msg = await self.client.send_file(ent, media)
        self.guard.record_send(key)
        return {"sent": True, "message_id": msg.id, "question": question, "options": opts}

    async def click(self, chat: Any, message_id: int, button: Any = None) -> dict:
        """Buttons under a bot message: without button show them, with button press one."""
        ent = await self.resolve(chat)
        msg = await self.client.get_messages(ent, ids=int(message_id))
        if msg is None:
            raise ValueError(f"There is no message {message_id} in this chat")
        buttons = _buttons(msg)
        if button is None:
            return {
                "message_id": msg.id,
                "buttons": buttons or [],
                "note": "pass button — the number of the button or its text",
            }
        self._assert_write()
        if not buttons:
            raise ValueError("This message has no buttons")
        try:
            idx = int(button)
        except (TypeError, ValueError):
            match = [b for b in buttons if str(button).lower() in b["text"].lower()]
            if not match:
                raise ValueError(
                    f"There is no button {button!r}. There is: "
                    + ", ".join(b["text"] for b in buttons)
                ) from None
            target = match[0]
        else:
            if not 0 <= idx < len(buttons):
                raise ValueError(f"There is no button {idx}, there are {len(buttons)}")
            target = buttons[idx]
        res = await msg.click(target["row"], target["col"])
        return {
            "clicked": target["text"],
            "url": target.get("url"),
            "answer": getattr(res, "message", None),
        }

    async def send_sticker(
        self,
        chat: Any,
        scope: str = "faved",
        set: str | None = None,
        index: int = 0,
        emoji: str | None = None,
        reply_to: int | None = None,
    ) -> dict:
        """Send a sticker or a gif: from a pack (scope=set + short_name), from the
        favorites, the recent ones or the saved gifs."""
        self._assert_write()
        if scope not in ("set", "faved", "recent", "gifs"):
            raise ValueError("scope: set, faved, recent, gifs")
        doc = await self._pick_document(scope, set, index, emoji)
        ent = await self.resolve(chat)
        key = await self._send_key(ent)
        self.guard.check_send(key)
        msg = await self.client.send_file(ent, doc, reply_to=reply_to)
        self.guard.record_send(key)
        row = self._doc_row(doc, index)
        return {"sent": True, "message_id": msg.id, "emoji": row["emoji"], "scope": scope}

    async def topic_create(
        self, chat: Any, title: str, icon_emoji_id: int | None = None
    ) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        res = await self.client(
            functions.messages.CreateForumTopicRequest(
                peer=peer, title=title, icon_emoji_id=icon_emoji_id
            )
        )
        # the topic id is the id of the service message about its creation
        topic_id = None
        for upd in getattr(res, "updates", []):
            msg = getattr(upd, "message", None)
            if msg is not None and getattr(msg, "id", None):
                topic_id = msg.id
                break
        return {"created": True, "title": title, "topic_id": topic_id}

    async def topic_edit(
        self,
        chat: Any,
        topic_id: int,
        title: str | None = None,
        closed: bool | None = None,
        hidden: bool | None = None,
        pinned: bool | None = None,
    ) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        changed: dict[str, Any] = {}
        # Telegram answers TOPIC_CLOSE_SEPARATELY if closing goes together with
        # renaming, so every change goes as a separate request.
        for field, value in (("title", title), ("hidden", hidden), ("closed", closed)):
            if value is None:
                continue
            await self.client(
                functions.messages.EditForumTopicRequest(
                    peer=peer, topic_id=int(topic_id), **{field: value}
                )
            )
            changed[field] = value
        if pinned is not None:
            await self.client(
                functions.messages.UpdatePinnedForumTopicRequest(
                    peer=peer, topic_id=int(topic_id), pinned=bool(pinned)
                )
            )
            changed["pinned"] = bool(pinned)
        if not changed:
            raise ValueError("nothing to change: title, closed, hidden or pinned")
        return {"topic_id": int(topic_id), "updated": changed}

    async def bot_edit(
        self,
        bot: Any,
        name: str | None = None,
        about: str | None = None,
        description: str | None = None,
        commands: list | None = None,
        lang_code: str = "",
    ) -> dict:
        """The name, the "about", the description and the command list of a bot you own."""
        self._assert_write()
        ent = await self.resolve(bot)
        entity = await self.client.get_entity(ent)
        changed: dict[str, Any] = {}
        if name or about or description:
            await self.client(
                functions.bots.SetBotInfoRequest(
                    bot=utils.get_input_user(await self.client.get_input_entity(ent)),
                    lang_code=lang_code or "",
                    name=name,
                    about=about,
                    description=description,
                )
            )
            changed.update(
                {k: v for k, v in
                 (("name", name), ("about", about), ("description", description))
                 if v}
            )
        if commands is not None:
            # MTProto sets commands only on behalf of the bot itself, so its token
            # is needed here. It exists for exactly one bot: this agent's.
            token = config.bot_token()
            if not token or str(getattr(entity, "id", "")) != token.split(":")[0]:
                raise ValueError(
                    "Commands are changed with the bot's own token. For other people's "
                    "bots use @BotFather → /setcommands."
                )
            from .alerts import BotChannel

            channel = BotChannel(token=token)
            try:
                await channel.call(
                    "setMyCommands",
                    commands=[
                        {"command": str(c["command"]).lstrip("/"),
                         "description": str(c["description"])}
                        for c in commands
                    ],
                )
            finally:
                await channel.close()
            changed["commands"] = len(commands)
        if not changed:
            raise ValueError("nothing to change: name, about, description or commands")
        return {"bot": entity_name(entity), "updated": changed}

    async def block(self, user: Any, unblock: bool = False) -> dict:
        self._assert_write()
        ent = await self.resolve(user)
        peer = await self.client.get_input_entity(ent)
        req = functions.contacts.UnblockRequest if unblock else functions.contacts.BlockRequest
        await self.client(req(id=peer))
        return {
            "blocked": not unblock,
            "user": entity_name(await self.client.get_entity(ent)),
        }

    async def contact_edit(
        self,
        phone: str | None = None,
        name: str | None = None,
        last_name: str = "",
        user: Any = None,
        delete: bool = False,
        note: str | None = None,
    ) -> dict:
        """Add a contact by number, delete one, or leave yourself a note about a person."""
        self._assert_write()
        if note is not None:
            if user is None:
                raise ValueError("a note needs user")
            ent = await self.resolve(user)
            await self.client(
                functions.contacts.UpdateContactNoteRequest(
                    id=await self.client.get_input_entity(ent),
                    note=types.TextWithEntities(text=str(note), entities=[]),
                )
            )
            return {
                "user": entity_name(await self.client.get_entity(ent)),
                "note": str(note),
                "visible_to": "you alone",
            }
        if delete:
            if user is None:
                raise ValueError("deletion needs user")
            ent = await self.resolve(user)
            await self.client(
                functions.contacts.DeleteContactsRequest(
                    id=[await self.client.get_input_entity(ent)]
                )
            )
            return {"deleted": entity_name(await self.client.get_entity(ent))}
        if not (phone and name):
            raise ValueError("phone and name are required")
        res = await self.client(
            functions.contacts.ImportContactsRequest(
                contacts=[
                    types.InputPhoneContact(
                        client_id=0, phone=str(phone), first_name=name,
                        last_name=last_name or "",
                    )
                ]
            )
        )
        if not res.users:
            return {
                "added": False,
                "reason": "the number has no Telegram, or it is closed to being added by number",
            }
        u = res.users[0]
        return {
            "added": True,
            "id": u.id,
            "name": entity_name(u),
            "username": u.username,
            "link": dm_link(u),
        }

    # ---------- groups and channels ----------

    async def create_group(
        self, title: str, users: list | None = None, kind: str = "group", about: str = ""
    ) -> dict:
        """Create a supergroup or a channel; users is whom to invite right away."""
        self._assert_write()
        if kind not in ("group", "channel"):
            raise ValueError("kind: group or channel")
        res = await self.client(
            functions.channels.CreateChannelRequest(
                title=title,
                about=about or "",
                megagroup=kind == "group",
                broadcast=kind == "channel",
            )
        )
        chat = res.chats[0]
        invited = 0
        if users:
            inputs = [
                await self.client.get_input_entity(await self.resolve(u)) for u in users
            ]
            await self.client(
                functions.channels.InviteToChannelRequest(channel=chat, users=inputs)
            )
            invited = len(inputs)
        return {
            "created": True,
            "id": utils.get_peer_id(chat),
            "title": title,
            "kind": kind,
            "invited": invited,
        }

    async def invite(
        self, chat: Any, users: list | None = None, link: bool = False, revoke: bool = False
    ) -> dict:
        """Invite people into a chat and/or get an invite link."""
        self._assert_write()
        ent = await self.resolve(chat)
        entity = await self.client.get_entity(ent)
        out: dict[str, Any] = {"chat": entity_name(entity)}
        if link or revoke:
            res = await self.client(
                functions.messages.ExportChatInviteRequest(
                    peer=await self.client.get_input_entity(ent),
                    legacy_revoke_permanent=revoke,
                )
            )
            out["link"] = getattr(res, "link", None)
            if revoke:
                out["old_link_revoked"] = True
        if users:
            inputs = [
                await self.client.get_input_entity(await self.resolve(u)) for u in users
            ]
            if isinstance(entity, types.Channel):
                await self.client(
                    functions.channels.InviteToChannelRequest(channel=entity, users=inputs)
                )
            else:
                for u in inputs:
                    await self.client(
                        functions.messages.AddChatUserRequest(
                            chat_id=entity.id, user_id=u, fwd_limit=10
                        )
                    )
            out["invited"] = len(inputs)
        if len(out) == 1:
            raise ValueError("users or link=true is required")
        return out

    async def moderate(self, chat: Any, user: Any, action: str) -> dict:
        """kick, ban, unban, promote, demote, approve, decline (join requests)."""
        self._assert_write()
        act = str(action).lower().strip()
        ent = await self.resolve(chat)
        target = await self.resolve(user)
        if act in ("approve", "decline"):
            await self.client(
                functions.messages.HideChatJoinRequestRequest(
                    peer=await self.client.get_input_entity(ent),
                    user_id=await self.client.get_input_entity(target),
                    approved=act == "approve",
                )
            )
        elif act == "kick":
            await self.client.kick_participant(ent, target)
        elif act == "ban":
            await self.client.edit_permissions(ent, target, view_messages=False)
        elif act == "unban":
            await self.client.edit_permissions(ent, target, view_messages=True)
        elif act == "promote":
            await self.client.edit_admin(ent, target, is_admin=True, add_admins=False)
        elif act == "demote":
            await self.client.edit_admin(ent, target, is_admin=False)
        else:
            raise ValueError(
                "action: kick, ban, unban, promote, demote, approve, decline"
            )
        return {
            "action": act,
            "user": entity_name(await self.client.get_entity(target)),
            "chat": entity_name(await self.client.get_entity(ent)),
        }

    # What members may do by default. In Telegram these are "bans", so a true
    # value from the outside means "allowed" and is inverted here.
    PERMISSION_FLAGS = (
        "send_messages", "send_media", "send_stickers", "send_gifs", "send_polls",
        "embed_links", "change_info", "invite_users", "pin_messages", "manage_topics",
    )

    async def chat_edit(
        self,
        chat: Any,
        title: str | None = None,
        about: str | None = None,
        photo: str | None = None,
        slowmode: int | None = None,
        permissions: dict | None = None,
        forum: bool | None = None,
    ) -> dict:
        """Title, description, avatar, slowmode, member rights and forum mode."""
        self._assert_write()
        ent = await self.resolve(chat)
        entity = await self.client.get_entity(ent)
        is_channel = isinstance(entity, types.Channel)
        changed: dict[str, Any] = {}
        if title:
            if is_channel:
                await self.client(
                    functions.channels.EditTitleRequest(channel=entity, title=title)
                )
            else:
                await self.client(
                    functions.messages.EditChatTitleRequest(chat_id=entity.id, title=title)
                )
            changed["title"] = title
        if about is not None:
            await self.client(
                functions.messages.EditChatAboutRequest(
                    peer=await self.client.get_input_entity(ent), about=about
                )
            )
            changed["about"] = about
        if photo:
            p = Path(photo).expanduser()
            if not p.exists():
                raise ValueError(f"File not found: {p}")
            uploaded = await self.client.upload_file(str(p))
            new_photo = types.InputChatUploadedPhoto(file=uploaded)
            if is_channel:
                await self.client(
                    functions.channels.EditPhotoRequest(channel=entity, photo=new_photo)
                )
            else:
                await self.client(
                    functions.messages.EditChatPhotoRequest(
                        chat_id=entity.id, photo=new_photo
                    )
                )
            changed["photo"] = p.name
        if slowmode is not None:
            if not is_channel:
                raise ValueError("Only supergroups have slowmode")
            await self.client(
                functions.channels.ToggleSlowModeRequest(
                    channel=entity, seconds=int(slowmode)
                )
            )
            changed["slowmode_sec"] = int(slowmode)
        if permissions:
            unknown = set(permissions) - set(self.PERMISSION_FLAGS)
            if unknown:
                raise ValueError(
                    f"Unknown rights: {', '.join(sorted(unknown))}. "
                    f"Allowed: {', '.join(self.PERMISSION_FLAGS)}"
                )
            banned = types.ChatBannedRights(
                until_date=None,
                **{flag: not bool(permissions[flag]) for flag in permissions},
            )
            await self.client(
                functions.messages.EditChatDefaultBannedRightsRequest(
                    peer=await self.client.get_input_entity(ent), banned_rights=banned
                )
            )
            changed["permissions"] = permissions
        if forum is not None:
            if not is_channel:
                raise ValueError("Topics are switched on in supergroups only")
            await self.client(
                functions.channels.ToggleForumRequest(
                    channel=entity, enabled=bool(forum), tabs=False
                )
            )
            changed["forum"] = bool(forum)
        if not changed:
            raise ValueError(
                "nothing to change: title, about, photo, slowmode, permissions or forum"
            )
        return {"chat": entity_name(entity), "updated": changed}

    async def leave(self, chat: Any, delete: bool = False) -> dict:
        """Leave a group or a channel. delete=true on a DM wipes the conversation."""
        self._assert_write()
        ent = await self.resolve(chat)
        entity = await self.client.get_entity(ent) if ent != "me" else None
        if entity is None:
            raise ValueError("Saved Messages cannot be left")
        if isinstance(entity, types.User) and not delete:
            raise ValueError(
                "This is a DM, one does not leave it. delete=true will delete the "
                "conversation on your side — irreversibly, so only on an explicit "
                "request."
            )
        name = entity_name(entity)
        await self.client.delete_dialog(ent)
        return {"left": name, "deleted_history": bool(isinstance(entity, types.User))}

    # The auto-rules of a folder: the same checkboxes as in the app.
    FOLDER_RULES = (
        "contacts", "non_contacts", "groups", "broadcasts", "bots",
        "exclude_muted", "exclude_read", "exclude_archived",
    )

    async def folder_edit(
        self,
        folder: Any = None,
        add: list | None = None,
        remove: list | None = None,
        create: str | None = None,
        delete: bool = False,
        rename: str | None = None,
        emoji: str | None = None,
        rules: dict | None = None,
        exclude: list | None = None,
    ) -> dict:
        """Folders as a whole: create, delete, rename, set auto-rules, fill.

        Without `create` and without `delete` it works on an existing folder:
        `add`/`remove` move chats around, `exclude` adds exceptions, `rules`
        switches on the same checkboxes as in the app (all contacts, all groups,
        hide read ones).
        """
        self._assert_write()
        res = await self.client(functions.messages.GetDialogFiltersRequest())
        existing = [f for f in res.filters if getattr(f, "id", None) is not None]

        if create:
            taken = {f.id for f in existing}
            # the folder id is picked by the client; 0 and 1 are taken by the
            # system "All chats" and the archive
            new_id = next(i for i in range(2, 256) if i not in taken)
            include, excluded = [], []
            for chat in add or []:
                include.append(await self.client.get_input_entity(await self.resolve(chat)))
            for chat in exclude or []:
                excluded.append(await self.client.get_input_entity(await self.resolve(chat)))
            flags = {}
            for key, value in (rules or {}).items():
                if key not in self.FOLDER_RULES:
                    raise ValueError(f"folder rules: {', '.join(self.FOLDER_RULES)}")
                flags[key] = bool(value)
            fl = types.DialogFilter(
                id=new_id,
                title=types.TextWithEntities(text=str(create)[:12], entities=[]),
                pinned_peers=[], include_peers=include, exclude_peers=excluded,
                emoticon=emoji, **flags,
            )
            await self.client(
                functions.messages.UpdateDialogFilterRequest(id=new_id, filter=fl)
            )
            return {
                "created": str(create)[:12],
                "id": new_id,
                "chats": len(include),
                "excluded": len(excluded),
                "rules": flags or None,
                "emoji": emoji,
            }

        if folder is None:
            raise ValueError("a folder (id or title) is required, or create")

        want = str(folder).strip().lower()
        target = None
        for fl in res.filters:
            fid = getattr(fl, "id", None)
            raw_title = getattr(fl, "title", None)
            title = getattr(raw_title, "text", raw_title)
            if fid is None or title is None:
                continue  # "All chats" is a system one, nothing to change
            if want == str(fid) or want == str(title).lower() or want in str(title).lower():
                target = fl
                break
        if target is None or not hasattr(target, "include_peers"):
            raise ValueError(f"Folder {folder!r} was not found")

        title_now = getattr(getattr(target, "title", None), "text", None)
        if delete:
            await self.client(
                functions.messages.UpdateDialogFilterRequest(id=target.id, filter=None)
            )
            return {"deleted": title_now, "id": target.id,
                    "note": "the chats themselves stayed in place, only the folder is gone"}

        known = {utils.get_peer_id(p) for p in list(target.include_peers) + list(target.pinned_peers)}
        added, removed, changed = [], [], {}

        if rename:
            target.title = types.TextWithEntities(text=str(rename)[:12], entities=[])
            changed["title"] = str(rename)[:12]
        if emoji is not None:
            target.emoticon = emoji or None
            changed["emoji"] = emoji or None
        for key, value in (rules or {}).items():
            if key not in self.FOLDER_RULES:
                raise ValueError(f"folder rules: {', '.join(self.FOLDER_RULES)}")
            setattr(target, key, bool(value))
            changed[key] = bool(value)
        for chat in exclude or []:
            ent = await self.resolve(chat)
            peer = await self.client.get_input_entity(ent)
            if utils.get_peer_id(peer) not in {utils.get_peer_id(p) for p in target.exclude_peers}:
                target.exclude_peers.append(peer)
                changed.setdefault("excluded", []).append(
                    entity_name(await self.client.get_entity(ent))
                )
        for chat in add or []:
            ent = await self.resolve(chat)
            peer = await self.client.get_input_entity(ent)
            if utils.get_peer_id(peer) in known:
                continue
            target.include_peers.append(peer)
            known.add(utils.get_peer_id(peer))
            added.append(entity_name(await self.client.get_entity(ent)))
        for chat in remove or []:
            ent = await self.resolve(chat)
            pid = utils.get_peer_id(await self.client.get_entity(ent))
            before = len(target.include_peers) + len(target.pinned_peers)
            target.include_peers = [
                p for p in target.include_peers if utils.get_peer_id(p) != pid
            ]
            target.pinned_peers = [
                p for p in target.pinned_peers if utils.get_peer_id(p) != pid
            ]
            if before != len(target.include_peers) + len(target.pinned_peers):
                removed.append(entity_name(await self.client.get_entity(ent)))
        if not (added or removed or changed):
            raise ValueError(
                "nothing to change: pass add, remove, exclude, rename, emoji, rules or delete"
            )
        await self.client(
            functions.messages.UpdateDialogFilterRequest(id=target.id, filter=target)
        )
        out = {
            "folder": getattr(getattr(target, "title", None), "text", None),
            "id": target.id,
            "added": added,
            "removed": removed,
            "total": len(target.include_peers) + len(target.pinned_peers),
        }
        if changed:
            out["changed"] = changed
        return out

    async def edit(self, chat: Any, message_id: int, text: str) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        msg = await self.client.edit_message(ent, message_id, text)
        return {"edited": True, "message_id": msg.id}

    async def delete(self, chat: Any, message_ids: list[int], revoke: bool = True) -> dict:
        self._assert_write()
        ids = [int(i) for i in message_ids]
        self.guard.check_delete(len(ids))
        ent = await self.resolve(chat)
        await self.client.delete_messages(ent, ids, revoke=revoke)
        self.guard.record_delete(len(ids))
        return {"deleted": len(ids), "revoke": revoke}

    async def forward(self, from_chat: Any, message_ids: list[int], to_chat: Any) -> dict:
        self._assert_write()
        src = await self.resolve(from_chat)
        dst = await self.resolve(to_chat)
        key = str(utils.get_peer_id(await self.client.get_entity(dst)) if dst != "me" else "me")
        self.guard.check_send(key)
        res = await self.client.forward_messages(dst, [int(i) for i in message_ids], src)
        self.guard.record_send(key)
        return {"forwarded": len(res) if isinstance(res, list) else 1}

    async def mark_read(
        self, chat: Any, clear_mentions: bool = True, unread: bool = False
    ) -> dict:
        """Read a whole chat, or the other way round, put the "unread" mark back on it."""
        self._assert_write()
        ent = await self.resolve(chat)
        if unread:
            peer = await self.client.get_input_entity(ent)
            await self.client(
                functions.messages.MarkDialogUnreadRequest(
                    peer=types.InputDialogPeer(peer=peer), unread=True
                )
            )
            return {"unread": True}
        await self.client.send_read_acknowledge(ent, clear_mentions=clear_mentions)
        return {"read": True}

    NOTIFY_SCOPES = {
        "users": types.InputNotifyUsers,
        "groups": types.InputNotifyChats,
        "channels": types.InputNotifyBroadcasts,
    }

    @staticmethod
    def _notify_row(s) -> dict:
        until = getattr(s, "mute_until", None)
        muted = bool(until and until > datetime.now(timezone.utc))
        return {
            "muted": muted,
            "muted_until": _iso(until) if muted else None,
            "sound": None if getattr(s, "silent", None) is None else (not s.silent),
            "previews": getattr(s, "show_previews", None),
            "stories_muted": getattr(s, "stories_muted", None),
        }

    async def notify(
        self,
        chat: Any = None,
        scope: str | None = None,
        mute: bool | None = None,
        hours: int | None = None,
        sound: bool | None = None,
        previews: bool | None = None,
        stories: bool | None = None,
        exceptions: bool = False,
    ) -> dict:
        """Notifications: look and set — for one chat or for a whole category.

        Without arguments it shows the defaults for DMs, groups and channels.
        `scope` changes the default of a whole category ("switch off the
        notifications of all channels"), `chat` of a single chat.
        `exceptions=True` lists the chats whose settings differ from the default.
        """
        if scope and scope not in self.NOTIFY_SCOPES:
            raise ValueError(f"scope: {', '.join(self.NOTIFY_SCOPES)}")

        if exceptions:
            # compare_sound / compare_stories narrow the selection to chats where
            # exactly the sound or exactly the stories differ. We need every
            # difference, so both flags are off: with them Telegram hands out an
            # empty list.
            res = await self.client(
                functions.account.GetNotifyExceptionsRequest(
                    compare_sound=False, compare_stories=False
                )
            )
            names = {utils.get_peer_id(e): entity_name(e)
                     for e in list(getattr(res, "chats", [])) + list(getattr(res, "users", []))}
            rows = []
            for upd in getattr(res, "updates", []):
                peer = getattr(getattr(upd, "peer", None), "peer", None)
                settings = getattr(upd, "notify_settings", None)
                if peer is None or settings is None:
                    continue
                pid = utils.get_peer_id(peer)
                rows.append({"chat": names.get(pid), "id": pid, **self._notify_row(settings)})
            return {"count": len(rows), "exceptions": rows}

        changes = {
            "mute": mute, "hours": hours, "sound": sound,
            "previews": previews, "stories": stories,
        }
        if all(v is None for v in changes.values()):
            # We change nothing — which means the question is how things stand
            if chat is not None:
                ent = await self.resolve(chat)
                peer = types.InputNotifyPeer(peer=await self.client.get_input_entity(ent))
                s = await self.client(functions.account.GetNotifySettingsRequest(peer=peer))
                return {
                    "chat": entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages",
                    **self._notify_row(s),
                }
            out = {}
            for label, factory in self.NOTIFY_SCOPES.items():
                s = await self.client(
                    functions.account.GetNotifySettingsRequest(peer=factory())
                )
                out[label] = self._notify_row(s)
            return {"defaults": out}

        self._assert_write()
        if mute is True:
            until = datetime.now(timezone.utc) + (
                timedelta(hours=hours) if hours else timedelta(days=365 * 5)
            )
        elif mute is False:
            until = None
        elif hours:
            until = datetime.now(timezone.utc) + timedelta(hours=hours)
        else:
            until = None

        settings = types.InputPeerNotifySettings(
            mute_until=until,
            # `silent` in Telegram means "without sound", so the understandable
            # `sound` is exposed outwards and inverted here.
            silent=None if sound is None else (not sound),
            show_previews=previews,
            stories_muted=None if stories is None else (not stories),
        )
        if scope:
            peer = self.NOTIFY_SCOPES[scope]()
            where = f"all {scope}"
        elif chat is not None:
            ent = await self.resolve(chat)
            peer = types.InputNotifyPeer(peer=await self.client.get_input_entity(ent))
            where = entity_name(await self.client.get_entity(ent)) if ent != "me" else "Saved Messages"
        else:
            raise ValueError("chat or scope is required")
        await self.client(
            functions.account.UpdateNotifySettingsRequest(peer=peer, settings=settings)
        )
        applied = {k: v for k, v in changes.items() if v is not None}
        return {"target": where, "applied": applied}

    async def mute(self, chat: Any, hours: int | None = None, unmute: bool = False) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        if unmute:
            until = None
        elif hours:
            until = datetime.now(timezone.utc) + timedelta(hours=hours)
        else:
            until = datetime.now(timezone.utc) + timedelta(days=365 * 5)
        settings = types.InputPeerNotifySettings(mute_until=until, show_previews=None, silent=None)
        await self.client(
            functions.account.UpdateNotifySettingsRequest(peer=ent, settings=settings)
        )
        return {"muted": not unmute, "until": _iso(until)}

    async def archive(self, chat: Any, undo: bool = False) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        await self.client(
            functions.folders.EditPeerFoldersRequest(
                folder_peers=[types.InputFolderPeer(peer=peer, folder_id=0 if undo else 1)]
            )
        )
        return {"archived": not undo}

    async def pin_dialog(self, chat: Any, unpin: bool = False) -> dict:
        self._assert_write()
        ent = await self.resolve(chat)
        peer = await self.client.get_input_entity(ent)
        await self.client(
            functions.messages.ToggleDialogPinRequest(
                peer=types.InputDialogPeer(peer=peer), pinned=not unpin
            )
        )
        return {"pinned": not unpin}
