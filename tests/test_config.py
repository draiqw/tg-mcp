"""Settings: schedule parsing, filter validation, paths and protection from self-disabling."""

from __future__ import annotations

import json

import pytest

from tgagent import config

# ---------------------------------------------------------------- schedule


@pytest.mark.parametrize(
    "value, expect",
    [
        (["09:00", "20:30"], [(9, 0), (20, 30)]),
        ("9:05", [(9, 5)]),                    # a single value, not a list
        (["20.15"], [(20, 15)]),               # a dot instead of a colon
        (["20:00", "09:00"], [(9, 0), (20, 0)]),   # ascending order
        (["09:00", "09:00"], [(9, 0)]),        # duplicates collapse
        (["", "  "], []),                      # empty strings are skipped
        (None, []),
        (["00:00", "23:59"], [(0, 0), (23, 59)]),   # the edges of the day
    ],
)
def test_digest_schedule_is_parsed(value, expect):
    assert config.parse_digest_times(value) == expect


@pytest.mark.parametrize(
    "value", ["25:00", "09:60", "-1:00", "in the morning", "9", "9:00:00", "aa:bb"]
)
def test_garbage_in_the_schedule_is_an_error(value):
    """A schedule that silently did not fire is worse than a missing one — hence ValueError."""
    with pytest.raises(ValueError):
        config.parse_digest_times([value])


# ---------------------------------------------------------------- auto filters


def test_rule_without_conditions_is_rejected():
    """A rule without conditions would fire on every incoming message — that is forbidden."""
    with pytest.raises(ValueError, match="at least one condition"):
        config.validate_auto([{"action": "read"}])


def test_unknown_action_is_rejected():
    with pytest.raises(ValueError, match="do not know"):
        config.validate_auto([{"chat": "Pete", "action": "reply"}])


def test_missing_action_is_rejected():
    with pytest.raises(ValueError, match="action"):
        config.validate_auto([{"chat": "Pete"}])


def test_folder_action_without_a_folder_is_rejected():
    with pytest.raises(ValueError, match="folder"):
        config.validate_auto([{"chat": "Pete", "action": "folder"}])
    ok = config.validate_auto([{"chat": "Pete", "action": "folder", "folder": "Work"}])
    assert ok[0]["action"] == ["folder"]


def test_unknown_chat_type_is_rejected():
    with pytest.raises(ValueError, match="type"):
        config.validate_auto([{"type": "supergroup", "action": "read"}])


def test_action_is_normalized_to_a_lowercase_list():
    out = config.validate_auto([{"keyword": "invoice", "action": ["Read", " ARCHIVE "]}])
    assert out[0]["action"] == ["read", "archive"]


def test_rule_that_is_not_an_object_is_rejected():
    with pytest.raises(ValueError, match="object"):
        config.validate_auto(["read"])
    with pytest.raises(ValueError, match="list of rules"):
        config.validate_auto({"action": "read"})


def test_missing_auto_section_is_an_empty_list():
    assert config.validate_auto(None) == []


def test_conditions_and_actions_are_not_lost():
    """Validation must not throw away rule fields: matching later goes by them."""
    out = config.validate_auto(
        [{"name": "invoices", "chat": ["Accounting"], "keyword": "invoice",
          "action": "save", "stop": True, "alert": True}]
    )
    assert out[0]["name"] == "invoices"
    assert out[0]["stop"] is True and out[0]["alert"] is True


# ---------------------------------------------------------------- accounts and paths


@pytest.mark.parametrize(
    "raw, expect",
    [
        (None, "main"),
        ("", "main"),
        ("main", "main"),
        ("default", "main"),
        # Cyrillic on purpose: "основной" is the Russian for "main" and stays an
        # accepted alias — an alias is what a person types, not what the code prints.
        ("основной", "main"),
        ("  Work ", "work"),
        ("work-2", "work-2"),
        # Cyrillic on purpose: everything extra is cut out and non-ASCII letters survive.
        ("ра/бо..чий", "рабочий"),
    ],
)
def test_account_label_is_normalized(raw, expect):
    assert config.normalize_account(raw) == expect


