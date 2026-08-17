"""Shared test scaffolding: a sandbox instead of data/, fakes instead of Telegram.

No test in this directory goes out to the network, opens a session file or reads
the real .env. That rests on two levels, and both are needed:

1. Environment variables are set here, before `tgagent.config` is imported. The
   module computes its paths and picks up .env right at import, so substituting
   them later is too late: `TG_ENV_FILE` sends the .env load to a file that does
   not exist (otherwise the owner's keys would end up in the environment of the
   test process), and `TG_DATA_DIR` sends every path into a directory under the
   temporary directory.
2. The autouse `data_dir` fixture re-points the already computed attributes of
   the module at `tmp_path` — its own folder for every test. One environment
   variable is not enough for that: the constants are computed once, at import.

The fixtures and factories below deliberately build real Telethon objects where
the code looks at their type (`isinstance`, `utils.get_peer_id`, the display
name), and fakes — where only behaviour matters.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

# --- sandbox instead of the real data/ and .env -----------------------------

# The directory is deliberately not created: the real files are put by each test
# into its own tmp_path (see the data_dir fixture), and this is a knowingly empty
# address in case some code computes a path before the substitution anyway.
_SANDBOX = Path(tempfile.gettempdir()) / "tgagent-tests-sandbox"

# Foreign TG_*/keys from the developer's environment are not let into the tests:
# they would change behaviour (allow_write, the bot, the model) and could leak
# into the output.
for _name in [k for k in os.environ if k.startswith("TG_")]:
    del os.environ[_name]
for _name in ("OPENAI_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(_name, None)

os.environ["TG_ENV_FILE"] = str(_SANDBOX / "absent.env")   # no such file, ever
os.environ["TG_DATA_DIR"] = str(_SANDBOX / "data")
os.environ["TG_API_ID"] = "1"
os.environ["TG_API_HASH"] = "0" * 32

from telethon.tl import types  # noqa: E402 — only after the environment above

from tgagent import alerts, config  # noqa: E402 — same: config reads paths at import
from tgagent.core import RateGuard, TelegramService  # noqa: E402 — same
from tgagent.daemon import Daemon  # noqa: E402 — same

# Paths of the config module that a test must see inside its own tmp_path.
_PATHS = {
    "SESSION": "session",
    "SOCKET": "daemon.sock",
    "EVENTS_LOG": "events.jsonl",
    "ACTIONS_LOG": "actions.jsonl",
    "DAEMON_LOG": "daemon.log",
    "PID_FILE": "daemon.pid",
    "RULES_FILE": "rules.json",
    "REMINDERS_FILE": "reminders.json",
    "DIGEST_FILE": "digest.json",
    "DOWNLOADS": "downloads",
    "INDEX_DB": "index.db",
    "MEMORY_DIR": "memory",
}


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A data directory of its own for every test. Automatic — so that a
    forgetful test author does not write into the owner's real data/."""
    root = tmp_path / "data"
    root.mkdir()
    assert root != config.ROOT / "data"
    monkeypatch.setenv("TG_DATA_DIR", str(root))
    monkeypatch.setattr(config, "DATA", root)
    for attr, name in _PATHS.items():
        monkeypatch.setattr(config, attr, root / name)
    return root


# --- fakes ------------------------------------------------------------------


class FakeBot:
    """Instead of alerts.BotChannel: remembers what was sent, goes nowhere."""

    def __init__(self, configured: bool = True) -> None:
        self._configured = configured
        self.chat_id = "42" if configured else None
        self.sent: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    @property
    def configured(self) -> bool:
        return self._configured

    async def send(self, text: str, chat_id: str | None = None, silent: bool = False) -> dict:
        self.sent.append(text)
        return {"message_id": len(self.sent)}

    async def call(self, method: str, **params: Any) -> dict:
        self.calls.append((method, params))
        return {"message_id": len(self.calls)}

    async def close(self) -> None:
        pass


class FakeService:
    """Instead of TelegramService in the daemon tests.

    Can do exactly what the daemon pulls out of it: the filter actions, `resolve`
    and `client.get_entity` for the confirmation question. Every call is
    recorded, so that a test can assert not only "it did not crash", but "this is
    exactly what was done".
    """

    def __init__(self, account: str = "main", entities: dict | None = None) -> None:
        self.account = account
        self.calls: list[tuple[str, dict]] = []
        self.entities = entities or {}
        self.client = self._Client(self)

    class _Client:
        def __init__(self, outer: FakeService) -> None:
            self.outer = outer

        async def get_entity(self, ent: Any) -> Any:
            return ent

    async def resolve(self, chat: Any) -> Any:
        # "избранное" is Cyrillic here for the same reason as in
        # core.SAVED_ALIASES: an alias is input, and the owner types it on
        # their own keyboard.
        if str(chat).strip().lower() in ("me", "self", "saved", "избранное"):
            return "me"
        return self.entities.get(chat, chat)

    def _record(self, name: str, kw: dict) -> dict:
        self.calls.append((name, kw))
        return {"ok": True}

    async def mark_read(self, **kw: Any) -> dict:
        return self._record("mark_read", kw)

    async def archive(self, **kw: Any) -> dict:
        return self._record("archive", kw)

    async def mute(self, **kw: Any) -> dict:
        return self._record("mute", kw)

    async def folder_edit(self, **kw: Any) -> dict:
        return self._record("folder_edit", kw)

    async def forward(self, **kw: Any) -> dict:
        return self._record("forward", kw)

    async def dialogs(self, **kw: Any) -> list[dict]:
        self.calls.append(("dialogs", kw))
        return []


