"""The setup wizard and the diagnostics: everything that is checked without the
network and without Telegram.

The sign-in itself is not checked here — it consists of the owner typing a code
and a password, and it cannot be automated, neither in a test nor in the code.
What is checked is what usually breaks the wizard at a stranger's: the choice of
steps by the current state, a repeat run, assembling the registration command,
installing the subagents and the diagnostics report — including the fact that it
holds no secrets.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from tgagent import config, install


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Not a single subprocess and not a single foreign directory.

    Without this `probe` would poke the real `claude` and compare the subagents
    with the ones installed for whoever ran the tests.
    """
    monkeypatch.setattr(install, "claude_bin", lambda: None)
    monkeypatch.setattr(install, "uv_bin", lambda: "/usr/bin/uv")
    monkeypatch.setattr(install, "AGENT_DIR", tmp_path / "agents-home")
    monkeypatch.setattr(install, "LAUNCH_AGENTS", tmp_path / "LaunchAgents")
    monkeypatch.setattr(
        install, "_run",
        lambda cmd, timeout=60: pytest.fail(f"a subprocess in a test: {cmd}"),
    )


def state(**over) -> dict:
    """A snapshot of an installation in which nothing has been done. The fields are
    listed explicitly: a forgotten field must break the test, not silently read
    as False."""
    base = {
        "root": config.ROOT,
        "python": "3.13.0",
        "uv": "/usr/bin/uv",
        "env_file": config.ENV_FILE,
        "env_exists": False,
        "env_mode": None,
        "data": config.DATA,
        "data_mode": 0o700,
        "api": False,
        "allow_write": True,
        "account": "main",
        "accounts": [],
        "session": config.DATA / "session.session",
        "session_exists": False,
        "session_mode": None,
        "login_pending": False,
        "bot_token": False,
        "alert_chat": False,
        "openai": False,
        "groq": False,
        "local_whisper": None,
        "daemon_pid": None,
        "socket": False,
        "claude": None,
        "mcp": None,
        "agents": [{"name": n, "state": "missing", "dst": Path("/tmp") / n}
                   for n in install.AGENT_FILES],
        "autostart": False,
        "autostart_kind": "launchd",
        "autostart_template": config.ROOT / install.PLIST_NAME,
        "autostart_target": install.LAUNCH_AGENTS / install.PLIST_NAME,
    }
    unknown = set(over) - set(base)
    assert not unknown, f"no such state fields: {sorted(unknown)}"
    base.update(over)
    return base


def done_state(**over) -> dict:
    """An installation in which everything has been done."""
    full = {
        "api": True, "env_exists": True, "env_mode": 0o600, "accounts": ["main"],
        "session_exists": True, "session_mode": 0o600, "bot_token": True,
        "alert_chat": True, "openai": True, "groq": True,
        "local_whisper": "mlx_whisper", "daemon_pid": 4242, "socket": True,
        "claude": "/usr/bin/claude", "mcp": True, "autostart": True,
        "agents": [{"name": n, "state": "same", "dst": Path("/tmp") / n}
                   for n in install.AGENT_FILES],
    }
    return state(**{**full, **over})


# --- the registration command for Claude Code --------------------------------


def test_mcp_add_command_knows_the_project_path() -> None:
    cmd = install.mcp_add_command("/home/someone/tg-agent")
    assert cmd[:6] == ["claude", "mcp", "add", "-s", "user", "telegram"]
    # Everything after `--` goes as the server command: the client options and the
    # uv options are separated exactly this way, otherwise `--directory` would be
    # eaten by claude itself.
    assert cmd[cmd.index("--") + 1:] == [
        "uv", "--directory", "/home/someone/tg-agent", "run", "tg-mcp",
    ]


def test_mcp_add_command_defaults_to_this_checkout() -> None:
    assert str(config.ROOT) in install.mcp_add_command()


def test_mcp_registered_without_claude_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Not False: "there is no client" and "there is a client and no server in it"
    # are different things, and the wizard behaves differently on them.
    assert install.mcp_registered() is None


def test_mcp_registered_asks_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install, "claude_bin", lambda: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return (0 if cmd[-1] == "telegram" else 1), ""

    monkeypatch.setattr(install, "_run", fake_run)
    assert install.mcp_registered() is True
    assert install.mcp_registered("nosuchserver") is False
    assert calls[0][:3] == ["claude", "mcp", "get"]


