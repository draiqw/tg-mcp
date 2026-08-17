"""Paths, environment and persisted settings for the Telegram agent."""

from __future__ import annotations

import json
import os
import platform
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
# Settings of the installation itself, not of the alert rules: the default account
# choice lands here. A separate file, because the meaning is different — rules.json
# describes when to wake the owner, this is where the agent writes when nobody
# switched it by hand.
SETTINGS_FILE = DATA / "settings.json"
REMINDERS_FILE = DATA / "reminders.json"
DIGEST_FILE = DATA / "digest.json"
DOWNLOADS = DATA / "downloads"
INDEX_DB = DATA / "index.db"     # local search index, see tgagent/index.py
MEMORY_DIR = DATA / "memory"     # chat dossiers, one markdown file per chat

DEFAULT_RULES = {
    "enabled": True,
    "alert_on_private": True,       # any DM from a human
    "alert_on_mention": True,       # @you or reply to you in groups
    "keywords": [],                 # substrings that raise an alert anywhere
    "watch_chats": [],              # chat ids/titles: alert on every message
    "mute_chats": [],               # chat ids/titles: never alert
    "ignore_bots": True,
    "transcribe_voice": True,       # voice notes and video circles — text straight into the alert
    # Reactions to your messages: they always land in the event log, in the alert — on demand.
    "alert_on_reaction": False,
    "min_interval_sec": 3,          # per-chat alert throttle
    "quiet_hours": None,            # e.g. [23, 8] -> no alerts 23:00..08:00
    # Digest on a schedule: ["09:00", "20:00"] in local time. An empty list means
    # off. The daemon counts it itself, so it works even when Claude is not running.
    "digest_at": [],
    # Inbox filters, mail-style: condition -> a safe, reversible action.
    # No action of them writes to living people — see AUTO_ACTIONS.
    "auto": [],
    # A middle write mode between TG_ALLOW_WRITE=0 and 1: every writing action
    # asks the owner in the bot. Off by default, so that the behaviour of already
    # configured installations does not change with an update.
    "confirm_writes": "off",        # off | outgoing | all
    "confirm_whitelist": ["me"],    # chats not to ask about
    "confirm_timeout_sec": 90,      # how long to wait for an answer; silence = refusal
    # Chat dossiers. Updating them costs money and sends chunks of the
    # conversation outside, so it is off by default and works only by list.
    "memory_auto": False,           # update dossiers on their own, as the conversation goes
    "memory_after": 50,             # after how many new messages in a chat
    "memory_chats": [],             # for which chats; empty — for every chat that already has one
    "memory_max_per_hour": 10,      # cap on auto-updates per hour: this is money
}

# Keys of the confirmation mode. They live in the same file as the alert rules, but
# are edited by hand only: this is the owner's restriction, not an agent setting.
CONFIRM_KEYS = ("confirm_writes", "confirm_whitelist", "confirm_timeout_sec")

# What the inbox filter can do. The list is closed and deliberately short: only
# what is reversible and invisible to outsiders gets in. There are no autoreplies
# and no sending to living people here, and there must not be — one bad rule must
# not end in a message to a stranger.
AUTO_ACTIONS = ("read", "archive", "mute", "folder", "save")
AUTO_TYPES = ("private", "group", "channel", "bot")
AUTO_CONDITIONS = ("chat", "from", "keyword", "type")


def as_list(value: object) -> list:
    """One value or a list — everywhere a rule may be given either."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def parse_digest_times(value: object) -> list[tuple[int, int]]:
    """["09:00", "20:00"] -> [(9, 0), (20, 0)], ascending. Garbage is an error.

    The parsing is shared by the daemon and by the check on save: a schedule that
    silently did not fire is worse than a missing one.
    """
    out: list[tuple[int, int]] = []
    for raw in as_list(value):
        text = str(raw).strip()
        if not text:
            continue
        parts = text.replace(".", ":").split(":")
        if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
            raise ValueError(f"digest_at: {raw!r} — expected the format HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError(f"digest_at: {raw!r} — there is no such time")
        out.append((hour, minute))
    return sorted(set(out))


def validate_auto(value: object) -> list[dict]:
    """Check the auto section and turn action into a list.

    The check is strict and happens on input, not on firing: a rule with a typo in
    the action would otherwise simply do nothing, and there would be nothing to
    notice it by.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("auto: expected a list of rules")
    out: list[dict] = []
    for i, raw in enumerate(value, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"auto[{i}]: a rule is an object, not {type(raw).__name__}")
        rule = dict(raw)
        actions = [str(a).strip().lower() for a in as_list(rule.get("action"))]
        unknown = [a for a in actions if a not in AUTO_ACTIONS]
        if not actions or unknown:
            raise ValueError(
                f"auto[{i}]: action — one or several of {', '.join(AUTO_ACTIONS)}"
                + (f"; do not know: {', '.join(unknown)}" if unknown else "")
            )
        if "folder" in actions and not str(rule.get("folder") or "").strip():
            raise ValueError(f"auto[{i}]: the folder action needs a folder in the folder field")
        kind = rule.get("type")
        if kind is not None and str(kind).strip().lower() not in AUTO_TYPES:
            raise ValueError(f"auto[{i}]: type — one of {', '.join(AUTO_TYPES)}")
        if not any(rule.get(k) for k in AUTO_CONDITIONS):
            raise ValueError(
                f"auto[{i}]: at least one condition is needed ({', '.join(AUTO_CONDITIONS)}) — "
                "a rule without conditions would fire on every incoming message"
            )
        rule["action"] = actions
        out.append(rule)
    return out