class FakeClient:
    """A minimal Telethon client: a dict of entities and a list of dialogs."""

    def __init__(self, entities: dict | None = None, dialogs: list | None = None) -> None:
        self.entities = entities or {}
        self._dialogs = dialogs or []
        self.calls: list[tuple[str, Any]] = []

    async def get_entity(self, key: Any) -> Any:
        self.calls.append(("get_entity", key))
        if isinstance(key, (int, str)) and key in self.entities:
            return self.entities[key]
        raise ValueError(f"no such entity: {key!r}")

    def iter_dialogs(self, limit: int = 100, archived: bool = False):
        rows = [d for d in self._dialogs if bool(d.archived) == bool(archived)]

        async def gen():
            for d in rows:
                yield d

        return gen()


class FakeDialog:
    """A dialog to the extent dialog_row/dialog_kind sees it."""

    def __init__(
        self,
        id: int,
        name: str,
        entity: Any = None,
        archived: bool = False,
        unread_count: int = 0,
        unread_mentions_count: int = 0,
        pinned: bool = False,
        date: datetime | None = None,
        message: Any = None,
        is_user: bool = False,
        is_group: bool = False,
        is_channel: bool = False,
    ) -> None:
        self.id = id
        self.name = name
        self.entity = entity
        self.archived = archived
        self.unread_count = unread_count
        self.unread_mentions_count = unread_mentions_count
        self.pinned = pinned
        self.date = date
        self.message = message
        self.is_user = is_user
        self.is_group = is_group
        self.is_channel = is_channel


class FakeEvent:
    """A Telethon event to the extent the daemon watcher reads it."""

    def __init__(self, message: Any, chat: Any, sender: Any, is_private: bool = True) -> None:
        self.message = message
        self.is_private = is_private
        self._chat = chat
        self._sender = sender

    async def get_chat(self) -> Any:
        return self._chat

    async def get_sender(self) -> Any:
        return self._sender


class FakeMessage:
    """A Telethon message to the extent `message_dict`, `_links`, `_media_kind`
    and the indexer read it. An unknown key is an error: otherwise a typo in a
    test would silently turn into "the field is absent"."""

    _DEFAULTS: dict[str, Any] = {
        "id": 1,
        "date": datetime(2026, 8, 17, 9, 30, tzinfo=UTC),
        "message": "",
        "out": False,
        "sender": None,
        "from_id": None,
        "reply_to": None,
        "mentioned": False,
        "edit_date": None,
        "media_unread": False,
        "fwd_from": None,
        "reactions": None,
        "entities": None,
        "chat": None,
        "post_author": None,
        # kinds of attachment — this is how _media_kind tells them apart
        "media": None,
        "photo": None,
        "video": None,
        "video_note": None,
        "gif": None,
        "voice": None,
        "audio": None,
        "sticker": None,
        "document": None,
        "web_preview": None,
        "poll": None,
        "contact": None,
        "geo": None,
    }

    def __init__(self, **over: Any) -> None:
        unknown = set(over) - set(self._DEFAULTS)
        assert not unknown, f"unknown message fields: {sorted(unknown)}"
        for key, value in {**self._DEFAULTS, **over}.items():
            setattr(self, key, value)


def make_entity(kind: str, offset: int, length: int, url: str | None = None) -> Any:
    """A message entity (MessageEntityUrl and kin).

    A fake and not a Telethon type, deliberately: `_links` tells entities apart
    by `type(e).__name__`, and the test must break if that coupling is rewritten.
    """
    cls = type(kind, (), {})
    obj = cls()
    obj.offset = offset
    obj.length = length
    obj.url = url
    return obj


def make_event(**over: Any) -> dict:
    """An event of the log — what alert_reason, the filters and the digest work on."""
    # The name and the text stay Cyrillic on purpose: this event travels through
    # alert wording, the digest and the index, and non-ASCII is exactly what
    # breaks along that road (UTF-16 offsets, case folding, search).
    ev = {
        "at": datetime.now(UTC).isoformat(),
        "account": "main",
        "chat": "Петя",
        "chat_id": 555,
        "chat_type": "user",
        "private": True,
        "from": "Петя",
        "from_id": 555,
        "from_bot": False,
        "message_id": 10,
        "text": "привет",
        "media": False,
        "mentioned": False,
        "out": False,
        "link": None,
    }
    ev.update(over)
    return ev


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def service() -> TelegramService:
    """TelegramService without __init__: the real one demands keys and would
    create a session file. Only the pure serialisation and parsing methods are
    needed here."""
    svc = TelegramService.__new__(TelegramService)
    svc.account = "main"
    svc.me = None
    svc.client = None
    svc.guard = RateGuard(dict(config.LIMITS))
    svc._dialog_cache = []
    svc._dialog_cache_at = 0.0
    return svc


@pytest.fixture
def daemon(monkeypatch: pytest.MonkeyPatch) -> Daemon:
    """A daemon without Telegram and without a bot: default rules, a fake channel."""
    monkeypatch.setattr(alerts, "BotChannel", lambda *a, **kw: FakeBot(configured=False))
    d = Daemon()
    d.bot = FakeBot(configured=False)
    return d


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot(configured=True)


# The display names of these two stay Cyrillic on purpose: entity_name, the
# dialog rows and the search index are where non-ASCII breaks, and these two
# fixtures feed most of the tests that check it.
@pytest.fixture
def user() -> types.User:
    return types.User(id=555, first_name="Петя", username="petya")


@pytest.fixture
def supergroup() -> types.Channel:
    return types.Channel(
        id=1234567890, title="Команда", photo=None, date=None, megagroup=True
    )
