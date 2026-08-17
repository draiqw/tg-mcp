"""Command line for setup, login and daemon control.

Credential entry (`tg setup`, `tg login`) is meant to be run by you in your own
terminal: the agent never sees your login code, 2FA password or tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from getpass import getpass
from pathlib import Path

from . import config


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _onboarding(title: str, account: str | None = None) -> None:
    """Tail of setup and login: what came out and what is still missing.

    Printed where a person has just configured something and does not understand
    what exactly they got. The same text is available on its own:
    `uv run tg capabilities`.
    """
    _p("\n" + "─" * 60)
    _p(title + "\n")
    _p(capabilities_text(account))
    _p("─" * 60)


# ---------------------------------------------------------------- setup


def prompt_api_credentials(head: str = "api_id / api_hash") -> dict[str, str] | None:
    """Ask for api_id/api_hash and check their shape. None — the input was wrong.

    Separate from `cmd_setup`, because the `tg init` wizard asks the same
    question: two copies of one question would drift apart in both the text and
    the check.
    """
    _p(f"{head}: https://my.telegram.org → API development tools")
    api_id = input("   TG_API_ID: ").strip()
    api_hash = getpass("   TG_API_HASH (hidden input): ").strip()
    if not api_id.isdigit() or len(api_hash) < 20:
        _p("   api_id must be a number, api_hash a long string. Aborted.")
        return None
    return {"TG_API_ID": api_id, "TG_API_HASH": api_hash}


def prompt_bot_token(head: str = "notification bot") -> str:
    """Bot token, or an empty string if the bot was not wanted."""
    _p(f"{head}: @BotFather → /newbot → copy the token")
    return getpass("   TG_BOT_TOKEN (hidden input, Enter to skip): ").strip()


def cmd_setup(args) -> int:
    _p("Telegram agent setup. Values are written to .env (chmod 600), "
       "nothing leaves the machine.\n")
    creds = prompt_api_credentials("1) api_id / api_hash")
    if not creds:
        return 1

    bot_token = prompt_bot_token("\n2) notification bot")

    values = {**creds, "TG_ALLOW_WRITE": "1"}
    if bot_token:
        values["TG_BOT_TOKEN"] = bot_token
    config.write_env(values)
    _p(f"\n   Written to {config.ENV_FILE}")

    code = asyncio.run(_link_bot(bot_token)) if bot_token else 0
    _onboarding("What is set up so far")
    return code


async def _link_bot(token: str) -> int:
    from .alerts import BotChannel

    bot = BotChannel(token=token, chat_id=None)
    try:
        me = await bot.me()
        _p(f"   Bot confirmed: @{me['username']}")
        _p(f"\n3) Open https://t.me/{me['username']} and press Start "
           "(waiting up to 120 seconds)...")
        deadline = time.time() + 120
        chat_id = None
        while time.time() < deadline and not chat_id:
            for upd in await bot.poll(timeout=20):
                msg = upd.get("message") or {}
                cid = msg.get("chat", {}).get("id")
                if cid:
                    chat_id = str(cid)
                    break
        if not chat_id:
            _p("   Gave up waiting. Press Start and run `uv run tg link-bot`.")
            return 1
        config.write_env({"TG_ALERT_CHAT_ID": chat_id})
        await bot.send("Alert channel connected.", chat_id)
        _p(f"   Done: alerts will go to chat_id {chat_id}")
        return 0
    finally:
        await bot.close()


def cmd_link_bot(args) -> int:
    token = config.bot_token()
    if not token:
        _p("TG_BOT_TOKEN is not set — run `uv run tg setup`.")
        return 1
    return asyncio.run(_link_bot(token))


# ---------------------------------------------------------------- login


def cmd_login(args) -> int:
    return asyncio.run(_login(args))


async def _login(args) -> int:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    config.ensure_dirs()
    try:
        api_id, api_hash = config.api_credentials()
    except Exception as exc:
        _p(str(exc))
        return 1

    client = TelegramClient(
        str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
        **config.client_info(),
    )
    await client.connect()
    # The `tg init` wizard prints the same digest itself, at the very end. Here it
    # would be a second copy in the middle of the installation — hence the flag,
    # not a separate function.
    brief = getattr(args, "brief", False)
    if await client.is_user_authorized():
        me = await client.get_me()
        _p(f"Already signed in: {me.first_name} (@{me.username}). The session is in place.")
        await client.disconnect()
        if not brief:
            _onboarding("What you can do", getattr(args, "account", None))
        return 0

    # Blocking the loop on the two lines below is the whole point of the step: this
    # is a one-off sign-in from a terminal, nothing else spins in this process,
    # and waiting is exactly what we owe the person — asyncio has no async
    # replacement for input() anyway.
    phone = input("Phone in +79991234567 format: ").strip()  # noqa: ASYNC250 — waiting for a human
    sent = await client.send_code_request(phone)
    _p("Code sent to Telegram (not SMS — check the app).")
    code = input("Code: ").strip()  # noqa: ASYNC250 — waiting for a human
    try:
        await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
    except SessionPasswordNeededError:
        pwd = getpass("Two-factor password (hidden input): ")
        await client.sign_in(password=pwd)
    me = await client.get_me()
    await client.disconnect()

    session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
    if session_file.exists():
        session_file.chmod(0o600)
    _p(f"\nDone: signed in as {me.first_name} (@{me.username}, id {me.id}).")
    _p("Session: " + str(session_file))
    # The account tier is known right here: it is a flag of the user who has just
    # signed in, no extra request is needed for it. The caps that follow from it
    # are shown by `tg capabilities` — those need a running daemon.
    _p("Telegram Premium: " + ("yes" if getattr(me, "premium", False) else "no"))
    if not brief:
        _p("Next: uv run tg daemon start")
        _onboarding("What you can do", getattr(args, "account", None))
    return 0


def login_state(account: str | None = None) -> Path:
    """Intermediate sign-in state, one per account."""
    label = config.normalize_account(account)
    name = "login_state.json" if label == config.MAIN_ACCOUNT else f"login_state-{label}.json"
    return config.DATA / name


def cmd_send_code(args) -> int:
    """Non-interactive step 1: request the login code for a phone number."""

    async def _run() -> int:
        from telethon import TelegramClient

        config.ensure_dirs()
        api_id, api_hash = config.api_credentials()
        client = TelegramClient(
            str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
            **config.client_info(),
        )
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            _p(f"Already signed in as {me.first_name} (@{me.username}).")
            await client.disconnect()
            return 0
        sent = await client.send_code_request(args.phone)
        login_state(getattr(args, "account", None)).write_text(
            json.dumps({"phone": args.phone, "hash": sent.phone_code_hash})
        )
        login_state(getattr(args, "account", None)).chmod(0o600)
        await client.disconnect()
        _p(f"Code sent to {args.phone} (to the Telegram app). "
           "Next: tg sign-in --code XXXXX")
        return 0

    return asyncio.run(_run())


def cmd_sign_in(args) -> int:
    """Non-interactive step 2: complete the login with the received code."""

    async def _run() -> int:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        if not login_state(getattr(args, "account", None)).exists():
            _p("Run tg send-code <phone> first.")
            return 1
        state = json.loads(login_state(getattr(args, "account", None)).read_text())
        api_id, api_hash = config.api_credentials()
        client = TelegramClient(
            str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
            **config.client_info(),
        )
        await client.connect()
        try:
            await client.sign_in(state["phone"], args.code, phone_code_hash=state["hash"])
        except SessionPasswordNeededError:
            pwd = args.password or getpass("2FA password (hidden input): ")
            await client.sign_in(password=pwd)
        me = await client.get_me()
        await client.disconnect()
        login_state(getattr(args, "account", None)).unlink(missing_ok=True)
        session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
        if session_file.exists():
            session_file.chmod(0o600)
        _p(f"Signed in as {me.first_name} (@{me.username}, id {me.id}).")
        return 0

    return asyncio.run(_run())


def cmd_password(args) -> int:
    """Finish a login that stopped at the two-factor password prompt.

    Run this yourself in a real terminal: the password is read straight from the
    tty and never appears in an argument, a log or the agent's context.
    """

    async def _run() -> int:
        from telethon import TelegramClient

        if not sys.stdin.isatty():
            _p("A real terminal is required: open a terminal and run there\n"
               f"  cd {config.ROOT} && uv run tg password")
            return 1

        api_id, api_hash = config.api_credentials()
        client = TelegramClient(
            str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
            **config.client_info(),
        )
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            _p(f"Already signed in as {me.first_name} (@{me.username}).")
            await client.disconnect()
            return 0
        from telethon import functions
        from telethon.errors import PasswordHashInvalidError

        info = await client(functions.account.GetPasswordRequest())
        if info.hint:
            _p(f"Telegram password hint: {info.hint}")
        _p("This is the Telegram cloud password for two-step verification "
           "(Settings → Privacy and Security → Two-Step Verification),\n"
           "not the password of your Apple ID, your mail, or a code from an SMS.\n")

        me = None
        for attempt in range(1, 4):
            pwd = getpass(f"Cloud 2FA password (attempt {attempt}/3, hidden input): ")
            try:
                await client.sign_in(password=pwd)
                me = await client.get_me()
                break
            except PasswordHashInvalidError:
                _p("Wrong password.")
        if me is None:
            _p("\nNot signed in. If the password is forgotten — reset it in the Telegram app:\n"
               "Settings → Privacy and Security → Two-Step Verification → Forgot password,\n"
               "then repeat: uv run tg send-code <your number>")
            await client.disconnect()
            return 1
        await client.disconnect()
        login_state(getattr(args, "account", None)).unlink(missing_ok=True)
        session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
        if session_file.exists():
            session_file.chmod(0o600)
        _p(f"Signed in as {me.first_name} (@{me.username}, id {me.id}).")
        _p("Next: uv run tg daemon start")
        return 0

    return asyncio.run(_run())


def cmd_logout(args) -> int:
    async def _run() -> int:
        from telethon import TelegramClient

        api_id, api_hash = config.api_credentials()
        session = str(config.session_path(getattr(args, "account", None)))
        client = TelegramClient(session, api_id, api_hash)
        await client.connect()
        if await client.is_user_authorized():
            await client.log_out()
            _p("Session revoked on the Telegram side.")
        await client.disconnect()
        base = config.session_path(getattr(args, "account", None))
        for suffix in (".session", ".session-journal"):
            Path(str(base) + suffix).unlink(missing_ok=True)
        _p("Local session files deleted.")
        # A default pointing at a deleted account is a refusal on every next
        # call. We return it to the main one right here, instead of leaving the
        # owner to deal with an error whose cause they have already forgotten.
        label = config.normalize_account(getattr(args, "account", None))
        if config.default_account() == label:
            config.set_default_account(config.MAIN_ACCOUNT)
            if label != config.MAIN_ACCOUNT:
                _p("It was the default account — the default is back to main.")
        # The index and the dossiers stay: they survive a re-login, and wiping
        # them silently along with the session is not allowed — this is
        # correspondence, not a service file.
        _p(f"The index and dossiers of this account are untouched: {config.index_path(label)}, "
           f"{config.memory_dir(label)}")
        return 0

    cmd_daemon_stop(args)
    return asyncio.run(_run())


# ---------------------------------------------------------------- daemon


def _daemon_pid() -> int | None:
    if not config.PID_FILE.exists():
        return None
    try:
        pid = int(config.PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        return None


def cmd_daemon_start(args) -> int:
    if _daemon_pid():
        _p(f"The daemon is already running (pid {_daemon_pid()}).")
        return 0
    # Checked before the start, not after: without keys and without a session the
    # daemon is guaranteed to die, and without the check the person would wait 18
    # seconds only to read a traceback at the end instead of one line with a
    # command.
    hint = config.setup_hint()
    if hint:
        _p(hint)
        return 1
    config.ensure_dirs()
    config.SOCKET.unlink(missing_ok=True)
    with config.DAEMON_LOG.open("a") as logfh:
        subprocess.Popen(
            [sys.executable, "-m", "tgagent.daemon"],
            cwd=str(config.ROOT), stdout=logfh, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    for _ in range(60):
        if config.SOCKET.exists():
            _p(f"Daemon started (pid {_daemon_pid()}). Log: {config.DAEMON_LOG}")
            return 0
        time.sleep(0.3)
    _p("The daemon did not come up in 18 seconds. Last lines of the log:")
    _p(_tail(config.DAEMON_LOG, 25))
    return 1


def cmd_daemon_run(args) -> int:
    """Run the daemon in the foreground — this is what the container executes."""
    from . import daemon

    daemon.main()
    return 0


def cmd_daemon_stop(args) -> int:
    pid = _daemon_pid()
    if not pid:
        _p("The daemon is not running.")
        config.SOCKET.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not _daemon_pid():
            _p("Daemon stopped.")
            return 0
        time.sleep(0.2)
    _p("Did not stop on SIGTERM.")
    return 1


def cmd_daemon_restart(args) -> int:
    cmd_daemon_stop(args)
    return cmd_daemon_start(args)


def cmd_daemon_logs(args) -> int:
    _p(_tail(config.DAEMON_LOG, args.lines))
    return 0


def _tail(path: Path, n: int) -> str:
    if not path.exists():
        return f"(no file {path})"
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- status


def cmd_accounts(args) -> int:
    found = config.list_accounts()
    if not found:
        _p(f"Not a single account. Sign in: {config.login_command()}  "
           "(a second one: --account work)")
        return 1

    wanted = getattr(args, "default", None)
    if wanted is not None:
        label = config.normalize_account(wanted)
        if label not in found:
            _p(config.not_logged_in(label, found))
            return 1
        config.set_default_account(label)
        _p(f"Default account: {label}. Holds across restarts too.")
        if config.SOCKET.exists():
            _p("The daemon already holds every account, no need to restart it.")

    default = config.default_account()
    for label in found:
        mark = " ← default" if label == default else ""
        _p(f"{label:12} {config.session_path(label)}.session{mark}")
    _p(f"\nAdd another: {config.add_account_command()}")
    _p("Change the default: uv run tg accounts --default <label>")
    return 0


def cmd_status(args) -> int:
    session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
    rows = [
        ("accounts", ", ".join(config.list_accounts()) or "none"),
        ("default", config.default_account()),
        (
            ".env",
            "yes"
            if config.ENV_FILE.exists()
            else (
                "no file, values from the environment"   # the usual situation in docker
                if config.env("TG_API_ID")
                else "MISSING — run tg setup"
            ),
        ),
        ("api_id/api_hash", "set" if config.env("TG_API_ID") else "MISSING"),
        (
            "session",
            "sign-in unfinished — needs tg password (2FA)"
            if login_state(getattr(args, "account", None)).exists()
            else ("yes" if session_file.exists() else "MISSING — run tg login"),
        ),
        ("bot", "token set" if config.bot_token() else "not configured"),
        ("alert chat", config.alert_chat_id() or "not linked — tg link-bot"),
        ("write", "allowed" if config.allow_write() else "off"),
        ("daemon", f"running (pid {_daemon_pid()})" if _daemon_pid() else "not running"),
        ("socket", "yes" if config.SOCKET.exists() else "no"),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        _p(f"{k.ljust(width)}  {v}")

    if config.SOCKET.exists():
        try:
            _p("\n" + json.dumps(asyncio.run(_rpc("status")), ensure_ascii=False, indent=2))
        except Exception as exc:
            _p(f"\nRPC is not answering: {exc}")
    return 0


def capabilities_text(account: str | None = None) -> str:
    """Digest of "what is available", in human text.

    The full one goes through the daemon: only the server knows the subscription
    and the Telegram caps. Without the daemon (and right after `tg setup` and
    `tg login` there usually is none) the local half is shown, with an honest
    note about what is missing from it.
    """
    from . import capabilities as caps

    head = ""
    if config.SOCKET.exists():
        try:
            return caps.render(asyncio.run(_rpc("capabilities", account=account)))
        except Exception as exc:
            # A separate case, because after an update it is the most frequent
            # one: the daemon is spinning on code that did not have this method
            # yet. The phrase matched here is the wording the daemon replies
            # with — the two sides have to stay in step.
            head = ("The daemon is running an old version of the code — restart it: "
                    "uv run tg daemon restart.\n\n"
                    if "does not know method" in str(exc)
                    else f"The daemon did not answer ({exc}).\n\n")
    else:
        head = ("The daemon is not running — run `uv run tg daemon start`, "
                "then `uv run tg capabilities`.\n\n")
    local = caps.local_state()
    rows = caps.restrictions(local)
    return head + caps.render({
        "local": {"nature": caps.NATURES["local"], **local},
        "summary": caps.summary(rows, partial=True),
        "restricted": rows,
    })


def cmd_capabilities(args) -> int:
    _p(capabilities_text(getattr(args, "account", None)))
    return 0


async def _rpc(method: str, params: dict | None = None, account: str | None = None):
    # Parameters go as a dict, not as **kwargs: the tools have a `method`
    # parameter (`tg_actions`), and on **kwargs such a call would fail with
    # "multiple values".
    import aiohttp

    connector = aiohttp.UnixConnector(path=str(config.SOCKET))
    payload = {"method": method, "params": params or {}, "account": account}
    async with aiohttp.ClientSession(connector=connector) as sess:
        async with sess.post("http://tg/call", json=payload) as r:
            data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error"))
    return data["result"]


def cmd_call(args) -> int:
    params = json.loads(args.params) if args.params else {}
    try:
        result = asyncio.run(_rpc(args.method, params, account=getattr(args, "account", None)))
        _p(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        _p(f"Error: {exc}")
        return 1


# ---------------------------------------------------------------- init / doctor


def cmd_init(args) -> int:
    """Setup wizard. Lives in `tgagent/install.py`, here only the entry into it:
    it reuses the commands of this module, and the back dependency is held by a
    deferred import, not by the order of lines."""
    from . import install

    return install.cmd_init(args)


def cmd_doctor(args) -> int:
    from . import install

    return install.cmd_doctor(args)


# ---------------------------------------------------------------- entry


def main() -> None:
    parser = argparse.ArgumentParser(prog="tg", description="Telegram agent control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_account(p):
        """Every sign-in command can work with a second, third and so on account."""
        p.add_argument(
            "--account", default=None,
            help="account label: main by default, for example work or second",
        )
        return p

    # The wizard comes first on purpose: in `tg --help` it has to be met before
    # the steps it consists of — those are looked up separately, later.
    with_account(
        sub.add_parser("init", help="setup wizard: walk through everything up to a working state")
    ).set_defaults(fn=cmd_init)
    with_account(
        sub.add_parser(
            "doctor",
            help="installation diagnostics: what is in place, what is broken, what to do",
        )
    ).set_defaults(fn=cmd_doctor)
    sub.add_parser("setup", help="enter api_id/api_hash and the bot token").set_defaults(
        fn=cmd_setup
    )
    sub.add_parser("link-bot", help="link the chat_id for alerts").set_defaults(fn=cmd_link_bot)
    with_account(sub.add_parser("login", help="sign in to a Telegram account")).set_defaults(
        fn=cmd_login
    )
    sc = with_account(sub.add_parser("send-code", help="sign-in step 1: request the code"))
    sc.add_argument("phone")
    sc.set_defaults(fn=cmd_send_code)

    si = with_account(sub.add_parser("sign-in", help="sign-in step 2: confirm the code"))
    si.add_argument("--code", required=True)
    si.add_argument("--password", default=None, help="2FA password, if it is enabled")
    si.set_defaults(fn=cmd_sign_in)

    with_account(
        sub.add_parser("password", help="step 3: enter the cloud 2FA password")
    ).set_defaults(fn=cmd_password)
    with_account(
        sub.add_parser("logout", help="revoke the session and delete the files")
    ).set_defaults(fn=cmd_logout)
    with_account(sub.add_parser("status", help="state of the installation")).set_defaults(
        fn=cmd_status
    )
    acc = sub.add_parser("accounts", help="which accounts are signed in")
    acc.add_argument(
        "--default", default=None, metavar="LABEL",
        help="remember this account as the default for all clients and restarts",
    )
    acc.set_defaults(fn=cmd_accounts)
    with_account(
        sub.add_parser("capabilities", help="what is available and what is missing")
    ).set_defaults(fn=cmd_capabilities)

    d = sub.add_parser("daemon", help="daemon control")
    dsub = d.add_subparsers(dest="action", required=True)
    dsub.add_parser("start").set_defaults(fn=cmd_daemon_start)
    dsub.add_parser("run", help="in the foreground (for docker/launchd)").set_defaults(
        fn=cmd_daemon_run
    )
    dsub.add_parser("stop").set_defaults(fn=cmd_daemon_stop)
    dsub.add_parser("restart").set_defaults(fn=cmd_daemon_restart)
    logs = dsub.add_parser("logs")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.set_defaults(fn=cmd_daemon_logs)

    c = with_account(sub.add_parser("call", help="call a daemon method directly"))
    c.add_argument("method")
    c.add_argument("params", nargs="?", help='JSON, e.g. \'{"limit": 5}\'')
    c.set_defaults(fn=cmd_call)

    args = parser.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