# --- subagents ---------------------------------------------------------------


def test_install_agents_copies_missing(tmp_path: Path) -> None:
    target = tmp_path / "agents"
    actions = {r["name"]: r["action"] for r in install.install_agents(target)}
    assert set(actions.values()) == {"installed"}
    for name in install.AGENT_FILES:
        assert (target / name).read_bytes() == (config.ROOT / "agents" / name).read_bytes()


def test_install_agents_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "agents"
    install.install_agents(target)
    again = {r["name"]: r["action"] for r in install.install_agents(target, ask=lambda n: True)}
    # A matching file is not asked about again and not rewritten: a repeat run of
    # the wizard must not look like a change.
    assert set(again.values()) == {"same"}


def test_install_agents_asks_about_a_changed_file(tmp_path: Path) -> None:
    target = tmp_path / "agents"
    install.install_agents(target)
    changed = target / install.AGENT_FILES[0]
    changed.write_text("a hand edit\n")

    asked: list[str] = []
    kept = install.install_agents(target, ask=lambda name: asked.append(name) or False)
    assert asked == [changed.name]
    assert changed.read_text() == "a hand edit\n"
    assert {r["name"]: r["action"] for r in kept}[changed.name] == "kept"

    replaced = install.install_agents(target, ask=lambda name: True)
    assert {r["name"]: r["action"] for r in replaced}[changed.name] == "replaced"
    assert changed.read_bytes() == (config.ROOT / "agents" / changed.name).read_bytes()


def test_agent_rows_sees_a_difference(tmp_path: Path) -> None:
    target = tmp_path / "agents"
    install.install_agents(target)
    (target / install.AGENT_FILES[0]).write_text("something else\n")
    rows = {r["name"]: r["state"] for r in install.agent_rows(target)}
    assert rows[install.AGENT_FILES[0]] == "differs"
    assert rows[install.AGENT_FILES[1]] == "same"


# --- choosing the steps ------------------------------------------------------


def test_nothing_done_means_every_step_pending() -> None:
    steps = install.pending(state())
    assert [s.key for s in steps] == [s.key for s in install.plan(state())]
    required = [s.key for s in steps if s.required]
    # Exactly three are required: the keys, the sign-in and the daemon. All the
    # rest is optional, and that is a property of the wizard, not a detail of the
    # text.
    assert required == ["api", "login", "daemon"]


def test_everything_done_means_no_steps() -> None:
    assert install.pending(done_state()) == []


def test_steps_are_skipped_one_by_one() -> None:
    """Idempotence step by step: every finished thing removes exactly its own step."""
    for key, patch in [
        ("api", {"api": True}),
        ("login", {"session_exists": True}),
        ("bot", {"bot_token": True, "alert_chat": True}),
        ("memory_key", {"openai": True}),
        ("groq", {"groq": True}),
        ("local_whisper", {"local_whisper": "mlx_whisper"}),
        ("daemon", {"daemon_pid": 7}),
        ("mcp", {"mcp": True}),
        ("autostart", {"autostart": True}),
    ]:
        keys = [s.key for s in install.pending(state(**patch))]
        assert key not in keys, key
        # Exactly one step went away: something done must not close the neighbouring steps.
        assert len(keys) == len(install.plan(state())) - 1, key


def test_half_linked_bot_is_not_done() -> None:
    # A token without a chat_id is the most frequent half-setup: there is a bot,
    # and there is nowhere to send to it.
    keys = [s.key for s in install.pending(state(bot_token=True))]
    assert "bot" in keys


def test_changed_subagent_reopens_its_step() -> None:
    rows = [{"name": install.AGENT_FILES[0], "state": "differs", "dst": Path("/tmp/a")},
            {"name": install.AGENT_FILES[1], "state": "same", "dst": Path("/tmp/b")}]
    assert "agents" in [s.key for s in install.pending(done_state(agents=rows))]


def test_every_step_has_a_handler() -> None:
    wizard = install.Wizard.__new__(install.Wizard)
    assert set(install.Wizard.handlers(wizard)) == {s.key for s in install.plan(state())}


def test_pending_steps_say_what_breaks_without_them() -> None:
    for step in install.plan(state()):
        assert step.cost, step.key
        assert step.fix, step.key


# --- live state --------------------------------------------------------------


