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
from .i18n import t


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _onboarding(title: str, account: str | None = None) -> None:
    """Tail of setup and login: what came out and what is still missing.

    Printed where a person has just configured something and does not understand
    what exactly they got. The same text is available on its own:
    `tg capabilities`.
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
    _p(t("setup.api_where", head=head))
    api_id = input("   TG_API_ID: ").strip()
    api_hash = getpass(t("setup.api_hash_prompt")).strip()
    if not api_id.isdigit() or len(api_hash) < 20:
        _p(t("setup.api_bad_format"))
        return None
    return {"TG_API_ID": api_id, "TG_API_HASH": api_hash}


def prompt_bot_token(head: str | None = None) -> str:
    """Bot token, or an empty string if the bot was not wanted."""
    _p(t("setup.bot_where", head=head or t("setup.bot_head")))
    return getpass(t("setup.bot_token_prompt")).strip()


def cmd_setup(args) -> int:
    _p(t("setup.intro"))
    creds = prompt_api_credentials("1) api_id / api_hash")
    if not creds:
        return 1

    bot_token = prompt_bot_token(t("setup.step_bot"))

    values = {**creds, "TG_ALLOW_WRITE": "1"}
    if bot_token:
        values["TG_BOT_TOKEN"] = bot_token
    config.write_env(values)
    _p(t("setup.written", path=config.ENV_FILE))

    code = asyncio.run(_link_bot(bot_token)) if bot_token else 0
    _onboarding(t("setup.onboarding_title"))
    return code


async def _link_bot(token: str) -> int:
    from .alerts import BotChannel

    bot = BotChannel(token=token, chat_id=None)
    try:
        me = await bot.me()
        _p(t("setup.bot_confirmed", username=me["username"]))
        _p(t("setup.bot_start", username=me["username"]))
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
            _p(t("setup.bot_not_started"))
            return 1
        config.write_env({"TG_ALERT_CHAT_ID": chat_id})
        await bot.send(t("setup.alert_channel_linked"), chat_id)
        _p(t("setup.bot_linked", chat_id=chat_id))
        return 0
    finally:
        await bot.close()


def cmd_link_bot(args) -> int:
    token = config.bot_token()
    if not token:
        _p(t("setup.bot_token_missing"))
        return 1
    return asyncio.run(_link_bot(token))


# ---------------------------------------------------------------- login


def cmd_login(args) -> int:
    # Ctrl-C, Ctrl-D and `tg login < /dev/null` are not a program failure but the
    # ordinary way a person leaves a dialogue. A traceback on them frightens for
    # no reason, and it gets in the way of the `tg init` wizard: it catches them
    # itself and prints its own continuation.
    try:
        return asyncio.run(_login(args))
    except (KeyboardInterrupt, EOFError):
        _p(t("login.aborted", command=config.login_command(getattr(args, "account", None))))
        return 1


async def _login(args) -> int:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    config.ensure_dirs()
    try:
        api_id, api_hash = config.api_credentials()
    except Exception as exc:
        _p(str(exc))
        return 1

    session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
    # Telethon creates the session file in connect(), long before the code is
    # entered. An interrupted sign-in left a stub file on disk without
    # authorization — and list_accounts, `tg doctor` and the wizard all counted
    # the account as signed in on it and led to the daemon, which died on it.
    # So we remember whether the file was there before us, and remove our own.
    preexisting = session_file.exists()

    client = TelegramClient(
        str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
        **config.client_info(),
    )
    await client.connect()
    # Permissions are set right away, not only on the successful branch: the file
    # is already on disk, and there are two more questions to the person before
    # the sign-in ends — all that time it would be lying there with 644.
    if session_file.exists():
        session_file.chmod(0o600)
    # The `tg init` wizard prints the same digest itself, at the very end. Here it
    # would be a second copy in the middle of the installation — hence the flag,
    # not a separate function.
    brief = getattr(args, "brief", False)
    if await client.is_user_authorized():
        me = await client.get_me()
        _p(t("login.already", name=me.first_name, username=me.username))
        await client.disconnect()
        if not brief:
            _onboarding(t("login.onboarding_title"), getattr(args, "account", None))
        return 0

    signed_in = False
    try:
        # Blocking the loop on the two lines below is the whole point of the step:
        # this is a one-off sign-in from a terminal, nothing else spins in this
        # process, and waiting is exactly what we owe the person — asyncio has no
        # async replacement for input().
        phone = input(t("login.phone_prompt")).strip()  # noqa: ASYNC250 — waiting for a human
        sent = await client.send_code_request(phone)
        _p(t("login.code_sent"))
        code = input(t("login.code_prompt")).strip()  # noqa: ASYNC250 — waiting for a human
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        except SessionPasswordNeededError:
            pwd = getpass(t("login.password_prompt"))
            await client.sign_in(password=pwd)
        signed_in = True
        me = await client.get_me()
    finally:
        await client.disconnect()
        if not signed_in and not preexisting:
            session_file.unlink(missing_ok=True)

    session_file.chmod(0o600)
    _p(t("login.done", name=me.first_name, username=me.username, user_id=me.id))
    _p(t("login.session_file", path=session_file))
    # The account tier is known right here: it is a flag of the user who has just
    # signed in, no extra request is needed for it. The caps that follow from it
    # are shown by `tg capabilities` — those need a running daemon.
    _p(t("login.premium",
         value=t("login.premium_yes") if getattr(me, "premium", False) else t("login.premium_no")))
    if not brief:
        _p(t("login.next_daemon"))
        _onboarding(t("login.onboarding_title"), getattr(args, "account", None))
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
            _p(t("login.already_short", name=me.first_name, username=me.username))
            await client.disconnect()
            return 0
        sent = await client.send_code_request(args.phone)
        login_state(getattr(args, "account", None)).write_text(
            json.dumps({"phone": args.phone, "hash": sent.phone_code_hash})
        )
        login_state(getattr(args, "account", None)).chmod(0o600)
        await client.disconnect()
        _p(t("login.code_sent_to", phone=args.phone))
        return 0

    return asyncio.run(_run())


def cmd_sign_in(args) -> int:
    """Non-interactive step 2: complete the login with the received code."""

    async def _run() -> int:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError

        if not login_state(getattr(args, "account", None)).exists():
            _p(t("login.need_send_code"))
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
            pwd = args.password or getpass(t("login.password_2fa_prompt"))
            await client.sign_in(password=pwd)
        me = await client.get_me()
        await client.disconnect()
        login_state(getattr(args, "account", None)).unlink(missing_ok=True)
        session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
        if session_file.exists():
            session_file.chmod(0o600)
        _p(t("login.signed_in", name=me.first_name, username=me.username, user_id=me.id))
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
            _p(t("login.need_tty", root=config.ROOT))
            return 1

        api_id, api_hash = config.api_credentials()
        client = TelegramClient(
            str(config.session_path(getattr(args, "account", None))), api_id, api_hash,
            **config.client_info(),
        )
        await client.connect()
        if await client.is_user_authorized():
            me = await client.get_me()
            _p(t("login.already_short", name=me.first_name, username=me.username))
            await client.disconnect()
            return 0
        from telethon import functions
        from telethon.errors import PasswordHashInvalidError

        info = await client(functions.account.GetPasswordRequest())
        if info.hint:
            _p(t("login.password_hint", hint=info.hint))
        _p(t("login.password_explain"))

        me = None
        for attempt in range(1, 4):
            pwd = getpass(t("login.cloud_password_prompt", attempt=attempt))
            try:
                await client.sign_in(password=pwd)
                me = await client.get_me()
                break
            except PasswordHashInvalidError:
                _p(t("login.password_wrong"))
        if me is None:
            _p(t("login.password_failed"))
            await client.disconnect()
            return 1
        await client.disconnect()
        login_state(getattr(args, "account", None)).unlink(missing_ok=True)
        session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
        if session_file.exists():
            session_file.chmod(0o600)
        _p(t("login.signed_in", name=me.first_name, username=me.username, user_id=me.id))
        _p(t("login.next_daemon"))
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
            _p(t("account.session_revoked"))
        await client.disconnect()
        base = config.session_path(getattr(args, "account", None))
        for suffix in (".session", ".session-journal"):
            Path(str(base) + suffix).unlink(missing_ok=True)
        _p(t("account.session_files_removed"))
        # A default pointing at a deleted account is a refusal on every next
        # call. We return it to the main one right here, instead of leaving the
        # owner to deal with an error whose cause they have already forgotten.
        label = config.normalize_account(getattr(args, "account", None))
        if config.default_account() == label:
            config.set_default_account(config.MAIN_ACCOUNT)
            if label != config.MAIN_ACCOUNT:
                _p(t("account.default_reset_to_main"))
        # The index and the dossiers stay: they survive a re-login, and wiping
        # them silently along with the session is not allowed — this is
        # correspondence, not a service file.
        _p(t("account.index_kept",
             index=config.index_path(label), memory=config.memory_dir(label)))
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
        _p(t("daemon.already_running", pid=_daemon_pid()))
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
            _p(t("daemon.started", pid=_daemon_pid(), log=config.DAEMON_LOG))
            return 0
        time.sleep(0.3)
    _p(t("daemon.start_timeout"))
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
        _p(t("daemon.not_running"))
        config.SOCKET.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not _daemon_pid():
            _p(t("daemon.stopped"))
            return 0
        time.sleep(0.2)
    _p(t("daemon.stop_timeout"))
    return 1


def cmd_daemon_restart(args) -> int:
    cmd_daemon_stop(args)
    return cmd_daemon_start(args)


def cmd_daemon_logs(args) -> int:
    _p(_tail(config.DAEMON_LOG, args.lines))
    return 0


def _tail(path: Path, n: int) -> str:
    if not path.exists():
        return t("cli.no_file", path=path)
    lines = path.read_text(errors="replace").splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------- status


def cmd_accounts(args) -> int:
    found = config.list_accounts()
    if not found:
        _p(t("account.none", command=config.login_command()))
        return 1

    wanted = getattr(args, "default", None)
    if wanted is not None:
        label = config.normalize_account(wanted)
        if label not in found:
            _p(config.not_logged_in(label, found))
            return 1
        config.set_default_account(label)
        _p(t("account.default_set", label=label))
        if config.SOCKET.exists():
            _p(t("account.daemon_holds_all"))

    default = config.default_account()
    for label in found:
        mark = t("account.default_mark") if label == default else ""
        _p(f"{label:12} {config.session_path(label)}.session{mark}")
    _p(t("account.add_more", command=config.add_account_command()))
    _p(t("account.change_default"))
    return 0


def cmd_status(args) -> int:
    session_file = Path(str(config.session_path(getattr(args, "account", None))) + ".session")
    rows = [
        (t("status.row_accounts"), ", ".join(config.list_accounts()) or t("status.no_accounts")),
        (t("status.row_default"), config.default_account()),
        (
            ".env",
            t("status.yes")
            if config.ENV_FILE.exists()
            else (
                t("status.env_from_environment")   # the usual situation in docker
                if config.env("TG_API_ID")
                else t("status.env_missing")
            ),
        ),
        (
            "api_id/api_hash",
            t("status.creds_set") if config.env("TG_API_ID") else t("status.creds_missing"),
        ),
        (
            t("status.row_session"),
            t("status.session_pending_2fa")
            if login_state(getattr(args, "account", None)).exists()
            else (t("status.yes") if session_file.exists() else t("status.session_missing")),
        ),
        (
            t("status.row_bot"),
            t("status.bot_token_set") if config.bot_token() else t("status.bot_not_configured"),
        ),
        (t("status.row_alert_chat"), config.alert_chat_id() or t("status.alert_chat_unlinked")),
        (
            t("status.row_write"),
            t("status.write_allowed") if config.allow_write() else t("status.write_off"),
        ),
        (
            t("status.row_daemon"),
            t("status.daemon_running", pid=_daemon_pid())
            if _daemon_pid()
            else t("status.daemon_stopped"),
        ),
        (t("status.row_socket"), t("status.yes") if config.SOCKET.exists() else t("status.no")),
    ]
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        _p(f"{k.ljust(width)}  {v}")

    if config.SOCKET.exists():
        try:
            _p("\n" + json.dumps(asyncio.run(_rpc("status")), ensure_ascii=False, indent=2))
        except Exception as exc:
            _p(t("status.rpc_no_answer", error=exc))
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
            head = (t("status.daemon_old_code")
                    if "does not know method" in str(exc)
                    else t("status.daemon_no_answer", error=exc))
    else:
        head = t("status.daemon_not_started")
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
        _p(t("cli.error", error=exc))
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
    parser = argparse.ArgumentParser(prog="tg", description=t("cli.description"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    def with_account(p):
        """Every sign-in command can work with a second, third and so on account."""
        p.add_argument(
            "--account", default=None,
            help=t("cli.arg_account"),
        )
        return p

    # The wizard comes first on purpose: in `tg --help` it has to be met before
    # the steps it consists of — those are looked up separately, later.
    with_account(
        sub.add_parser("init", help=t("cli.cmd_init"))
    ).set_defaults(fn=cmd_init)
    with_account(
        sub.add_parser("doctor", help=t("cli.cmd_doctor"))
    ).set_defaults(fn=cmd_doctor)
    sub.add_parser("setup", help=t("cli.cmd_setup")).set_defaults(fn=cmd_setup)
    sub.add_parser("link-bot", help=t("cli.cmd_link_bot")).set_defaults(fn=cmd_link_bot)
    with_account(sub.add_parser("login", help=t("cli.cmd_login"))).set_defaults(
        fn=cmd_login
    )
    sc = with_account(sub.add_parser("send-code", help=t("cli.cmd_send_code")))
    sc.add_argument("phone")
    sc.set_defaults(fn=cmd_send_code)

    si = with_account(sub.add_parser("sign-in", help=t("cli.cmd_sign_in")))
    si.add_argument("--code", required=True)
    si.add_argument("--password", default=None, help=t("cli.arg_password"))
    si.set_defaults(fn=cmd_sign_in)

    with_account(
        sub.add_parser("password", help=t("cli.cmd_password"))
    ).set_defaults(fn=cmd_password)
    with_account(
        sub.add_parser("logout", help=t("cli.cmd_logout"))
    ).set_defaults(fn=cmd_logout)
    with_account(sub.add_parser("status", help=t("cli.cmd_status"))).set_defaults(
        fn=cmd_status
    )
    acc = sub.add_parser("accounts", help=t("cli.cmd_accounts"))
    acc.add_argument(
        "--default", default=None, metavar=t("cli.metavar_label"),
        help=t("cli.arg_default_account"),
    )
    acc.set_defaults(fn=cmd_accounts)
    with_account(
        sub.add_parser("capabilities", help=t("cli.cmd_capabilities"))
    ).set_defaults(fn=cmd_capabilities)

    d = sub.add_parser("daemon", help=t("cli.cmd_daemon"))
    dsub = d.add_subparsers(dest="action", required=True)
    dsub.add_parser("start").set_defaults(fn=cmd_daemon_start)
    dsub.add_parser("run", help=t("cli.cmd_daemon_run")).set_defaults(
        fn=cmd_daemon_run
    )
    dsub.add_parser("stop").set_defaults(fn=cmd_daemon_stop)
    dsub.add_parser("restart").set_defaults(fn=cmd_daemon_restart)
    # `tg daemon status` is what people type first, even though the state lives
    # in `tg status`. Cheaper to accept both forms than to explain why one of
    # them is the wrong one.
    with_account(dsub.add_parser("status", help=t("cli.cmd_daemon_status"))).set_defaults(
        fn=cmd_status
    )
    logs = dsub.add_parser("logs")
    logs.add_argument("-n", "--lines", type=int, default=40)
    logs.set_defaults(fn=cmd_daemon_logs)

    c = with_account(sub.add_parser("call", help=t("cli.cmd_call")))
    c.add_argument("method")
    c.add_argument("params", nargs="?", help=t("cli.arg_call_params"))
    c.set_defaults(fn=cmd_call)

    args = parser.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