# Write-side safety limits (per rolling hour unless stated otherwise).
LIMITS = {
    "max_sends_per_hour": 60,
    "max_distinct_chats_per_hour": 15,
    "max_deletes_per_hour": 50,
    "max_text_len": 4096,
}


MAIN_ACCOUNT = "main"

# How the installation signs itself in the Telegram device list (Settings → Devices).
# One table for every place a client is created: the line "macOS" on somebody else's
# Linux would simply be untrue in the list the owner uses to decide what to revoke.
def client_info() -> dict[str, str]:
    # The version comes from a late import: tgagent/__init__.py pulls nothing in, but
    # an import at the top of the file would make config depend on the package that
    # itself imports config — and config is loaded first, before everything else.
    from tgagent import __version__

    system = platform.system() or "unknown"
    release = platform.release() or ""
    return {
        "device_model": "claude-tg-agent",
        "system_version": f"{system} {release}".strip(),
        "app_version": f"tgagent {__version__}",
    }


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


def index_path(account: str | None = None) -> Path:
    """Conversation index for a label: main → data/index.db, work → data/index-work.db.

    One file per account, same as with the session: mixing the conversations of
    different accounts in one index is not allowed — wiping one without touching
    the other would become impossible.
    """
    label = normalize_account(account)
    return INDEX_DB if label == MAIN_ACCOUNT else DATA / f"index-{label}.db"


def memory_dir(account: str | None = None) -> Path:
    """Chat dossiers: main → data/memory, work → data/memory-work.

    One directory per account — for the same reason as with the index: the same
    person in two accounts is two different conversations with different histories.
    """
    label = normalize_account(account)
    return MEMORY_DIR if label == MAIN_ACCOUNT else DATA / f"memory-{label}"


def openai_key() -> str | None:
    return env("OPENAI_API_KEY")


def memory_settings() -> dict:
    """With what and how the chat dossiers are kept."""
    return {
        "model": env("TG_MEMORY_MODEL", "gpt-4o-mini"),
        "base_url": env("TG_MEMORY_BASE_URL", "https://api.openai.com/v1"),
        # How many messages go into the model: deeper on the first pass, after that
        # only what has arrived since the last time.
        "first_messages": int(env("TG_MEMORY_FIRST", "300") or 300),
        "max_new_messages": int(env("TG_MEMORY_MAX_NEW", "400") or 400),
        "max_chars": int(env("TG_MEMORY_MAX_CHARS", "3000") or 3000),
        "timeout_sec": int(env("TG_MEMORY_TIMEOUT", "90") or 90),
    }


def list_accounts() -> list[str]:
    """Which accounts are actually signed in — by the session files on disk."""
    found = []
    if Path(str(SESSION) + ".session").exists():
        found.append(MAIN_ACCOUNT)
    for p in sorted(DATA.glob("session-*.session")):
        found.append(p.name[len("session-") : -len(".session")])
    return found


def login_command(account: str | None = None) -> str:
    """The exact sign-in command for a label. One for the whole code base: the error
    "account is not signed in" is useless if it says "sign in" instead of what to type."""
    label = normalize_account(account)
    tail = "" if label == MAIN_ACCOUNT else f" --account {label}"
    return f"cd {ROOT} && uv run tg login{tail}"


def add_account_command() -> str:
    """The command for one more account — with a slot for the label.

    Separate from `login_command`, because `<label>` cannot be passed through it:
    normalization would strip the angle brackets out and substitute a name that
    does not exist.
    """
    return f"{login_command()} --account <label>"