def test_probe_reads_the_sandbox(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    st = install.probe()
    assert st["data"] == data_dir
    assert st["session_exists"] is False
    assert st["daemon_pid"] is None
    assert st["mcp"] is None                      # there is no claude in PATH
    assert {r["state"] for r in st["agents"]} == {"missing"}

    (data_dir / "session.session").write_text("x")
    assert install.probe()["session_exists"] is True


def test_probe_matches_pending(data_dir: Path) -> None:
    """`init` and `doctor` look at one snapshot: they must not diverge."""
    st = install.probe()
    keys = {s.key for s in install.pending(st)}
    assert "login" in keys and "daemon" in keys
    assert "api" not in keys                      # the keys are set in the test environment


# --- the diagnostics report --------------------------------------------------


def rows_by_key(st: dict) -> dict[str, dict]:
    return {r["key"]: r for r in install.report(st)}


def test_report_on_empty_install_is_all_problems() -> None:
    rows = rows_by_key(state())
    assert rows["api"]["status"] == install._BAD
    assert rows["accounts"]["status"] == install._BAD
    assert rows["daemon"]["status"] == install._BAD
    assert rows["claude"]["status"] == install._SKIP
    # The optional is "skip", not "bad": its absence is not a breakage.
    assert rows["groq"]["status"] == install._SKIP
    assert rows["openai"]["status"] == install._SKIP


def test_report_on_full_install_has_no_problems() -> None:
    rows = install.report(done_state())
    assert [r for r in rows if r["status"] == install._BAD] == []
    assert "everything is in place" in install.render(rows)


def test_report_flags_loose_permissions() -> None:
    rows = rows_by_key(done_state(env_mode=0o644, session_mode=0o644, data_mode=0o755))
    for key in ("env", "session", "data"):
        assert rows[key]["status"] == install._BAD, key
        assert "chmod" in rows[key]["fix"], key


def test_report_flags_a_stale_socket() -> None:
    rows = rows_by_key(state(socket=True))
    assert rows["socket"]["status"] == install._BAD
    assert "restart" in rows["socket"]["fix"]


def test_report_flags_unfinished_login() -> None:
    rows = rows_by_key(state(login_pending=True))
    assert rows["login_pending"]["status"] == install._BAD
    assert "tg password" in rows["login_pending"]["fix"]


def test_report_flags_unregistered_mcp() -> None:
    rows = rows_by_key(done_state(claude="/usr/bin/claude", mcp=False))
    assert rows["mcp"]["status"] == install._BAD
    assert "claude mcp add" in rows["mcp"]["fix"]


def test_report_flags_stale_subagents() -> None:
    rows = rows_by_key(done_state(agents=[
        {"name": install.AGENT_FILES[0], "state": "differs", "dst": Path("/tmp/a")},
        {"name": install.AGENT_FILES[1], "state": "missing", "dst": Path("/tmp/b")},
    ]))
    for name in install.AGENT_FILES:
        assert rows[f"agent:{name}"]["status"] == install._BAD


def test_report_counts_problems() -> None:
    text = install.render(install.report(state()))
    assert "bad:" in text


def test_report_keeps_no_secrets(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """The report is written so that it is attached to an issue whole — which means
    it must hold neither keys, nor tokens, nor the phone number, nor the account
    name."""
    secrets = {
        "TG_API_HASH": "hash-" + "s" * 30,
        "TG_BOT_TOKEN": "1234567890:AA-secret-token",
        "TG_ALERT_CHAT_ID": "987654321",
        "OPENAI_API_KEY": "sk-secret",
        "GROQ_API_KEY": "gsk-secret",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    text = install.render(install.report(install.probe()))
    for value in secrets.values():
        assert value not in text


def test_doctor_prints_and_returns_zero(capsys: pytest.CaptureFixture, data_dir: Path) -> None:
    code = install.cmd_doctor(argparse.Namespace(account=None))
    out = capsys.readouterr().out
    assert code == 0
    assert "daemon" in out
    assert "uv run tg init" in out               # in one line, what to do
    assert "issue" in out


# --- step errors -------------------------------------------------------------


def fake_error(name: str, text: str = ""):
    return type(name, (Exception,), {})(text)


@pytest.mark.parametrize(
    ("name", "expect"),
    [
        ("PhoneCodeInvalidError", "the code did not fit"),
        ("PhoneCodeExpiredError", "the code has gone stale"),
        ("PasswordHashInvalidError", "uv run tg password"),
        ("ApiIdInvalidError", "my.telegram.org"),
        ("AuthKeyDuplicatedError", "another machine"),
    ],
)
def test_login_errors_explain_the_way_out(name: str, expect: str) -> None:
    assert expect in install.explain_login_error(fake_error(name))


def test_flood_wait_falls_back_to_the_common_table() -> None:
    exc = fake_error("FloodWaitError", "FLOOD_WAIT_42")
    exc.seconds = 42
    assert "42" in install.explain_login_error(exc)


def test_unknown_error_is_not_invented() -> None:
    # None is a refusal to explain: an invented cause is worse than one shown as it is.
    assert install.explain_login_error(fake_error("CompletelyUnfamiliarError")) is None


# --- wizard behaviour --------------------------------------------------------


def wizard(monkeypatch: pytest.MonkeyPatch, interactive: bool = False,
           answer: bool | None = None) -> install.Wizard:
    """A wizard without a terminal. `answer` substitutes the answers to the yes/no
    questions — otherwise a non-interactive wizard takes the default everywhere,
    and the default is almost everywhere "no", so the steps that have to be
    checked whole never get to the point."""
    w = install.Wizard(argparse.Namespace(account=None))
    w.interactive = interactive
    if answer is not None:
        w.yes = lambda prompt, default=False: answer
    return w


def test_wizard_without_a_terminal_takes_defaults(monkeypatch: pytest.MonkeyPatch,
                                                  data_dir: Path) -> None:
    w = wizard(monkeypatch)
    # Asks nothing and switches nothing on: Enter and the absence of a terminal
    # mean one and the same thing — "no need".
    assert w.yes("install it?") is False
    assert w.yes("install it?", default=True) is True


def test_wizard_records_what_was_skipped(monkeypatch: pytest.MonkeyPatch,
                                         data_dir: Path,
                                         capsys: pytest.CaptureFixture) -> None:
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "groq")
    w.skip(step)
    assert w.skipped == [f"{step.title}: {step.fix}"]
    assert step.cost in capsys.readouterr().out


def test_wizard_requires_a_terminal_for_credentials(monkeypatch: pytest.MonkeyPatch,
                                                    data_dir: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "api")
    assert w.step_api(step) == 1
    out = capsys.readouterr().out
    assert "my.telegram.org" in out
    assert "Bot API" in out                     # what you lose without the keys is explained
    assert "uv run tg init" in out


def test_wizard_login_step_explains_the_session_file(monkeypatch: pytest.MonkeyPatch,
                                                     data_dir: Path,
                                                     capsys: pytest.CaptureFixture) -> None:
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "login")
    assert w.step_login(step) == 1              # without a terminal the sign-in is not started
    out = capsys.readouterr().out
    assert "not SMS" in out
    assert "must not be copied" in out


def test_wizard_login_step_points_at_the_password_command(monkeypatch: pytest.MonkeyPatch,
                                                          data_dir: Path,
                                                          capsys: pytest.CaptureFixture) -> None:
    from tgagent import cli

    cli.login_state(None).write_text("{}")
    w = wizard(monkeypatch, interactive=True)
    step = next(s for s in install.plan(state()) if s.key == "login")
    assert w.step_login(step) == 1
    assert "uv run tg password" in capsys.readouterr().out


def test_wizard_mcp_step_without_claude_only_prints(monkeypatch: pytest.MonkeyPatch,
                                                    data_dir: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "mcp")
    assert w.step_mcp(step) == 0                # an optional step does not fail the wizard
    out = capsys.readouterr().out
    assert " ".join(install.mcp_add_command(config.ROOT)) in out
    assert w.skipped


def test_wizard_registers_mcp_once(monkeypatch: pytest.MonkeyPatch, data_dir: Path,
                                   capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(install, "claude_bin", lambda: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return (1, "") if cmd[2] == "get" else (0, "")

    monkeypatch.setattr(install, "_run", fake_run)
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "mcp")
    assert w.step_mcp(step) == 0
    assert calls[-1][:3] == ["claude", "mcp", "add"]
    assert "--" in calls[-1]


def test_wizard_does_not_register_mcp_twice(monkeypatch: pytest.MonkeyPatch,
                                            data_dir: Path,
                                            capsys: pytest.CaptureFixture) -> None:
    """An already registered server is not added a second time: `mcp add` with the
    same name is a client error, and it looks like a broken wizard."""
    monkeypatch.setattr(install, "claude_bin", lambda: "/usr/bin/claude")
    calls: list[list[str]] = []

    def fake_run(cmd, timeout=60):
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(install, "_run", fake_run)
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "mcp")
    assert w.step_mcp(step) == 0
    assert {c[2] for c in calls} == {"get"}


def test_wizard_survives_a_failed_bot_link(monkeypatch: pytest.MonkeyPatch, data_dir: Path,
                                           tmp_path: Path,
                                           capsys: pytest.CaptureFixture) -> None:
    """A Start not pressed in time must not fail the installation — only leave a
    line in "skipped"."""
    from tgagent import cli

    # write_env also writes the value into the process environment. The empty value
    # is set through monkeypatch for the sake of cleanup: only this way does the
    # teardown remove what write_env writes — otherwise the token from this test
    # would be seen by the next ones.
    monkeypatch.setenv("TG_BOT_TOKEN", "")
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(cli, "prompt_bot_token", lambda head="": "1234567890:AA-token")
    monkeypatch.setattr(cli, "cmd_link_bot", lambda args: 1)
    w = wizard(monkeypatch, interactive=True)
    step = next(s for s in install.plan(state()) if s.key == "bot")
    assert w.step_bot(step) == 0
    assert "tg link-bot" in w.skipped[0]
    assert "1234567890:AA-token" in (tmp_path / ".env").read_text()
    # And why the bot has to be a separate one, the person read before the question.
    out = capsys.readouterr().out
    assert "@BotFather" in out
    assert "alerts" in out


def test_wizard_run_end_to_end_without_a_terminal(monkeypatch: pytest.MonkeyPatch,
                                                  data_dir: Path,
                                                  capsys: pytest.CaptureFixture) -> None:
    """A full pass over an installation where only the keys and the sign-in are done.

    Without a terminal the wizard is obliged to go through to the end on its own:
    the required steps are closed, the optional ones skipped, and at the end it
    says what is missing and how to switch it on.
    """
    from tgagent import cli

    (data_dir / "session.session").write_text("x")          # the sign-in is already done
    monkeypatch.setattr(cli, "cmd_daemon_start", lambda args: 0)
    w = wizard(monkeypatch)
    assert w.run() == 0
    out = capsys.readouterr().out
    assert "Skipped (and how to switch it on" in out
    assert "uv run tg doctor" in out
    # No bot was started — the wizard is obliged to name what exactly stops
    # working because of that.
    assert any("notification bot" in line for line in w.skipped)


def test_wizard_run_is_idempotent(monkeypatch: pytest.MonkeyPatch, data_dir: Path,
                                  capsys: pytest.CaptureFixture) -> None:
    """A second pass over the same installation does not redo what is already done."""
    from tgagent import cli

    (data_dir / "session.session").write_text("x")
    starts: list[int] = []
    monkeypatch.setattr(cli, "cmd_daemon_start", lambda args: starts.append(1) or 0)
    assert wizard(monkeypatch).run() == 0

    # The daemon "came up" — the second time it is not touched, and the subagents
    # already match.
    monkeypatch.setattr(cli, "_daemon_pid", lambda: 4242)
    second = wizard(monkeypatch)
    keys = [s.key for s in install.pending(second.state)]
    # What is left is exactly what was skipped deliberately; what is done is not
    # asked about again.
    assert "login" not in keys and "daemon" not in keys and "agents" not in keys
    assert "bot" in keys
    assert second.run() == 0
    assert len(starts) == 1


def test_wizard_agents_step_installs(monkeypatch: pytest.MonkeyPatch, data_dir: Path,
                                     tmp_path: Path) -> None:
    w = wizard(monkeypatch)
    step = next(s for s in install.plan(state()) if s.key == "agents")
    assert w.step_agents(step) == 0
    for name in install.AGENT_FILES:
        assert (install.AGENT_DIR / name).exists()
    assert w.skipped == []


# --- autostart: launchd on macOS, systemd on Linux ----------------------------


def test_autostart_kind_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.sys, "platform", "darwin")
    assert install.autostart_kind() == "launchd"
    monkeypatch.setattr(install.sys, "platform", "linux")
    assert install.autostart_kind() == "systemd"
    # Windows: the daemon talks over a unix socket, there is no autostart for it
    # here and there must not be — None is more honest than offering a launchd
    # that does not work.
    monkeypatch.setattr(install.sys, "platform", "win32")
    assert install.autostart_kind() is None


