"""Outbound alerts and inbound commands over a dedicated BotFather bot.

The bot is a separate identity from the user account: the userbot session reads
and acts on Telegram, the bot only talks to you. That keeps the notification
channel out of your own chat list and makes it safe to poll.
"""

from __future__ import annotations

import html
from typing import Any

import aiohttp

from . import config

API = "https://api.telegram.org/bot{token}/{method}"


class BotChannel:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or config.bot_token()
        self.chat_id = chat_id or config.alert_chat_id()
        self._session: aiohttp.ClientSession | None = None
        self._offset: int | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=70)
            )
        return self._session

    async def call(self, method: str, **params: Any) -> dict:
        if not self.token:
            raise RuntimeError("TG_BOT_TOKEN is not configured")
        sess = await self._http()
        async with sess.post(API.format(token=self.token, method=method), json=params) as resp:
            data = await resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Bot API {method} failed: {data.get('description')}")
        return data["result"]

    async def me(self) -> dict:
        return await self.call("getMe")

    async def send(self, text: str, chat_id: str | None = None, silent: bool = False) -> dict:
        target = chat_id or self.chat_id
        if not target:
            raise RuntimeError("TG_ALERT_CHAT_ID is not configured")
        return await self.call(
            "sendMessage",
            chat_id=target,
            text=text[:4000],
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=silent,
        )

    async def poll(self, timeout: int = 50) -> list[dict]:
        """Long-poll for messages sent *to* the bot (your commands)."""
        params: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if self._offset is not None:
            params["offset"] = self._offset
        updates = await self.call("getUpdates", **params)
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def format_alert(event: dict, reason: str) -> str:
    """Render one Telegram event as an alert message."""
    chat = html.escape(event.get("chat") or "?")
    sender = html.escape(event.get("from") or "?")
    text = html.escape((event.get("text") or "")[:600])
    tag = {
        "private": "DM",
        "mention": "Mention",
        "keyword": "Keyword",
        "watch": "Watched chat",
        "reply": "Reply to you",
    }.get(reason, reason)
    link = event.get("link")
    account = event.get("account")
    if account and account != config.MAIN_ACCOUNT:
        tag = f"{tag} · account {html.escape(account)}"
    head = f"<b>{tag}</b> · {chat}"
    if chat != sender:
        head += f" · {sender}"
    body = head
    if text:
        body += f"\n\n{text}"
    transcript = event.get("transcript")
    if transcript:
        body += f"\n\n<b>Transcript:</b> {html.escape(transcript[:900])}"
    elif not text and event.get("media"):
        body += "\n\n[attachment]"
    if link:
        body += f'\n\n<a href="{link}">Open</a>'
    return body
