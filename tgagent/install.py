"""Setup wizard (`tg init`) and diagnostics (`tg doctor`).

Installing this agent takes six steps, and before the wizard appeared not one of
them pointed at the next: `uv sync`, app keys, sign-in, bot, daemon,
registration with the client, copying the subagents. Any of them is easy to
forget, and the symptom is vague — "there are no tools", "the daemon does not
answer", "alerts do not arrive".

So there are two entrances here, and both look at the same installation state:

* `tg init` — the wizard. It first finds out what is already done and does only
  what is missing, so a repeat run is safe and works as "fix my installation".
  Only the app keys and the sign-in are required in it; everything else is
  skipped with Enter, and the wizard names exactly what will stop working.
* `tg doctor` — the same analysis, but it asks nothing and changes nothing.
  Prints a "good / bad / here is what to do" list. This is the first thing worth
  attaching to an issue, so its output holds no keys, no phone number and no
  account name — only facts about the installation.

The code and the two-factor password are not automated by the wizard and are
stored nowhere: the owner types them in their own terminal, the sign-in step is
simply handed over to `tg login`. The wizard reuses the `setup`, `login`,
`link-bot` logic instead of repeating it — otherwise two implementations of one
question would have drifted apart on the very first edit.
"""

from __future__ import annotations

import argparse
import asyncio
import filecmp
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path

from . import config

MCP_NAME = "telegram"
AGENT_FILES = ("telegram.md", "telegram-watch.md")
AGENT_DIR = Path.home() / ".claude" / "agents"


def agents_src_dir() -> Path:
    """Where the subagent sources come from.

    In a clone this is `agents/` next to the code. When installed as a package
    `ROOT` points inside site-packages, where there is no `agents/` at all — the
    same files are put there by the wheel build (force-include in pyproject).
    Both places are checked, otherwise `tg doctor` from an installed package
    would fail with FileNotFoundError.
    """
    repo = config.ROOT / "agents"
    if repo.is_dir():
        return repo
    return Path(__file__).resolve().parent / "agents"

# Autostart: launchd on macOS, a user systemd unit on Linux. Both forms live in
# the repository as templates with placeholders instead of paths — only the
# installation can substitute them, because the project directory and the path
# to uv are different for everyone.
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
PLIST_NAME = "com.tgagent.daemon.plist"
SYSTEMD_USER_DIR = Path.home() / ".config" / "systemd" / "user"
UNIT_NAME = "tgagent.service"

# (uv path placeholder, project directory placeholder) — one pair per template.
AUTOSTART_PLACEHOLDERS = (
    ("/Users/YOUR_USER/.local/bin/uv", "/Users/YOUR_USER/tg-agent"),
    ("/home/YOUR_USER/.local/bin/uv", "/home/YOUR_USER/tg-agent"),
)


def render_autostart(text: str, uv: str, root: Path | str) -> str:
    """Substitute the real paths into an autostart template.

    Both pairs of placeholders are run over any template: that way one function
    serves both the plist and the unit, and adding a third form does not require
    a third branch.
    """
    for uv_token, root_token in AUTOSTART_PLACEHOLDERS:
        text = text.replace(uv_token, uv).replace(root_token, str(root))
    return text


