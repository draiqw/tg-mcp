"""First run on an empty machine: no .env, no data/, no session.

This is the only state an outsider is guaranteed to see, and the only one in
which they do not yet know a single command of the project. So what is checked
here is not "it did not crash" but "it said what to do": every entry point (the
CLI, the daemon, the MCP server) must name the missing step and the exact
command instead of dumping a traceback.

A separate file, because the subject here is shared while the modules are
different: the hint is one for all of them, and it is exactly between the
modules that it drifts apart.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tgagent import cli, config, daemon, mcp_server


@pytest.fixture
def no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installation where absolutely nothing has been done.

    conftest sets TG_API_ID/TG_API_HASH for the whole run so that the tests do
    not reach into the real .env; here they are taken back out — otherwise an
    "empty" installation cannot be portrayed.
    """
    monkeypatch.delenv("TG_API_ID", raising=False)
    monkeypatch.delenv("TG_API_HASH", raising=False)


# --- one hint for every entry point -------------------------------------------


def test_without_keys_the_hint_leads_to_the_wizard(no_keys) -> None:
    hint = config.setup_hint()
    assert hint and "tg init" in hint
    # The project directory itself, not "go to the project directory": it must be
    # possible to run the command word for word, without working out where the
    # repository was cloned.
    assert str(config.ROOT) in hint


def test_keys_present_but_no_session_the_hint_leads_to_sign_in(data_dir: Path) -> None:
    hint = config.setup_hint()
    assert hint and hint.endswith(config.login_command())


def test_when_everything_is_in_place_there_is_no_hint(data_dir: Path) -> None:
    (data_dir / "session.session").write_text("")
    assert config.setup_hint() is None


def test_a_missing_key_is_a_setup_error_not_a_failure(no_keys) -> None:
    with pytest.raises(config.SetupError) as exc:
        config.require_env("TG_API_ID")
    # A subclass of RuntimeError: old handlers keep catching it as before, new
    # ones can tell an unfinished installation from a breakage.
    assert isinstance(exc.value, RuntimeError)
    assert "tg init" in str(exc.value)


