"""Paths, environment and persisted settings for the Telegram agent."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Both locations are overridable so the same code runs from a checkout, from an
# installed wheel and inside a container with a mounted volume.
ENV_FILE = Path(os.environ.get("TG_ENV_FILE") or ROOT / ".env")
load_dotenv(ENV_FILE, override=False)   # early, so TG_DATA_DIR may live in .env
DATA = Path(os.environ.get("TG_DATA_DIR") or ROOT / "data")

SESSION = DATA / "session"          # Telethon appends .session; this is "main"
SOCKET = DATA / "daemon.sock"
EVENTS_LOG = DATA / "events.jsonl"
ACTIONS_LOG = DATA / "actions.jsonl"
DAEMON_LOG = DATA / "daemon.log"
PID_FILE = DATA / "daemon.pid"
RULES_FILE = DATA / "rules.json"
DOWNLOADS = DATA / "downloads"

DEFAULT_RULES = {
    "enabled": True,
    "alert_on_private": True,       # any DM from a human
    "alert_on_mention": True,       # @you or reply to you in groups
    "keywords": [],                 # substrings that raise an alert anywhere
    "watch_chats": [],              # chat ids/titles: alert on every message
    "mute_chats": [],               # chat ids/titles: never alert
    "ignore_bots": True,
    "transcribe_voice": True,       # voice notes and video circles — text straight into the alert
    "alert_on_reaction": False,     # reactions to your messages: always in the event log, in the alert — on demand
    "min_interval_sec": 3,          # per-chat alert throttle
    "quiet_hours": None,            # e.g. [23, 8] -> no alerts 23:00..08:00
}
# Write-side safety limits (per rolling hour unless stated otherwise).
LIMITS = {
    "max_sends_per_hour": 60,
    "max_distinct_chats_per_hour": 15,
    "max_deletes_per_hour": 50,
    "max_text_len": 4096,
}


MAIN_ACCOUNT = "main"


def normalize_account(account: str | None) -> str:
    label = (account or MAIN_ACCOUNT).strip().lower()
    # The last one is the Russian for "main". Aliases are input, not output: they
    # are what a person types, and nothing else here says anything about the
    # keyboard in front of them. An alias costs nothing; a rejected label costs
    # a command.
    if label in ("", "main", "default", "основной"):
        return MAIN_ACCOUNT
    safe = "".join(c for c in label if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError(f"Bad account label: {account!r}")
    return safe


def session_path(account: str | None = None) -> Path:
    """Session file for a label: main → data/session, work → data/session-work."""
    label = normalize_account(account)
    return SESSION if label == MAIN_ACCOUNT else DATA / f"session-{label}"


def list_accounts() -> list[str]:
    """Which accounts are actually signed in — by the session files on disk."""
    found = []
    if Path(str(SESSION) + ".session").exists():
        found.append(MAIN_ACCOUNT)
    for p in sorted(DATA.glob("session-*.session")):
        found.append(p.name[len("session-") : -len(".session")])
    return found


def ensure_dirs() -> None:
    DATA.mkdir(mode=0o700, exist_ok=True)
    DOWNLOADS.mkdir(mode=0o700, exist_ok=True)


def load_env() -> None:
    load_dotenv(ENV_FILE, override=False)


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def require_env(name: str) -> str:
    val = env(name)
    if not val:
        raise RuntimeError(
            f"{name} is not set in {ENV_FILE}. Run `uv run tg setup` in your terminal."
        )
    return val


def api_credentials() -> tuple[int, str]:
    return int(require_env("TG_API_ID")), require_env("TG_API_HASH")


def bot_token() -> str | None:
    return env("TG_BOT_TOKEN")


def alert_chat_id() -> str | None:
    return env("TG_ALERT_CHAT_ID")


def groq_key() -> str | None:
    return env("GROQ_API_KEY")


def whisper_settings() -> dict:
    """What to use for transcribing audio."""
    return {
        # auto: first Telegram's built-in transcript (Premium, free and instant),
        # then Groq, then the local model.
        "engine": (env("TG_WHISPER_ENGINE", "auto") or "auto").lower(),
        "groq_model": env("TG_GROQ_MODEL", "whisper-large-v3-turbo"),
        "local_model": env("TG_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo"),
        "max_upload_mb": int(env("TG_TRANSCRIBE_MAX_MB", "24") or 24),
    }


def allow_write() -> bool:
    return (env("TG_ALLOW_WRITE", "1") or "1").lower() not in ("0", "false", "no")


def load_rules() -> dict:
    if RULES_FILE.exists():
        try:
            stored = json.loads(RULES_FILE.read_text())
        except json.JSONDecodeError:
            stored = {}
    else:
        stored = {}
    rules = dict(DEFAULT_RULES)
    rules.update(stored)
    return rules


def save_rules(rules: dict) -> dict:
    ensure_dirs()
    merged = dict(DEFAULT_RULES)
    merged.update(rules)
    RULES_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    RULES_FILE.chmod(0o600)
    return merged


def write_env(values: dict[str, str]) -> None:
    """Merge values into .env, keeping the file private."""
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()
    existing.update({k: v for k, v in values.items() if v is not None})
    body = "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n"
    ENV_FILE.write_text(body)
    ENV_FILE.chmod(0o600)
    load_dotenv(ENV_FILE, override=True)