def not_logged_in(account: str | None, known: list[str] | None = None) -> str:
    """The text of the refusal "there is no such account". The code and the password
    are typed by the owner in their own terminal, the agent never sees them and cannot
    sign anyone in — so the whole point of the message is that the owner can run it
    word for word."""
    label = normalize_account(account)
    have = ", ".join(known) if known else "none"
    return (
        f"Account {label!r} is not signed in (available: {have}). The owner signs in themselves, "
        f"the agent never sees the code: {login_command(label)}"
    )


class SetupError(RuntimeError):
    """The installation was not finished — not a code failure, but a step not taken.

    A separate type is needed for exactly one thing: such a message is printed as
    it is, without a traceback. A traceback on the first run, in front of somebody
    from the outside, looks like a broken program, even though all that is missing
    is `.env` or the sign-in.
    """


def setup_hint() -> str | None:
    """What is missing right now — in one line and with the exact command.

    None means everything is in place. One text for every entry point (the MCP
    server, `tg daemon start`, the daemon itself): three different pieces of advice
    for one unfinished installation are three different ideas of what to do next.
    """
    if not (env("TG_API_ID") and env("TG_API_HASH")):
        return (
            "The app keys (TG_API_ID/TG_API_HASH) are not set — you get them at "
            f"my.telegram.org. The whole setup: cd {ROOT} && uv run tg init"
        )
    if not list_accounts():
        return (
            "There is no Telegram session at all. The owner signs in themselves, the agent "
            f"never sees the code: {login_command()}"
        )
    return None


def ensure_dirs() -> None:
    # parents=True: TG_DATA_DIR may point at a nested directory that does not exist
    # yet (a volume in a container, ~/.local/share/tgagent/data). Without the
    # parents, the very first run on somebody else's machine would fail with
    # FileNotFoundError instead of simply creating the directory.
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    DOWNLOADS.mkdir(mode=0o700, parents=True, exist_ok=True)


def load_env() -> None:
    load_dotenv(ENV_FILE, override=False)


def env(name: str, default: str | None = None) -> str | None:
    load_env()
    val = os.environ.get(name, default)
    return val.strip() if isinstance(val, str) else val


def require_env(name: str) -> str:
    val = env(name)
    if not val:
        raise SetupError(
            f"{name} is set neither in the environment nor in {ENV_FILE}. "
            f"Run the setup in your own terminal: cd {ROOT} && uv run tg init"
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


def _stored_rules() -> dict:
    if RULES_FILE.exists():
        try:
            return json.loads(RULES_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def load_rules() -> dict:
    rules = dict(DEFAULT_RULES)
    rules.update(_stored_rules())
    return rules


def load_confirm() -> dict:
    """Write confirmation settings — always fresh from disk.

    Separate from load_rules, because they are read on every writing call: editing
    the file by hand must take effect at once, not after a daemon restart.
    """
    stored = _stored_rules()
    return {k: stored.get(k, DEFAULT_RULES[k]) for k in CONFIRM_KEYS}


def save_rules(rules: dict) -> dict:
    ensure_dirs()
    merged = dict(DEFAULT_RULES)
    merged.update(rules)
    # Whatever is being saved — the alert rules from tg_rules, /watch or /mute —
    # the confirmation mode is taken from disk, not from the dictionary passed in.
    # Otherwise the agent would lift the restriction off itself by accident: it
    # would be enough to write the rules over a stale copy in the daemon's memory.
    merged.update(load_confirm())
    # The schedule and the filters are checked before writing: a wrong schedule
    # would stay silent, and a wrong filter would silently not fire — there is
    # nothing to notice either case by.
    parse_digest_times(merged.get("digest_at"))
    merged["auto"] = validate_auto(merged.get("auto"))
    RULES_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    RULES_FILE.chmod(0o600)
    return merged


def load_settings() -> dict:
    """Installation settings from disk. A broken file means empty settings, not a
    crash: one broken line must not stop the daemon from coming up."""
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def save_settings(patch: dict) -> dict:
    ensure_dirs()
    merged = {**load_settings(), **patch}
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    SETTINGS_FILE.chmod(0o600)
    return merged


def default_account() -> str:
    """The account label calls go to when the client has not been switched.

    It lives on disk, not in the MCP server process: the choice "I am working from
    the work account" survives closing Claude, otherwise the very first message
    after a restart would go to the wrong place, and nobody would warn about it.
    """
    raw = load_settings().get("default_account")
    if not raw:
        return MAIN_ACCOUNT
    try:
        return normalize_account(str(raw))
    except ValueError:
        return MAIN_ACCOUNT


def set_default_account(account: str | None) -> str:
    """Remember the default for good. None or "main" returns to the main account."""
    label = normalize_account(account)
    save_settings({"default_account": label})
    return label


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