def test_the_data_directory_is_created_together_with_its_parents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TG_DATA_DIR may point deep into a tree that does not exist."""
    deep = tmp_path / "nowhere" / "yet" / "data"
    monkeypatch.setattr(config, "DATA", deep)
    monkeypatch.setattr(config, "DOWNLOADS", deep / "downloads")
    config.ensure_dirs()
    assert deep.is_dir() and (deep / "downloads").is_dir()
    assert deep.stat().st_mode & 0o777 == 0o700


# --- MCP server ---------------------------------------------------------------


def test_mcp_on_an_empty_installation_calls_for_setup_not_for_starting_the_daemon(
    no_keys,
) -> None:
    """Claude Code starts the server before the person signs in.

    The advice "start the daemon" would be a dead end here: without keys and a
    session it will not start, and the person gets stuck on `tg daemon start`.
    """
    hint = mcp_server._daemon_hint()
    assert "tg init" in hint
    assert "daemon start" not in hint


def test_mcp_on_a_ready_installation_talks_about_the_daemon(data_dir: Path) -> None:
    (data_dir / "session.session").write_text("")
    hint = mcp_server._daemon_hint()
    assert "daemon start" in hint and "daemon logs" in hint


async def test_a_call_without_the_daemon_refuses_with_text_not_a_traceback(no_keys) -> None:
    with pytest.raises(RuntimeError) as exc:
        await mcp_server.call("status")
    assert "tg init" in str(exc.value)


def test_daemon_autostart_is_not_attempted_without_a_single_session(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        mcp_server.subprocess, "Popen",
        lambda *a, **kw: pytest.fail("the daemon was started without a session"),
    )
    mcp_server._try_autostart()


def test_daemon_autostart_fires_on_a_non_main_account_too(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default account can be any label, not only main."""
    (data_dir / "session-work.session").write_text("")
    started: list[list[str]] = []
    monkeypatch.setattr(mcp_server.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    monkeypatch.setattr(mcp_server, "AUTOSTART_TRIES", 1)
    monkeypatch.setattr(mcp_server, "AUTOSTART_PAUSE_SEC", 0)
    mcp_server._try_autostart()
    assert started and started[0][-1] == "tgagent.daemon"


# --- the CLI and the daemon ---------------------------------------------------


def test_daemon_start_refuses_immediately_not_after_eighteen_seconds(
    no_keys, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        cli.subprocess, "Popen",
        lambda *a, **kw: pytest.fail("the daemon was started without keys"),
    )
    monkeypatch.setattr(
        cli.time, "sleep", lambda _: pytest.fail("waiting instead of an answer")
    )
    assert cli.cmd_daemon_start(None) == 1
    assert "tg init" in capsys.readouterr().out


def test_the_daemon_does_not_print_a_traceback_on_an_unfinished_installation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    async def broken(self) -> None:
        raise config.SetupError(
            "There is no Telegram session at all. Sign in: uv run tg login"
        )

    monkeypatch.setattr(daemon.Daemon, "run", broken)
    with pytest.raises(SystemExit) as exc:
        daemon.main()
    out = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Traceback" not in out
    assert "uv run tg login" in out


async def test_the_daemon_explains_the_missing_session_instead_of_crashing(
    data_dir: Path,
) -> None:
    with pytest.raises(config.SetupError) as exc:
        await daemon.Daemon().run()
    assert config.login_command() in str(exc.value)


# --- an interrupted sign-in ---------------------------------------------------


class _AbortingClient:
    """Telethon in exactly the part the sign-in touches.

    The session file is created in connect() — like a real client does: it is
    precisely because of that an interrupted sign-in used to leave a stub file on
    disk.
    """

    def __init__(self, path: str, api_id: int, api_hash: str, **kw) -> None:
        self.path = Path(str(path) + ".session")
        self.disconnected = False

    async def connect(self) -> None:
        # A real client opens the existing database and creates a new one only if
        # there is none; a fake that always writes an empty file would hide
        # exactly the bug this test was written for.
        if not self.path.exists():
            self.path.write_text("")

    async def is_user_authorized(self) -> bool:
        return False

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def aborting_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sign-in that breaks off at the very first question (Ctrl-D, `< /dev/null`)."""
    import telethon

    monkeypatch.setattr(telethon, "TelegramClient", _AbortingClient)
    monkeypatch.setattr(
        "builtins.input", lambda *a: (_ for _ in ()).throw(EOFError())
    )


def test_an_interrupted_sign_in_leaves_no_session_file(
    aborting_login, data_dir: Path, capsys: pytest.CaptureFixture
) -> None:
    """A stub file on disk used to fool everything else.

    list_accounts, `tg doctor` and the wizard count an account by the presence of
    the file. As long as the file stayed, the wizard skipped the sign-in step and
    led to the daemon, which crashed on that session — and the person read a
    traceback instead of "sign in".
    """
    assert cli.cmd_login(argparse.Namespace(account=None, brief=False)) == 1
    assert not (data_dir / "session.session").exists()
    assert config.list_accounts() == []
    out = capsys.readouterr().out
    assert "Traceback" not in out
    assert config.login_command() in out


def test_an_interrupted_sign_in_does_not_touch_an_already_working_session(
    aborting_login, data_dir: Path
) -> None:
    """A repeated `tg login` on a live account is an everyday thing (to look at who
    is signed in, for example). Breaking it off must not cost the session."""
    session = data_dir / "session.session"
    session.write_text("working session")
    assert cli.cmd_login(argparse.Namespace(account=None, brief=False)) == 1
    assert session.read_text() == "working session"


def test_the_session_file_is_closed_to_others_from_the_first_second(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """The permissions used to be set only at the end of a successful sign-in, and
    between connect() and typing the code there are two questions to the person —
    all that time the file lay there with the default umask."""
    import telethon

    seen: list[int] = []

    class _CheckingClient(_AbortingClient):
        async def is_user_authorized(self) -> bool:
            seen.append(self.path.stat().st_mode & 0o777)
            return False

    monkeypatch.setattr(telethon, "TelegramClient", _CheckingClient)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    cli.cmd_login(argparse.Namespace(account=None, brief=False))
    assert seen == [0o600]