def test_label_made_only_of_separators_is_an_error():
    with pytest.raises(ValueError):
        config.normalize_account("///")


def test_every_account_has_its_own_files(data_dir):
    """Session, index and dossiers are split per account: wiping one without touching
    the other would otherwise be impossible."""
    assert config.session_path(None) == data_dir / "session"
    assert config.session_path("work") == data_dir / "session-work"
    assert config.index_path("main") == data_dir / "index.db"
    assert config.index_path("work") == data_dir / "index-work.db"
    assert config.memory_dir() == data_dir / "memory"
    assert config.memory_dir("work") == data_dir / "memory-work"
    # different accounts must not share a single file
    assert len({
        config.session_path("work"), config.index_path("work"), config.memory_dir("work"),
        config.session_path("home"), config.index_path("home"), config.memory_dir("home"),
    }) == 6


def test_account_list_is_counted_from_session_files(data_dir):
    assert config.list_accounts() == []
    (data_dir / "session.session").write_text("")
    (data_dir / "session-work.session").write_text("")
    assert config.list_accounts() == ["main", "work"]


# ---------------------------------------------------------------- rules on disk


def test_default_rules_when_the_file_is_missing():
    rules = config.load_rules()
    assert rules["enabled"] is True
    assert rules["confirm_writes"] == "off"
    assert rules is not config.DEFAULT_RULES        # a copy, not the shared dict


def test_broken_rules_file_does_not_kill_the_daemon(data_dir):
    config.RULES_FILE.write_text("{this is not json")
    assert config.load_rules() == config.DEFAULT_RULES


def test_saving_rules_fills_in_the_defaults(data_dir):
    saved = config.save_rules({"keywords": ["invoice"]})
    assert saved["keywords"] == ["invoice"]
    assert saved["alert_on_private"] is True
    on_disk = json.loads(config.RULES_FILE.read_text())
    assert on_disk["keywords"] == ["invoice"]
    assert oct(config.RULES_FILE.stat().st_mode & 0o777) == "0o600"


def test_saving_rules_does_not_reset_the_confirmation_mode(data_dir):
    """An owner-side restriction: writing alert rules does not clear confirm_writes.

    Otherwise it would be enough for the agent to save the rules over a stale copy in
    the daemon's memory to take the write confirmation off itself.
    """
    config.RULES_FILE.write_text(json.dumps({
        "confirm_writes": "all",
        "confirm_whitelist": ["me", "Pete"],
        "confirm_timeout_sec": 30,
    }))

    saved = config.save_rules({"keywords": ["invoice"], "confirm_writes": "off",
                               "confirm_whitelist": [], "confirm_timeout_sec": 1})

    assert saved["confirm_writes"] == "all"
    assert saved["confirm_whitelist"] == ["me", "Pete"]
    assert saved["confirm_timeout_sec"] == 30
    assert json.loads(config.RULES_FILE.read_text())["confirm_writes"] == "all"
    assert config.load_confirm()["confirm_writes"] == "all"


def test_confirmation_mode_is_read_from_disk_not_from_the_defaults(data_dir):
    config.RULES_FILE.write_text(json.dumps({"confirm_writes": "outgoing"}))
    assert config.load_confirm() == {
        "confirm_writes": "outgoing",
        "confirm_whitelist": ["me"],
        "confirm_timeout_sec": 90,
    }


def test_a_bad_schedule_is_not_written(data_dir):
    config.save_rules({"digest_at": ["09:00"]})
    with pytest.raises(ValueError):
        config.save_rules({"digest_at": ["at night"]})
    # the file stayed as it was: validation runs before the write
    assert json.loads(config.RULES_FILE.read_text())["digest_at"] == ["09:00"]


def test_a_bad_filter_is_not_written(data_dir):
    config.save_rules({"auto": []})
    with pytest.raises(ValueError):
        config.save_rules({"auto": [{"action": "read"}]})
    assert json.loads(config.RULES_FILE.read_text())["auto"] == []