def autostart_kind() -> str | None:
    """What raises the daemon at sign-in on this system: launchd, systemd or nothing."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    return None


def autostart_src(name: str) -> Path:
    """Autostart template: in a clone it is in the root, in an installed package
    inside the package (see agents_src_dir, the reason is the same)."""
    repo = config.ROOT / name
    if repo.exists():
        return repo
    return Path(__file__).resolve().parent / "autostart" / name


def autostart_paths() -> tuple[Path, Path] | None:
    """(template in the repository, where it gets installed) for this system."""
    kind = autostart_kind()
    if kind == "launchd":
        return autostart_src(PLIST_NAME), LAUNCH_AGENTS / PLIST_NAME
    if kind == "systemd":
        return autostart_src(UNIT_NAME), SYSTEMD_USER_DIR / UNIT_NAME
    return None


# Sign-in errors whose cause and way out are known in advance. The keys are
# Telegram codes: `capabilities.error_codes` digs them out, and this is the same
# analysis that explains tool errors — there is no private copy of the code table
# here.
LOGIN_HINTS: dict[str, str] = {
    "PHONE_CODE_INVALID":
        "the code did not fit. It arrives in the Telegram app itself (not SMS) and is "
        "typed without spaces. Start the sign-in again: uv run tg login",
    "PHONE_CODE_EXPIRED":
        "the code has gone stale — Telegram keeps it for a few minutes. Request a new "
        "one: uv run tg login",
    "PHONE_CODE_EMPTY": "no code was entered. Start the sign-in again: uv run tg login",
    "PHONE_NUMBER_INVALID":
        "Telegram did not accept the number. It needs the international format with a "
        "plus, for example +79991234567.",
    "PHONE_NUMBER_BANNED": "this number is banned in Telegram — signing in with it is impossible.",
    "PASSWORD_HASH_INVALID":
        "the cloud password of two-step verification did not fit. This is the Telegram "
        "password (Settings → Privacy and Security → Two-Step Verification), not the "
        "password of your mail or Apple ID. To repeat only the password: "
        "uv run tg password",
    "SESSION_PASSWORD_NEEDED":
        "the account has two-step verification on: only the password is left. Type it "
        "yourself in the terminal: uv run tg password",
    "API_ID_INVALID":
        "Telegram did not accept api_id/api_hash. Check that they were copied from "
        "my.telegram.org → API development tools in full: uv run tg setup",
    "API_ID_PUBLISHED_FLOOD":
        "Telegram considers these api_id/api_hash leaked into public access and has "
        "limited them. Create a new application on my.telegram.org and enter its keys: "
        "uv run tg setup",
    "AUTH_KEY_DUPLICATED":
        "the session file was used from another machine at the same time, and Telegram "
        "revoked it. data/session.session must not be copied between machines — sign in "
        "again: uv run tg login",
    "AUTH_KEY_UNREGISTERED":
        "the session was revoked on the Telegram side (usually it was closed in the "
        "device list). Sign in again: uv run tg login",
}


# ---------------------------------------------------------------- environment


def claude_bin() -> str | None:
    """Path to Claude Code or None. A separate function — tests substitute it."""
    return shutil.which("claude")


def uv_bin() -> str | None:
    return shutil.which("uv")


def mcp_add_command(root: Path | str | None = None, scope: str = "user",
                    name: str = MCP_NAME) -> list[str]:
    """The command that registers the MCP server with Claude Code.

    The wizard knows the project path itself — that is half the point of this
    step: a person carries the line from the README by hand and gets the
    directory wrong. `--` separates the client options from the server command,
    and `uv --directory` is needed because the server starts from the client's
    project directory, not from here.
    """
    return [
        "claude", "mcp", "add", "-s", scope, name, "--",
        "uv", "--directory", str(root or config.ROOT), "run", "tg-mcp",
    ]


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run and return (code, output). A missing program is not an exception but
    code 127: the caller needs text for a person, not a traceback."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, f"no {cmd[0]} command"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} did not answer in {timeout} s"
    except OSError as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def mcp_registered(name: str = MCP_NAME) -> bool | None:
    """Whether the server is registered. None — there is nobody to ask, no `claude`.

    Through `claude mcp get`, not `mcp list`: the list checks the health of every
    server and goes to the network, and all we need is the fact that a record
    exists in any scope.
    """
    if not claude_bin():
        return None
    code, _ = _run(["claude", "mcp", "get", name], timeout=30)
    return code == 0


# ---------------------------------------------------------------- subagents


def agent_rows(target_dir: Path | None = None) -> list[dict]:
    """State of the subagents in the client's directory: missing | same | differs.

    The comparison is byte for byte: a file that has drifted apart means an
    outdated tool set for the agent, and it looks not like an error but like
    "the tool is somehow not visible".
    """
    dst_dir = target_dir or AGENT_DIR
    src_dir = agents_src_dir()
    rows = []
    for name in AGENT_FILES:
        src = src_dir / name
        dst = dst_dir / name
        if not src.exists():
            # There is no source at all — the installation is incomplete. That is
            # not the same as "not laid out": it is fixed not by the wizard but
            # by a proper installation.
            state = "no-source"
        elif not dst.exists():
            state = "missing"
        elif filecmp.cmp(src, dst, shallow=False):
            state = "same"
        else:
            state = "differs"
        rows.append({"name": name, "state": state, "src": src, "dst": dst})
    return rows


def install_agents(target_dir: Path | None = None,
                   ask: Callable[[str], bool] | None = None) -> list[dict]:
    """Lay the subagents out into the client's directory.

    `ask(name)` is asked only about a file that is already there and differs:
    overwriting somebody else's edit silently is not allowed, and keeping quiet
    about it means leaving the person with an agent that does not see half of the
    tools. Returns what was done with each: installed | replaced | kept | same.
    """
    dst_dir = target_dir or AGENT_DIR
    dst_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for row in agent_rows(dst_dir):
        if row["state"] == "no-source":
            action = "no-source"
        elif row["state"] == "same":
            action = "same"
        elif row["state"] == "missing":
            shutil.copyfile(row["src"], row["dst"])
            action = "installed"
        elif ask is not None and ask(row["name"]):
            shutil.copyfile(row["src"], row["dst"])
            action = "replaced"
        else:
            action = "kept"
        out.append({"name": row["name"], "action": action})
    return out


# ---------------------------------------------------------------- state


def _mode(path: Path) -> int | None:
    try:
        return path.stat().st_mode & 0o777
    except OSError:
        return None


def probe(account: str | None = None, check_claude: bool = True) -> dict:
    """Facts about the installation — without a single question to a person.

    One and the same snapshot is read by the wizard (what is left to do) and by
    the diagnostics (what to tell in an issue): a divergence between "init counts
    the step as done" and "doctor counts it as not done" would be the worst
    possible bug here.
    """
    from . import capabilities as caps
    from . import cli

    label = config.normalize_account(account)
    session = Path(str(config.session_path(label)) + ".session")
    registered = mcp_registered() if check_claude else None
    paths = autostart_paths()
    return {
        "root": config.ROOT,
        "python": ".".join(str(n) for n in sys.version_info[:3]),
        "uv": uv_bin(),
        "env_file": config.ENV_FILE,
        "env_exists": config.ENV_FILE.exists(),
        "env_mode": _mode(config.ENV_FILE),
        "data": config.DATA,
        "data_mode": _mode(config.DATA),
        "api": bool(config.env("TG_API_ID") and config.env("TG_API_HASH")),
        "allow_write": config.allow_write(),
        "account": label,
        "accounts": config.list_accounts(),
        "session": session,
        "session_exists": session.exists(),
        "session_mode": _mode(session),
        "login_pending": cli.login_state(label).exists(),
        "bot_token": bool(config.bot_token()),
        "alert_chat": bool(config.alert_chat_id()),
        "openai": bool(config.openai_key()),
        "groq": bool(config.groq_key()),
        "local_whisper": caps.local_whisper(),
        "daemon_pid": cli._daemon_pid(),
        "socket": config.SOCKET.exists(),
        "claude": claude_bin() if check_claude else None,
        "mcp": registered,
        "agents": [{k: v for k, v in row.items() if k != "src"} for row in agent_rows()],
        "autostart": bool(paths and paths[1].exists()),
        "autostart_kind": autostart_kind(),
        "autostart_template": paths[0] if paths else None,
        "autostart_target": paths[1] if paths else None,
    }


@dataclass
class Step:
    """A wizard step: what it is, whether it is done and what happens without it."""

    key: str
    title: str
    required: bool
    done: bool
    detail: str = ""            # what already closes the step — printed when done
    cost: str = ""              # what will not work without it
    fix: str = ""               # how to close the step by hand, without the wizard


def plan(st: dict) -> list[Step]:
    """The full list of steps marked "done" — from the state snapshot.

    The order is not accidental: the keys are needed by the sign-in, the optional
    keys by the daemon (it reads .env at startup), the daemon by the registration
    with the client, because the first thing a fresh MCP server does is knock on
    the daemon's socket.
    """
    agents_done = all(row["state"] == "same" for row in st["agents"])
    return [
        Step(
            key="api", title="app keys (api_id/api_hash)",
            required=True, done=st["api"],
            detail="already in .env",
            cost="without them nothing works: MTProto will not let you in, only the Bot API "
                 "is left",
            fix="uv run tg setup",
        ),
        Step(
            key="login", title="sign-in to the account",
            # An unfinished sign-in leaves login_state.json behind: the session
            # file is already there, there is no authorization in it yet. To
            # count such a step as done means leading the person to the daemon,
            # which will fall over on this session.
            required=True, done=st["session_exists"] and not st["login_pending"],
            detail=f"session in place: {st['session']}",
            cost="without the sign-in the daemon has nothing to work with: there is no account",
            fix="uv run tg login",
        ),
        Step(
            key="bot", title="notification bot",
            required=False, done=st["bot_token"] and st["alert_chat"],
            detail="token present, chat_id linked",
            cost="there will be no alerts about incoming messages, no digest, no tg_ask "
                 "and no write confirmation",
            fix="uv run tg setup, then uv run tg link-bot",
        ),
        Step(
            key="memory_key", title="model key for chat dossiers (OPENAI_API_KEY)",
            required=False, done=st["openai"],
            detail="set",
            cost="tg_memory will not be able to update chat dossiers (reading ready ones — "
                 "it will)",
            fix="add OPENAI_API_KEY to .env",
        ),
        Step(
            key="groq", title="Groq key for audio transcripts",
            required=False, done=st["groq"],
            detail="set",
            cost="transcripts will be left only through Telegram Premium or a local model",
            fix="add GROQ_API_KEY to .env (console.groq.com/keys)",
        ),
        Step(
            key="local_whisper", title="local transcription model",
            required=False, done=bool(st["local_whisper"]),
            detail=str(st["local_whisper"]),
            cost="without it and without Groq only Telegram will transcribe audio, and only "
                 "with Premium",
            fix="uv sync --extra local-whisper",
        ),
        Step(
            key="daemon", title="daemon",
            required=True, done=bool(st["daemon_pid"]),
            detail=f"running, pid {st['daemon_pid']}",
            cost="without the daemon not a single tool works: the session belongs to it",
            fix="uv run tg daemon start",
        ),
        Step(
            key="mcp", title="registering the server with Claude Code",
            required=False, done=st["mcp"] is True,
            detail="the telegram server is registered",
            cost="Claude Code will not see the tools — the server is unknown to it",
            fix=" ".join(mcp_add_command(st["root"])),
        ),
        Step(
            key="agents", title="subagents in ~/.claude/agents",
            required=False, done=agents_done,
            detail="match the repository",
            cost="there will be no ready-made telegram and telegram-watch subagents",
            fix=f"cp {st['root']}/agents/*.md {AGENT_DIR}/",
        ),
        Step(
            key="autostart", title="daemon autostart at system sign-in",
            required=False, done=st["autostart"],
            detail=f"{st['autostart_target']} installed" if st["autostart_target"] else "",
            cost="after a reboot the daemon will have to be raised by hand",
            fix=autostart_fix(st),
        ),
    ]


def autostart_enable_command(kind: str | None) -> str:
    """What switches on an autostart file that is already written. Empty — there is no
    such system."""
    if kind == "systemd":
        return (f"systemctl --user daemon-reload && "
                f"systemctl --user enable --now {UNIT_NAME}")
    if kind == "launchd":
        return f"launchctl load -w {LAUNCH_AGENTS / PLIST_NAME}"
    return ""


def autostart_fix(st: dict) -> str:
    """How to install autostart by hand — on the system where the wizard is running.

    The advice "copy the plist into ~/Library/LaunchAgents" on Linux does not
    simply fail to work, it leads away: there is neither the directory nor
    launchd there.
    """
    kind = st.get("autostart_kind")
    if not kind:
        return "this system has no autostart — keep the daemon in docker (see docs/docker.md)"
    return (f"copy {st['autostart_template']} into {st['autostart_target'].parent}, "
            f"substitute your own paths in it and switch it on: "
            f"{autostart_enable_command(kind)}")


def pending(st: dict) -> list[Step]:
    """The steps that are missing. The idempotence of the wizard rests on them."""
    return [s for s in plan(st) if not s.done]


# ---------------------------------------------------------------- errors


def explain_login_error(exc: BaseException) -> str | None:
    """A clear reason for a sign-in refusal, or None if the error is unfamiliar.

    None is a refusal to explain, not an empty explanation: an invented reason is
    worse than one shown as it is.
    """
    from . import capabilities as caps

    for code in caps.error_codes(exc):
        if code in LOGIN_HINTS:
            return LOGIN_HINTS[code]
    return caps.explain_error(exc)


# ---------------------------------------------------------------- diagnostics

_OK, _BAD, _SKIP = "ok", "bad", "skip"


def _row(key: str, section: str, status: str, text: str, fix: str | None = None) -> dict:
    return {"key": key, "section": section, "status": status, "text": text, "fix": fix}


def report(st: dict) -> list[dict]:
    """The diagnostics report as rows. Not one secret in the text — only facts:
    the report is written so that it is attached to an issue whole."""
    rows: list[dict] = []
    add = rows.append

    add(_row("root", "installation", _OK, f"project directory: {st['root']}"))
    add(_row("python", "installation", _OK, f"python {st['python']}"))
    add(_row(
        "uv", "installation",
        _OK if st["uv"] else _BAD,
        f"uv: {st['uv']}" if st["uv"] else "uv not found in PATH",
        None if st["uv"]
        else "install uv: https://docs.astral.sh/uv/ — everything is started with it",
    ))
    if st["env_exists"]:
        mode = st["env_mode"]
        open_to_others = bool(mode and mode & 0o077)
        add(_row(
            "env", "installation", _BAD if open_to_others else _OK,
            f".env is there, mode {mode:o}" if mode else ".env is there",
            f"chmod 600 {st['env_file']} — it holds full access to the account"
            if open_to_others else None,
        ))
    else:
        add(_row(
            "env", "installation", _SKIP if st["api"] else _BAD,
            "no .env, the values are taken from the environment" if st["api"] else "no .env",
            None if st["api"] else "uv run tg init",
        ))
    mode = st["data_mode"]
    add(_row(
        "data", "installation",
        _BAD if mode and mode & 0o077 else _OK,
        f"data directory: {st['data']}" + (f", mode {mode:o}" if mode else " (not created yet)"),
        f"chmod 700 {st['data']} — the session, the index and the dossiers are there"
        if mode and mode & 0o077 else None,
    ))

    add(_row(
        "api", "keys", _OK if st["api"] else _BAD,
        "api_id/api_hash are set" if st["api"] else "api_id/api_hash are not set",
        None if st["api"] else "uv run tg init (or uv run tg setup)",
    ))
    add(_row(
        "write", "keys", _OK,
        "writing to the account is allowed" if st["allow_write"]
        else "writing is off (TG_ALLOW_WRITE=0), the agent only reads",
    ))

    if st["accounts"]:
        add(_row("accounts", "accounts", _OK,
                 f"signed in: {', '.join(st['accounts'])}; default "
                 f"{config.default_account()}"))
    else:
        add(_row("accounts", "accounts", _BAD, "not a single signed-in account",
                 "uv run tg login"))
    if st["session_exists"]:
        mode = st["session_mode"]
        add(_row(
            "session", "accounts",
            _BAD if mode and mode & 0o077 else _OK,
            f"session file {st['account']}: mode {mode:o}" if mode
            else f"session file {st['account']} is in place",
            f"chmod 600 {st['session']} — this file is itself the sign-in to the account "
            "without a password and without 2FA" if mode and mode & 0o077 else None,
        ))
    if st["login_pending"]:
        add(_row("login_pending", "accounts", _BAD,
                 "the sign-in is not finished: the code was accepted, the cloud password "
                 "was not entered",
                 "uv run tg password — the password is typed only by hand, from a live terminal"))

    add(_row(
        "daemon", "daemon", _OK if st["daemon_pid"] else _BAD,
        f"running, pid {st['daemon_pid']}" if st["daemon_pid"] else "not running",
        None if st["daemon_pid"] else "uv run tg daemon start",
    ))
    if st["socket"] and not st["daemon_pid"]:
        add(_row("socket", "daemon", _BAD, "the socket file is left over from a dead daemon",
                 "uv run tg daemon restart"))
    if "rpc" in st:
        rpc = st["rpc"]
        add(_row(
            "rpc", "daemon", _OK if rpc.get("ok") else _BAD,
            f"RPC answers, live sessions: {rpc.get('sessions')}" if rpc.get("ok")
            else f"RPC does not answer: {rpc.get('error')}",
            None if rpc.get("ok") else "uv run tg daemon restart, then uv run tg daemon logs",
        ))

    if st["claude"]:
        add(_row("claude", "Claude Code", _OK, f"claude: {st['claude']}"))
        add(_row(
            "mcp", "Claude Code", _OK if st["mcp"] else _BAD,
            f"MCP server {MCP_NAME} is registered" if st["mcp"]
            else f"MCP server {MCP_NAME} is not registered",
            None if st["mcp"] else " ".join(mcp_add_command(st["root"])),
        ))
    else:
        add(_row("claude", "Claude Code", _SKIP, "claude not found in PATH",
                 "if the client is a different one this is normal; the registration "
                 "command is printed by uv run tg init"))
    for row in st["agents"]:
        state = row["state"]
        add(_row(
            f"agent:{row['name']}", "Claude Code",
            _OK if state == "same" else _BAD,
            {
                "same": f"{row['name']}: matches the repository",
                "differs": f"{row['name']}: differs from the repository",
                "missing": f"{row['name']}: not installed",
                "no-source": f"{row['name']}: no source, nothing to install",
            }[state],
            None if state == "same"
            else ("the installation is incomplete — the subagents live in the repository: "
                  "git clone https://github.com/draiqw/tg-mcp" if state == "no-source"
                  else f"uv run tg init — it will ask again and update; by hand: "
                       f"cp {st['root']}/agents/{row['name']} {row['dst']}"),
        ))

    add(_row(
        "bot", "optional",
        _OK if st["bot_token"] and st["alert_chat"] else _SKIP,
        "the notification bot is set up" if st["bot_token"] and st["alert_chat"]
        else ("the bot token is there, chat_id is not linked" if st["bot_token"]
              else "the notification bot is not set up"),
        None if st["bot_token"] and st["alert_chat"]
        else ("uv run tg link-bot — press Start in the chat with the bot" if st["bot_token"]
              else "uv run tg init: without the bot there are no alerts, no digest and no tg_ask"),
    ))
    add(_row(
        "openai", "optional", _OK if st["openai"] else _SKIP,
        "OPENAI_API_KEY is set" if st["openai"]
        else "OPENAI_API_KEY is not set — chat dossiers are not updated",
        None if st["openai"] else "add OPENAI_API_KEY to .env",
    ))
    add(_row(
        "groq", "optional", _OK if st["groq"] else _SKIP,
        "GROQ_API_KEY is set" if st["groq"] else "GROQ_API_KEY is not set",
        None if st["groq"] else "add GROQ_API_KEY to .env (console.groq.com/keys)",
    ))
    add(_row(
        "local_whisper", "optional", _OK if st["local_whisper"] else _SKIP,
        f"local transcription: {st['local_whisper']}" if st["local_whisper"]
        else "there is no local transcription model",
        None if st["local_whisper"] else "uv sync --extra local-whisper",
    ))
    if st["autostart_kind"]:
        add(_row(
            "autostart", "optional", _OK if st["autostart"] else _SKIP,
            f"daemon autostart is installed ({st['autostart_kind']})" if st["autostart"]
            else f"there is no autostart ({st['autostart_kind']}): after a reboot the daemon "
                 "has to be raised by hand",
            None if st["autostart"] else "uv run tg init will offer to install it",
        ))
    return rows


def render(rows: list[dict]) -> str:
    """The report as text: by section, with an indent under "what to do"."""
    out: list[str] = []
    section = None
    for row in rows:
        if row["section"] != section:
            section = row["section"]
            out.append(f"\n{section}")
        out.append(f"  [{row['status']:5}] {row['text']}")
        if row["fix"]:
            out.append(f"           → {row['fix']}")
    bad = sum(1 for r in rows if r["status"] == _BAD)
    out.append("")
    out.append("everything is in place" if not bad else f"bad: {bad}")
    return "\n".join(out).lstrip("\n")


# ---------------------------------------------------------------- wizard


class Wizard:
    """One pass of the wizard. Holds the installation state and the list of what
    was skipped."""

    def __init__(self, args) -> None:
        self.args = args
        self.account = getattr(args, "account", None)
        self.interactive = sys.stdin.isatty()
        self.skipped: list[str] = []
        self.state = probe(self.account)

    # --- input

    def p(self, msg: str = "") -> None:
        from . import cli

        cli._p(msg)

    def yes(self, prompt: str, default: bool = False) -> bool:
        """A yes/no question. Enter is the default, and the default is almost
        everywhere "no": the wizard must not switch on anything extra for
        somebody who simply presses Enter."""
        if not self.interactive:
            return default
        suffix = "[Y/n]" if default else "[y/N]"
        answer = input(f"   {prompt} {suffix}: ").strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def skip(self, step: Step) -> None:
        self.p(f"   Skipped — {step.cost}.")
        self.skipped.append(f"{step.title}: {step.fix}")

    # --- steps

    def step_api(self, step: Step) -> int:
        from . import cli

        self.p("App keys are access to MTProto, that is to the whole account.")
        self.p("Without them only the Bot API is left: you see just what was written to the")
        self.p("bot, but not your own chats, history and search.")
        self.p("Where to get them: https://my.telegram.org → API development tools → create")
        self.p("an application. Any name will do. The keys go into .env with mode 600.")
        if not self.interactive:
            self.p("There is no terminal, and the keys are typed by hand. Open a terminal and run:")
            self.p(f"   cd {config.ROOT} && uv run tg init")
            return 1
        values = cli.prompt_api_credentials()
        if not values:
            return 1
        config.write_env(values)
        self.p(f"   Written to {config.ENV_FILE}")
        return 0

    def step_login(self, step: Step) -> int:
        from . import cli

        if self.state["login_pending"]:
            self.p("The sign-in was started earlier and stopped at the 2FA cloud password.")
            self.p("The password is typed only from a live terminal and is saved nowhere:")
            self.p("   uv run tg password")
            return 1
        self.p("Now comes the sign-in to Telegram. The code arrives in the app itself (not SMS),")
        self.p("and you type it — the wizard does not request the code, does not fill it in and")
        self.p("does not store it. If two-step verification is on, it will ask for the cloud")
        self.p("password: that too is typed by hand and is written down nowhere.")
        self.p("")
        self.p(f"After the sign-in {self.state['session']} will appear — that is full access "
               "to the account")
        self.p("without a password and without 2FA. The file must not be copied to other "
               "machines:")
        self.p("Telegram will see two copies of one session and revoke it.")
        self.p("")
        if not self.interactive:
            self.p("The sign-in needs a terminal. Open a terminal and run:")
            self.p(f"   cd {config.ROOT} && uv run tg init")
            return 1
        try:
            code = cli.cmd_login(argparse.Namespace(account=self.account, brief=True))
        except (KeyboardInterrupt, EOFError):
            self.p("\n   Sign-in interrupted. To repeat: uv run tg login")
            return 1
        except Exception as exc:
            why = explain_login_error(exc)
            self.p(f"   Sign-in did not go through: {why or exc}")
            return 1
        return code

    def step_bot(self, step: Step) -> int:
        from . import cli

        self.p("The bot is needed as a back channel: alerts about important incoming messages,")
        self.p("the scheduled digest, the agent's questions (tg_ask) and write confirmations")
        self.p("arrive in it. Start a SEPARATE bot for the agent: @BotFather → /newbot. Somebody")
        self.p("else's bot cannot be reused — its messages become incoming for you too and")
        self.p("trigger an alert, which the next alert will answer (the daemon ignores")
        self.p("only its own bot, known by its token).")
        self.p("Without the bot everything else works; alerts, the digest and tg_ask just")
        self.p("silently disappear.")
        if self.state["bot_token"] and not self.state["alert_chat"]:
            self.p("The token is already there, only chat_id is missing.")
            if not self.yes("Link it now (you have to press Start at the bot)?", default=True):
                self.skip(step)
                return 0
            return self._link_bot()
        if not self.interactive:
            self.skip(step)
            return 0
        token = cli.prompt_bot_token()
        if not token:
            self.skip(step)
            return 0
        config.write_env({"TG_BOT_TOKEN": token})
        return self._link_bot()

    def _link_bot(self) -> int:
        """Linking chat_id. A failure here does not fail the installation: most
        often it is "did not press Start in time", and that is cured by one
        command later."""
        from . import cli

        if cli.cmd_link_bot(self.args):
            self.skipped.append(
                "linking the bot's chat_id: press Start at your bot and run uv run tg link-bot"
            )
        return 0

    def _optional_key(self, step: Step, name: str, why: str, where: str) -> int:
        self.p(why)
        if not self.interactive:
            self.skip(step)
            return 0
        self.p(f"Where to get it: {where}")
        value = getpass(f"   {name} (input hidden, Enter to skip): ").strip()
        if not value:
            self.skip(step)
            return 0
        config.write_env({name: value})
        self.p(f"   Written to {config.ENV_FILE}")
        return 0

    def step_memory_key(self, step: Step) -> int:
        return self._optional_key(
            step, "OPENAI_API_KEY",
            "Chat dossiers (tg_memory) are written by a language model — that is the only place\n"
            "where pieces of the correspondence leave the machine. Without the key the dossiers\n"
            "simply are not updated, everything else works as before. base_url can later be\n"
            "pointed at a local model — then the correspondence does not leave the machine\n"
            "(see .env.example).",
            "platform.openai.com/api-keys",
        )

    def step_groq(self, step: Step) -> int:
        return self._optional_key(
            step, "GROQ_API_KEY",
            "Groq transcribes voice messages, video notes, music and video. Without it what is\n"
            "left is Telegram's built-in transcription (voice messages and video notes only,\n"
            "Premium required) and the local model, if it is installed.",
            "console.groq.com/keys",
        )

    def step_local_whisper(self, step: Step) -> int:
        self.p("The local transcription model works without the internet and without keys, but")
        self.p("it takes up space and time to install. What gets installed is decided by the")
        self.p("system: on Apple Silicon it is mlx-whisper, on other hardware faster-whisper.")
        if not self.yes("Install it now (uv sync --extra local-whisper)?"):
            self.skip(step)
            return 0
        code, out = _run(["uv", "sync", "--extra", "local-whisper"], timeout=900)
        if code:
            self.p(f"   Did not install: {out.splitlines()[-1] if out else code}")
            self.p("   This is an optional step, the installation continues.")
            self.skipped.append(f"{step.title}: {step.fix}")
        return 0

    def step_daemon(self, step: Step) -> int:
        from . import cli

        self.p("The daemon owns the Telegram session and does all the work: the MCP server only")
        self.p("forwards calls to it through a unix socket. Without the daemon the tools do not")
        self.p("work, and alerts, the digest and reminders do not exist.")
        code = cli.cmd_daemon_start(self.args)
        if code:
            self.p("   The daemon did not come up. Common reasons:")
            self.p("   - the sign-in is not finished: uv run tg password")
            self.p("   - the daemon is already started by another copy of the project — "
                   "check: ps ax | grep tgagent")
            self.p(f"   - a socket file is left over from the previous run: rm {config.SOCKET}")
            self.p(f"   Full log: uv run tg daemon logs -n 50 ({config.DAEMON_LOG})")
        return code

    def step_mcp(self, step: Step) -> int:
        if not claude_bin():
            self.p("Claude Code (`claude`) is not in PATH — there is nothing to register the "
                   "server with.")
            self.p("If the client is a different one (Claude Desktop, your own), set it up "
                   "by hand:")
            self.p("   " + " ".join(mcp_add_command(config.ROOT)))
            self.p("   with a config — see docs/mcp.md")
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        if mcp_registered():
            # The state was taken before the first step, and a lot of time could
            # have passed between them. A second `mcp add` with the same name is
            # a client error, and it would look like a broken wizard.
            self.p("   The server is already registered — I am not adding it a second time.")
            return 0
        if not uv_bin():
            self.p("   uv is not found in the PATH of this shell. The command below is "
                   "started by the client:")
            self.p("   if uv is not visible to it either, the server will not start — put uv "
                   "into the common PATH.")
        self.p("The server has to be declared to the client once: the command and the project")
        self.p("directory the wizard knows itself. The user scope means the server will be "
               "available")
        self.p("in all projects; local would limit it to the current directory.")
        cmd = mcp_add_command(config.ROOT)
        self.p("   " + " ".join(cmd))
        code, out = _run(cmd)
        if code:
            self.p(f"   It did not work out: {out.splitlines()[-1] if out else code}")
            self.p("   Run the command above by hand and see what it says.")
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        self.p("   Registered. Claude Code reads the servers when a session starts —")
        self.p("   an already open one will have to be restarted, otherwise it will have no tools.")
        return 0

    def step_agents(self, step: Step) -> int:
        self.p("Subagents are ready-made roles for Claude Code: telegram (all the tools)")
        self.p("and telegram-watch (a trimmed set for background checks). The client reads them")
        self.p(f"from {AGENT_DIR}.")

        def ask(name: str) -> bool:
            self.p(f"   {name} is already there and differs from the version in the repository.")
            self.p("   A difference usually means an outdated tool set for the agent,")
            self.p("   but it may also be your own edit.")
            return self.yes(f"Overwrite {name}?")

        for row in install_agents(ask=ask):
            self.p(f"   {row['name']}: " + {
                "installed": "installed",
                "replaced": "updated",
                "kept": "left as it was",
                "same": "already up to date",
            }[row["action"]])
            if row["action"] == "kept":
                self.skipped.append(
                    f"subagent {row['name']} differs: "
                    f"cp {config.ROOT}/agents/{row['name']} {AGENT_DIR}/"
                )
        return 0

    def step_autostart(self, step: Step) -> int:
        kind = self.state["autostart_kind"]
        if not kind:
            self.p("The wizard can install autostart on macOS (launchd) and Linux (systemd);")
            self.p(f"this system is {sys.platform}. Keep the daemon in docker: there the role "
                   "of autostart")
            self.p("is played by restart: unless-stopped — see docs/docker.md.")
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        target = self.state["autostart_target"]
        self.p("Autostart raises the daemon at system sign-in so that alerts and")
        self.p(f"reminders work without Claude running. This is {kind} in")
        self.p(f"{target.parent}, and it does not require administrator rights.")
        if not self.yes("Install autostart?"):
            self.skip(step)
            return 0
        uv = uv_bin()
        if not uv:
            self.p(f"   uv is not found in PATH — {kind} needs an absolute path to it.")
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        template = self.state["autostart_template"]
        if not template.exists():
            self.p(f"   There is no template {template} — skipping.")
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_autostart(template.read_text(), uv, config.ROOT))
        code, out = self._enable_autostart(kind, target)
        if code:
            self.p(f"   The file is written ({target}), but switching it on did not work "
                   f"out: {out or code}")
            self.p(f"   By hand: {autostart_enable_command(kind)}")
            return 0
        self.p(f"   Installed: {target}")
        return 0

    def _enable_autostart(self, kind: str, target: Path) -> tuple[int, str]:
        """Switch on the unit that was just written. Separately from writing the
        file: the file is already on disk and useful on its own, even if
        switching it on did not work."""
        if kind == "launchd":
            # bootstrap is the modern form, load -w is the one that works on old
            # macOS; the second is tried only when the first has refused.
            code, out = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)])
            if code:
                code, out = _run(["launchctl", "load", "-w", str(target)])
            return code, out
        # systemd: without daemon-reload the new unit is simply not visible.
        code, out = _run(["systemctl", "--user", "daemon-reload"])
        if code:
            return code, out
        return _run(["systemctl", "--user", "enable", "--now", UNIT_NAME])

    # --- the pass

    def handlers(self) -> dict:
        """Step key -> what does it.

        A separate table rather than branching along the way: its completeness is
        an invariant. A step with no handler would drop the wizard in the middle
        of the installation, and that is checked by a test, not by the first
        unlucky run at a stranger's.
        """
        return {
            "api": self.step_api,
            "login": self.step_login,
            "bot": self.step_bot,
            "memory_key": self.step_memory_key,
            "groq": self.step_groq,
            "local_whisper": self.step_local_whisper,
            "daemon": self.step_daemon,
            "mcp": self.step_mcp,
            "agents": self.step_agents,
            "autostart": self.step_autostart,
        }

    def run(self) -> int:
        steps = plan(self.state)
        todo = [s for s in steps if not s.done]
        self.p("Setup wizard. It looks at what is already done and does only what is missing —")
        self.p('a repeat run breaks nothing and works as "fix my installation".')
        self.p("Only the app keys and the sign-in are required; the rest is skipped with Enter.")
        self.p("")
        for s in steps:
            mark = "done" if s.done else ("required" if s.required else "optional")
            self.p(f"  [{mark:10}] {s.title}" + (f" — {s.detail}" if s.done and s.detail else ""))
        if not todo:
            self.p("\nEverything is already set up, there is nothing to change.")
            self.finish()
            return 0

        handlers = self.handlers()
        for number, step in enumerate(todo, 1):
            self.p("\n" + "─" * 60)
            tail = "" if step.required else " (optional, Enter to skip)"
            self.p(f"{number}/{len(todo)}. {step.title}{tail}\n")
            try:
                code = handlers[step.key](step)
            except (KeyboardInterrupt, EOFError):
                self.p("\nInterrupted. What is already done is saved — run uv run tg init again.")
                return 1
            except Exception as exc:
                why = explain_login_error(exc)
                self.p(f"   The step did not pass: {why or exc}")
                code = 1 if step.required else 0
            if code and step.required:
                self.p("\nWithout this step you cannot go on. "
                       "Fix it and run the wizard again: uv run tg init")
                return 1
            # The state is recomputed after every step: the next step may depend
            # on the previous one (the daemon needs the keys, MCP a working daemon).
            self.state = probe(self.account, check_claude=False)
        self.finish()
        return 0

    def finish(self) -> None:
        from . import cli

        self.p("\n" + "─" * 60)
        self.p("Done. What is available now:\n")
        self.p(cli.capabilities_text(self.account))
        if self.skipped:
            self.p("\nSkipped (and how to switch it on if needed):")
            for line in self.skipped:
                self.p(f"  - {line}")
        self.p("\nTo check the whole installation: uv run tg doctor")


def cmd_init(args) -> int:
    config.ensure_dirs()
    return Wizard(args).run()


def cmd_doctor(args) -> int:
    """Diagnostics: asks nothing, changes nothing, reveals nothing."""
    from . import cli

    st = probe(getattr(args, "account", None))
    if st["socket"]:
        try:
            status = asyncio.run(cli._rpc("status"))
            st["rpc"] = {"ok": True, "sessions": len(status.get("accounts") or [])}
        except Exception as exc:
            st["rpc"] = {"ok": False, "error": str(exc)}
    rows = report(st)
    print(render(rows), flush=True)
    if any(r["status"] == _BAD for r in rows):
        print("\nmost of this is fixed by the wizard: uv run tg init", flush=True)
    print("\nThe report has no keys, no phone number and no account name — it can be "
          "attached to an issue as is.", flush=True)
    return 0
