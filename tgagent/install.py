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
from .i18n import t

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
# here. The values are catalog keys, not texts: the owner reads these, so the
# wording lives in `i18n` and is resolved at the moment of the failure.
LOGIN_HINTS: dict[str, str] = {
    "PHONE_CODE_INVALID": "login.hint_code_invalid",
    "PHONE_CODE_EXPIRED": "login.hint_code_expired",
    "PHONE_CODE_EMPTY": "login.hint_code_empty",
    "PHONE_NUMBER_INVALID": "login.hint_phone_invalid",
    "PHONE_NUMBER_BANNED": "login.hint_phone_banned",
    "PASSWORD_HASH_INVALID": "login.hint_password_invalid",
    "SESSION_PASSWORD_NEEDED": "login.hint_password_needed",
    "API_ID_INVALID": "login.hint_api_id_invalid",
    "API_ID_PUBLISHED_FLOOD": "login.hint_api_id_flood",
    "AUTH_KEY_DUPLICATED": "login.hint_auth_key_duplicated",
    "AUTH_KEY_UNREGISTERED": "login.hint_auth_key_unregistered",
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
        return 127, t("init.no_command", cmd=cmd[0])
    except subprocess.TimeoutExpired:
        return 124, t("init.no_answer", cmd=cmd[0], timeout=timeout)
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
            key="api", title=t("init.step_api_title"),
            required=True, done=st["api"],
            detail=t("init.step_api_done"),
            cost=t("init.step_api_cost"),
            fix="uv run tg setup",
        ),
        Step(
            key="login", title=t("init.step_login_title"),
            # An unfinished sign-in leaves login_state.json behind: the session
            # file is already there, there is no authorization in it yet. To
            # count such a step as done means leading the person to the daemon,
            # which will fall over on this session.
            required=True, done=st["session_exists"] and not st["login_pending"],
            detail=t("init.step_login_done", session=st["session"]),
            cost=t("init.step_login_cost"),
            fix="uv run tg login",
        ),
        Step(
            key="bot", title=t("init.step_bot_title"),
            required=False, done=st["bot_token"] and st["alert_chat"],
            detail=t("init.step_bot_done"),
            cost=t("init.step_bot_cost"),
            fix=t("init.step_bot_fix"),
        ),
        Step(
            key="memory_key", title=t("init.step_memory_key_title"),
            required=False, done=st["openai"],
            detail=t("init.detail_set"),
            cost=t("init.step_memory_key_cost"),
            fix=t("setup.add_openai_key"),
        ),
        Step(
            key="groq", title=t("init.step_groq_title"),
            required=False, done=st["groq"],
            detail=t("init.detail_set"),
            cost=t("init.step_groq_cost"),
            fix=t("setup.add_groq_key"),
        ),
        Step(
            key="local_whisper", title=t("init.step_local_whisper_title"),
            required=False, done=bool(st["local_whisper"]),
            detail=str(st["local_whisper"]),
            cost=t("init.step_local_whisper_cost"),
            fix="uv sync --extra local-whisper",
        ),
        Step(
            key="daemon", title=t("init.step_daemon_title"),
            required=True, done=bool(st["daemon_pid"]),
            detail=t("init.step_daemon_done", pid=st["daemon_pid"]),
            cost=t("init.step_daemon_cost"),
            fix="uv run tg daemon start",
        ),
        Step(
            key="mcp", title=t("init.step_mcp_title"),
            required=False, done=st["mcp"] is True,
            detail=t("init.step_mcp_done"),
            cost=t("init.step_mcp_cost"),
            fix=" ".join(mcp_add_command(st["root"])),
        ),
        Step(
            key="agents", title=t("init.step_agents_title"),
            required=False, done=agents_done,
            detail=t("init.step_agents_done"),
            cost=t("init.step_agents_cost"),
            fix=f"cp {st['root']}/agents/*.md {AGENT_DIR}/",
        ),
        Step(
            key="autostart", title=t("init.step_autostart_title"),
            required=False, done=st["autostart"],
            detail=t("init.step_autostart_done", target=st["autostart_target"])
            if st["autostart_target"] else "",
            cost=t("init.step_autostart_cost"),
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
        return t("init.autostart_none")
    return t("init.autostart_manual", template=st["autostart_template"],
             dir=st["autostart_target"].parent, enable=autostart_enable_command(kind))


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
            return t(LOGIN_HINTS[code])
    return caps.explain_error(exc)


# ---------------------------------------------------------------- diagnostics

# Verdicts of a row. These are internal tokens: the owner sees the localized
# labels that `render` picks for them.
_OK, _BAD, _SKIP = "ok", "bad", "skip"


def _row(key: str, section: str, status: str, text: str, fix: str | None = None) -> dict:
    return {"key": key, "section": section, "status": status, "text": text, "fix": fix}


def report(st: dict) -> list[dict]:
    """The diagnostics report as rows. Not one secret in the text — only facts:
    the report is written so that it is attached to an issue whole."""
    rows: list[dict] = []
    add = rows.append

    sec_install = t("doctor.section_install")
    add(_row("root", sec_install, _OK, t("doctor.root", path=st["root"])))
    add(_row("python", sec_install, _OK, f"python {st['python']}"))
    add(_row(
        "uv", sec_install,
        _OK if st["uv"] else _BAD,
        f"uv: {st['uv']}" if st["uv"] else t("doctor.uv_missing"),
        None if st["uv"] else t("doctor.uv_fix"),
    ))
    if st["env_exists"]:
        mode = st["env_mode"]
        open_to_others = bool(mode and mode & 0o077)
        add(_row(
            "env", sec_install, _BAD if open_to_others else _OK,
            t("doctor.env_mode", mode=f"{mode:o}") if mode else t("doctor.env_ok"),
            t("doctor.env_fix", path=st["env_file"]) if open_to_others else None,
        ))
    else:
        add(_row(
            "env", sec_install, _SKIP if st["api"] else _BAD,
            t("doctor.env_from_environment") if st["api"] else t("doctor.env_missing"),
            None if st["api"] else "uv run tg init",
        ))
    mode = st["data_mode"]
    add(_row(
        "data", sec_install,
        _BAD if mode and mode & 0o077 else _OK,
        t("doctor.data", path=st["data"])
        + (t("doctor.data_mode", mode=f"{mode:o}") if mode else t("doctor.data_absent")),
        t("doctor.data_fix", path=st["data"]) if mode and mode & 0o077 else None,
    ))

    sec_keys = t("doctor.section_keys")
    add(_row(
        "api", sec_keys, _OK if st["api"] else _BAD,
        t("doctor.api_set") if st["api"] else t("doctor.api_unset"),
        None if st["api"] else t("doctor.api_fix"),
    ))
    add(_row(
        "write", sec_keys, _OK,
        t("doctor.write_on") if st["allow_write"] else t("doctor.write_off"),
    ))

    sec_accounts = t("doctor.section_accounts")
    if st["accounts"]:
        add(_row("accounts", sec_accounts, _OK,
                 t("doctor.accounts", accounts=", ".join(st["accounts"]),
                   default=config.default_account())))
    else:
        add(_row("accounts", sec_accounts, _BAD, t("doctor.accounts_none"),
                 "uv run tg login"))
    if st["session_exists"]:
        mode = st["session_mode"]
        add(_row(
            "session", sec_accounts,
            _BAD if mode and mode & 0o077 else _OK,
            t("doctor.session_mode", account=st["account"], mode=f"{mode:o}") if mode
            else t("doctor.session_ok", account=st["account"]),
            t("doctor.session_fix", path=st["session"]) if mode and mode & 0o077 else None,
        ))
    if st["login_pending"]:
        add(_row("login_pending", sec_accounts, _BAD,
                 t("doctor.login_pending"),
                 t("doctor.login_pending_fix")))

    sec_daemon = t("doctor.section_daemon")
    add(_row(
        "daemon", sec_daemon, _OK if st["daemon_pid"] else _BAD,
        t("doctor.daemon_running", pid=st["daemon_pid"]) if st["daemon_pid"]
        else t("doctor.daemon_stopped"),
        None if st["daemon_pid"] else "uv run tg daemon start",
    ))
    if st["socket"] and not st["daemon_pid"]:
        add(_row("socket", sec_daemon, _BAD, t("doctor.socket_stale"),
                 "uv run tg daemon restart"))
    if "rpc" in st:
        rpc = st["rpc"]
        add(_row(
            "rpc", sec_daemon, _OK if rpc.get("ok") else _BAD,
            t("doctor.rpc_ok", sessions=rpc.get("sessions")) if rpc.get("ok")
            else t("doctor.rpc_bad", error=rpc.get("error")),
            None if rpc.get("ok") else t("doctor.rpc_fix"),
        ))

    if st["claude"]:
        add(_row("claude", "Claude Code", _OK, f"claude: {st['claude']}"))
        add(_row(
            "mcp", "Claude Code", _OK if st["mcp"] else _BAD,
            t("doctor.mcp_registered", name=MCP_NAME) if st["mcp"]
            else t("doctor.mcp_missing", name=MCP_NAME),
            None if st["mcp"] else " ".join(mcp_add_command(st["root"])),
        ))
    else:
        add(_row("claude", "Claude Code", _SKIP, t("doctor.claude_missing"),
                 t("doctor.claude_missing_fix")))
    for row in st["agents"]:
        state = row["state"]
        add(_row(
            f"agent:{row['name']}", "Claude Code",
            _OK if state == "same" else _BAD,
            {
                "same": t("doctor.agent_same", name=row["name"]),
                "differs": t("doctor.agent_differs", name=row["name"]),
                "missing": t("doctor.agent_missing", name=row["name"]),
                "no-source": t("doctor.agent_no_source", name=row["name"]),
            }[state],
            None if state == "same"
            else (t("doctor.agent_no_source_fix") if state == "no-source"
                  else t("doctor.agent_fix",
                         src=f"{st['root']}/agents/{row['name']}", dst=row["dst"])),
        ))

    sec_optional = t("doctor.section_optional")
    add(_row(
        "bot", sec_optional,
        _OK if st["bot_token"] and st["alert_chat"] else _SKIP,
        t("doctor.bot_ok") if st["bot_token"] and st["alert_chat"]
        else (t("doctor.bot_no_chat") if st["bot_token"] else t("doctor.bot_missing")),
        None if st["bot_token"] and st["alert_chat"]
        else (t("doctor.bot_link_fix") if st["bot_token"] else t("doctor.bot_fix")),
    ))
    add(_row(
        "openai", sec_optional, _OK if st["openai"] else _SKIP,
        t("doctor.openai_set") if st["openai"] else t("doctor.openai_unset"),
        None if st["openai"] else t("setup.add_openai_key"),
    ))
    add(_row(
        "groq", sec_optional, _OK if st["groq"] else _SKIP,
        t("doctor.groq_set") if st["groq"] else t("doctor.groq_unset"),
        None if st["groq"] else t("setup.add_groq_key"),
    ))
    add(_row(
        "local_whisper", sec_optional, _OK if st["local_whisper"] else _SKIP,
        t("doctor.whisper_ok", what=st["local_whisper"]) if st["local_whisper"]
        else t("doctor.whisper_missing"),
        None if st["local_whisper"] else "uv sync --extra local-whisper",
    ))
    if st["autostart_kind"]:
        add(_row(
            "autostart", sec_optional, _OK if st["autostart"] else _SKIP,
            t("doctor.autostart_ok", kind=st["autostart_kind"]) if st["autostart"]
            else t("doctor.autostart_missing", kind=st["autostart_kind"]),
            None if st["autostart"] else t("doctor.autostart_fix"),
        ))
    return rows


def render(rows: list[dict]) -> str:
    """The report as text: by section, with an indent under "what to do"."""
    out: list[str] = []
    label = {
        _OK: t("doctor.status_ok"),
        _BAD: t("doctor.status_bad"),
        _SKIP: t("doctor.status_skip"),
    }
    section = None
    for row in rows:
        if row["section"] != section:
            section = row["section"]
            out.append(f"\n{section}")
        out.append(f"  [{label[row['status']]:5}] {row['text']}")
        if row["fix"]:
            out.append(f"           → {row['fix']}")
    bad = sum(1 for r in rows if r["status"] == _BAD)
    out.append("")
    out.append(t("doctor.all_good") if not bad else t("doctor.bad_count", n=bad))
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
        # The affirmative words come from the catalog: an owner who read the
        # Russian question answers in Russian, and dropping those words would
        # break the answer, not the wording.
        return answer in ("y", "yes") or answer in t("init.yes_words").split()

    def skip(self, step: Step) -> None:
        self.p("   " + t("init.skipped", cost=step.cost))
        self.skipped.append(f"{step.title}: {step.fix}")

    # --- steps

    def step_api(self, step: Step) -> int:
        from . import cli

        self.p(t("init.api_intro"))
        if not self.interactive:
            self.p(t("init.needs_terminal_keys"))
            self.p(f"   cd {config.ROOT} && uv run tg init")
            return 1
        values = cli.prompt_api_credentials()
        if not values:
            return 1
        config.write_env(values)
        self.p("   " + t("init.written_to", path=config.ENV_FILE))
        return 0

    def step_login(self, step: Step) -> int:
        from . import cli

        if self.state["login_pending"]:
            self.p(t("init.login_pending"))
            self.p("   uv run tg password")
            return 1
        self.p(t("init.login_intro"))
        self.p("")
        self.p(t("init.login_session", session=self.state["session"]))
        self.p("")
        if not self.interactive:
            self.p(t("init.needs_terminal_login"))
            self.p(f"   cd {config.ROOT} && uv run tg init")
            return 1
        try:
            code = cli.cmd_login(argparse.Namespace(account=self.account, brief=True))
        except (KeyboardInterrupt, EOFError):
            self.p("\n   " + t("init.login_interrupted"))
            return 1
        except Exception as exc:
            why = explain_login_error(exc)
            self.p("   " + t("init.login_failed", why=why or exc))
            return 1
        return code

    def step_bot(self, step: Step) -> int:
        from . import cli

        self.p(t("init.bot_intro"))
        if self.state["bot_token"] and not self.state["alert_chat"]:
            self.p(t("init.bot_token_only"))
            if not self.yes(t("init.bot_link_ask"), default=True):
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
            self.skipped.append(t("init.bot_link_skipped"))
        return 0

    def _optional_key(self, step: Step, name: str, why: str, where: str) -> int:
        self.p(why)
        if not self.interactive:
            self.skip(step)
            return 0
        self.p(t("init.where_to_get", where=where))
        value = getpass("   " + t("init.key_prompt", name=name) + ": ").strip()
        if not value:
            self.skip(step)
            return 0
        config.write_env({name: value})
        self.p("   " + t("init.written_to", path=config.ENV_FILE))
        return 0

    def step_memory_key(self, step: Step) -> int:
        return self._optional_key(
            step, "OPENAI_API_KEY",
            t("init.memory_key_why"),
            "platform.openai.com/api-keys",
        )

    def step_groq(self, step: Step) -> int:
        return self._optional_key(
            step, "GROQ_API_KEY",
            t("init.groq_why"),
            "console.groq.com/keys",
        )

    def step_local_whisper(self, step: Step) -> int:
        self.p(t("init.local_whisper_intro"))
        if not self.yes(t("init.local_whisper_ask")):
            self.skip(step)
            return 0
        code, out = _run(["uv", "sync", "--extra", "local-whisper"], timeout=900)
        if code:
            self.p("   " + t("init.install_failed", why=out.splitlines()[-1] if out else code))
            self.p("   " + t("init.optional_continue"))
            self.skipped.append(f"{step.title}: {step.fix}")
        return 0

    def step_daemon(self, step: Step) -> int:
        from . import cli

        self.p(t("init.daemon_intro"))
        code = cli.cmd_daemon_start(self.args)
        if code:
            self.p("   " + t("init.daemon_failed"))
            self.p("   - " + t("init.daemon_reason_login"))
            self.p("   - " + t("init.daemon_reason_running"))
            self.p("   - " + t("init.daemon_reason_socket", path=config.SOCKET))
            self.p("   " + t("init.daemon_log", path=config.DAEMON_LOG))
        return code

    def step_mcp(self, step: Step) -> int:
        if not claude_bin():
            self.p(t("init.mcp_no_claude"))
            self.p("   " + " ".join(mcp_add_command(config.ROOT)))
            self.p("   " + t("init.mcp_by_config"))
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        if mcp_registered():
            # The state was taken before the first step, and a lot of time could
            # have passed between them. A second `mcp add` with the same name is
            # a client error, and it would look like a broken wizard.
            self.p("   " + t("init.mcp_already"))
            return 0
        if not uv_bin():
            self.p("   " + t("init.mcp_no_uv"))
            self.p("   " + t("init.mcp_no_uv_note"))
        self.p(t("init.mcp_intro"))
        cmd = mcp_add_command(config.ROOT)
        self.p("   " + " ".join(cmd))
        code, out = _run(cmd)
        if code:
            self.p("   " + t("init.mcp_failed", why=out.splitlines()[-1] if out else code))
            self.p("   " + t("init.mcp_run_by_hand"))
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        self.p("   " + t("init.mcp_done"))
        self.p("   " + t("init.mcp_restart_client"))
        return 0

    def step_agents(self, step: Step) -> int:
        self.p(t("init.agents_intro", dir=AGENT_DIR))

        def ask(name: str) -> bool:
            self.p("   " + t("init.agent_differs", name=name))
            self.p("   " + t("init.agent_differs_why"))
            self.p("   " + t("init.agent_differs_yours"))
            return self.yes(t("init.agent_overwrite_ask", name=name))

        for row in install_agents(ask=ask):
            self.p(f"   {row['name']}: " + {
                "installed": t("init.agent_installed"),
                "replaced": t("init.agent_replaced"),
                "kept": t("init.agent_kept"),
                "same": t("init.agent_same"),
            }[row["action"]])
            if row["action"] == "kept":
                self.skipped.append(t(
                    "init.agent_kept_skipped", name=row["name"],
                    cmd=f"cp {config.ROOT}/agents/{row['name']} {AGENT_DIR}/",
                ))
        return 0

    def step_autostart(self, step: Step) -> int:
        kind = self.state["autostart_kind"]
        if not kind:
            self.p(t("init.autostart_unsupported", platform=sys.platform))
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        target = self.state["autostart_target"]
        self.p(t("init.autostart_intro", kind=kind, dir=target.parent))
        if not self.yes(t("init.autostart_ask")):
            self.skip(step)
            return 0
        uv = uv_bin()
        if not uv:
            self.p("   " + t("init.autostart_no_uv", kind=kind))
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        template = self.state["autostart_template"]
        if not template.exists():
            self.p("   " + t("init.autostart_no_template", path=template))
            self.skipped.append(f"{step.title}: {step.fix}")
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_autostart(template.read_text(), uv, config.ROOT))
        code, out = self._enable_autostart(kind, target)
        if code:
            self.p("   " + t("init.autostart_not_enabled", path=target, why=out or code))
            self.p("   " + t("init.autostart_by_hand", cmd=autostart_enable_command(kind)))
            return 0
        self.p("   " + t("init.autostart_installed", path=target))
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
        self.p(t("init.wizard_intro"))
        self.p("")
        for s in steps:
            mark = t("init.mark_done") if s.done else (
                t("init.mark_required") if s.required else t("init.mark_optional"))
            self.p(f"  [{mark:10}] {s.title}" + (f" — {s.detail}" if s.done and s.detail else ""))
        if not todo:
            self.p("\n" + t("init.nothing_to_do"))
            self.finish()
            return 0

        handlers = self.handlers()
        for number, step in enumerate(todo, 1):
            self.p("\n" + "─" * 60)
            tail = "" if step.required else " " + t("init.optional_tail")
            self.p(f"{number}/{len(todo)}. {step.title}{tail}\n")
            try:
                code = handlers[step.key](step)
            except (KeyboardInterrupt, EOFError):
                self.p("\n" + t("init.interrupted"))
                return 1
            except Exception as exc:
                why = explain_login_error(exc)
                self.p("   " + t("init.step_failed", why=why or exc))
                code = 1 if step.required else 0
            if code and step.required:
                self.p("\n" + t("init.required_step_failed"))
                return 1
            # The state is recomputed after every step: the next step may depend
            # on the previous one (the daemon needs the keys, MCP a working daemon).
            self.state = probe(self.account, check_claude=False)
        self.finish()
        return 0

    def finish(self) -> None:
        from . import cli

        self.p("\n" + "─" * 60)
        self.p(t("init.finish") + "\n")
        self.p(cli.capabilities_text(self.account))
        if self.skipped:
            self.p("\n" + t("init.skipped_list"))
            for line in self.skipped:
                self.p(f"  - {line}")
        self.p("\n" + t("init.check_all"))


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
        print("\n" + t("doctor.fix_hint"), flush=True)
    print("\n" + t("doctor.no_secrets"), flush=True)
    return 0