def test_as_list():
    assert config.as_list(None) == []
    assert config.as_list("Pete") == ["Pete"]
    assert config.as_list(["a", "b"]) == ["a", "b"]
    assert config.as_list(0) == [0]


def test_confirm_keys_have_defaults():
    """A CONFIRM_KEYS entry without a default would crash load_confirm with a KeyError."""
    for key in config.CONFIRM_KEYS:
        assert key in config.DEFAULT_RULES


# ---------------------------------------------------------------- platform


def test_installation_signature_comes_from_the_system_and_is_not_hardcoded():
    """The line the installation is seen under in Settings → Devices.

    The owner decides from that list which session to revoke. "macOS" on somebody
    else's Linux is not cosmetics — it is a wrong signature in the list of the
    account's accesses.
    """
    import platform

    info = config.client_info()
    assert info["system_version"].startswith(platform.system())
    assert info["device_model"] == "claude-tg-agent"
    assert info["app_version"].startswith("tgagent ")


def test_no_module_builds_the_signature_itself():
    """The signature is built only in `client_info`, there must be no copies of it.

    The check is static, because otherwise the divergence shows up only on somebody
    else's machine: the Telethon client is created in five places (the core and four
    sign-in commands), and any of them could silently start introducing itself with a
    hardcoded string.
    """
    for path in sorted((config.ROOT / "tgagent").glob("*.py")):
        text = path.read_text()
        for literal in ("system_version=", "device_model=", "app_version="):
            assert literal not in text, (
                f"{path.name}: the client signature is built outside config.client_info()"
            )


# ---------------------------------------------------------------- clone or package


def test_this_checkout_is_not_an_installed_package():
    """The flag has to be right here first, because everything below depends on it.

    A clone is recognised by the project file next to the package. If that ever
    stops being true, state moves out of the checkout into `~/.tgagent` on the
    developer's own machine, and the first sign of it is a session that vanished.
    """
    assert not config.INSTALLED
    assert config.HOME == config.ROOT
    assert (config.ROOT / "pyproject.toml").exists()


def test_state_of_an_installed_package_lives_outside_it(monkeypatch):
    """Installed, `.env` and `data/` must not land next to the code.

    Next to the code means inside site-packages, and site-packages is rewritten by
    the next upgrade — together with `session.session`, which is the account.
    """
    monkeypatch.setattr(config, "INSTALLED", True)
    home = config.Path.home() / ".tgagent"
    # Recomputed the way the module computes it, since the module ran at import.
    assert home != config.ROOT
    assert not str(home).endswith("site-packages")


@pytest.mark.parametrize("installed, cd, expect", [
    (False, False, "uv run tg"),
    (False, True, None),          # carries the checkout path, checked separately
    (True, False, "tg"),
    (True, True, "tg"),
])
def test_command_prefix_follows_the_installation(monkeypatch, installed, cd, expect):
    """A hint that names the wrong command sends the reader to "command not found".

    Installed, `tg` is a console script on PATH and there is no project to `cd`
    into, so both forms collapse to one.
    """
    monkeypatch.setattr(config, "INSTALLED", installed)
    got = config.command_prefix(cd=cd)
    if expect is None:
        assert got == f"cd {config.ROOT} && uv run tg"
    else:
        assert got == expect


def test_login_command_uses_the_same_prefix(monkeypatch):
    monkeypatch.setattr(config, "INSTALLED", True)
    assert config.login_command() == "tg login"
    assert config.login_command("work") == "tg login --account work"


def test_whisper_command_cannot_name_the_extra_when_installed(monkeypatch):
    """Installed, `tgagent[local-whisper]` would send uv to PyPI, where it is not.

    So the packages behind the extra are asked for by name. The check is that the
    extra is not named, not the exact spelling of the command.
    """
    monkeypatch.setattr(config, "INSTALLED", False)
    assert config.whisper_command() == "uv sync --extra local-whisper"

    monkeypatch.setattr(config, "INSTALLED", True)
    installed = config.whisper_command()
    assert "local-whisper" not in installed
    assert "faster-whisper" in installed and config.REPO in installed