def test_autostart_paths_point_at_the_right_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install.sys, "platform", "linux")
    template, target = install.autostart_paths()
    assert template == config.ROOT / install.UNIT_NAME
    assert target == install.SYSTEMD_USER_DIR / install.UNIT_NAME
    monkeypatch.setattr(install.sys, "platform", "darwin")
    template, target = install.autostart_paths()
    assert template == config.ROOT / install.PLIST_NAME
    assert target == install.LAUNCH_AGENTS / install.PLIST_NAME


@pytest.mark.parametrize("name", [install.PLIST_NAME, install.UNIT_NAME])
def test_autostart_templates_have_no_leftover_placeholders(name: str) -> None:
    """After the substitution not a single YOUR_USER must be left in the template.

    A forgotten placeholder does not break the installation out loud: the file
    will be written, and the daemon simply will not come up — pointing at a
    non-existent directory of somebody else's user.
    """
    body = install.render_autostart(
        (config.ROOT / name).read_text(), "/opt/uv/bin/uv", "/srv/telegram-mcp"
    )
    assert "YOUR_USER" not in body
    assert "/opt/uv/bin/uv" in body and "/srv/telegram-mcp" in body


def test_autostart_fix_speaks_of_systemd_on_linux() -> None:
    st = state(
        autostart_kind="systemd",
        autostart_template=config.ROOT / install.UNIT_NAME,
        autostart_target=install.SYSTEMD_USER_DIR / install.UNIT_NAME,
    )
    step = next(s for s in install.plan(st) if s.key == "autostart")
    assert "systemctl --user" in step.fix
    assert "launchctl" not in step.fix and "LaunchAgents" not in step.fix


def test_report_shows_autostart_on_linux_too() -> None:
    rows = {r["key"]: r for r in install.report(state(autostart_kind="systemd"))}
    assert "systemd" in rows["autostart"]["text"]


def test_report_skips_autostart_where_there_is_none() -> None:
    rows = {r["key"]: r for r in install.report(state(autostart_kind=None))}
    assert "autostart" not in rows


def test_wizard_installs_a_systemd_unit(monkeypatch: pytest.MonkeyPatch, data_dir: Path,
                                        tmp_path: Path) -> None:
    """The full autostart step on Linux: the file is written, the unit is switched on."""
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr(install, "SYSTEMD_USER_DIR", tmp_path / "systemd-user")
    ran: list[list[str]] = []
    monkeypatch.setattr(install, "_run", lambda cmd, timeout=60: (ran.append(cmd), (0, ""))[1])
    w = wizard(monkeypatch, answer=True)
    step = next(s for s in install.plan(w.state) if s.key == "autostart")
    assert w.step_autostart(step) == 0

    unit = tmp_path / "systemd-user" / install.UNIT_NAME
    body = unit.read_text()
    assert "YOUR_USER" not in body
    assert str(config.ROOT) in body and "/usr/bin/uv" in body
    assert ran == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", install.UNIT_NAME],
    ]
    assert w.skipped == []


def test_wizard_keeps_the_unit_when_systemctl_refuses(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """There is no systemctl (a container, WSL without systemd) — the file is
    useful all the same."""
    monkeypatch.setattr(install.sys, "platform", "linux")
    monkeypatch.setattr(install, "SYSTEMD_USER_DIR", tmp_path / "systemd-user")
    monkeypatch.setattr(install, "_run", lambda cmd, timeout=60: (127, "no systemctl command"))
    w = wizard(monkeypatch, answer=True)
    step = next(s for s in install.plan(w.state) if s.key == "autostart")
    assert w.step_autostart(step) == 0
    assert (tmp_path / "systemd-user" / install.UNIT_NAME).exists()
    out = capsys.readouterr().out
    assert "systemctl --user enable --now" in out


def test_wizard_says_docker_where_there_is_no_autostart(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(install.sys, "platform", "win32")
    w = wizard(monkeypatch, answer=True)
    step = next(s for s in install.plan(w.state) if s.key == "autostart")
    assert w.step_autostart(step) == 0
    assert "docker" in capsys.readouterr().out
    assert w.skipped
