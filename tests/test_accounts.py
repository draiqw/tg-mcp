"""Several accounts: the default choice, file isolation, binding rules to a label.

Not a single test here goes to the network or opens a session file: for them an
account is a label, a set of paths and a string in a rule.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from conftest import FakeService, make_event

from tgagent import config, index, memory
from tgagent.daemon import Daemon

# ------------------------------------------------------- the default account


def test_without_settings_the_default_is_main(data_dir):
    assert config.default_account() == "main"
    assert not config.SETTINGS_FILE.exists()   # an empty installation writes nothing


def test_account_choice_survives_a_restart(data_dir):
    """This is why the choice moved to disk: a closed Claude must not put the
    agent back into the main account silently."""
    config.set_default_account("work")
    assert config.default_account() == "work"
    # A "restart" is reading the same file again, without a single object in memory.
    assert json.loads(config.SETTINGS_FILE.read_text())["default_account"] == "work"
    assert config.default_account() == "work"


def test_default_falls_back_to_main(data_dir):
    config.set_default_account("work")
    assert config.set_default_account(None) == "main"
    assert config.default_account() == "main"


def test_broken_settings_do_not_bring_the_daemon_down(data_dir):
    """One corrupted line must not keep it from coming up: the default is known
    without a file, and there is nothing here for the daemon to fall over."""
    config.SETTINGS_FILE.write_text("{this is not json")
    assert config.load_settings() == {}
    assert config.default_account() == "main"


def test_garbage_in_the_label_does_not_make_the_default_unreadable(data_dir):
    config.save_settings({"default_account": "///"})
    assert config.default_account() == "main"


def test_settings_do_not_live_in_the_alert_rules(data_dir):
    """The account choice is a property of the installation, not a condition of
    "when to wake me"."""
    config.set_default_account("work")
    config.save_rules({"keywords": ["invoice"]})
    assert "default_account" not in config.load_rules()
    assert config.default_account() == "work"


# --------------------------------------------------- daemon: where a call goes


def signed_in(daemon: Daemon, *labels: str) -> None:
    """Sign the fakes in: both in the daemon and with session files on disk (the
    known labels for rule prefixes are counted from them)."""
    for label in labels:
        daemon.services[label] = FakeService(account=label)
        name = "session.session" if label == "main" else f"session-{label}.session"
        (config.DATA / name).write_text("")
    daemon.tg = daemon.services[labels[0]]


def test_a_call_without_a_label_goes_to_the_chosen_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    assert daemon.service(None).account == "main"
    config.set_default_account("work")
    assert daemon.service(None).account == "work"


def test_a_one_off_label_beats_the_default(daemon, data_dir):
    signed_in(daemon, "main", "work")
    config.set_default_account("work")
    assert daemon.service("main").account == "main"


def test_a_vanished_default_is_a_refusal_not_a_substitution(daemon, data_dir):
    """A silent fallback to the main account would mean a message that went to
    the wrong place."""
    signed_in(daemon, "main")
    config.set_default_account("work")
    with pytest.raises(ValueError) as exc:
        daemon.service(None)
    assert "uv run tg login --account work" in str(exc.value)


def test_the_refusal_names_the_exact_sign_in_command(daemon, data_dir):
    """The agent cannot sign a second account in, and must not — but it is
    obliged to tell the owner clearly what exactly to type."""
    signed_in(daemon, "main")
    with pytest.raises(ValueError) as exc:
        daemon.service("work")
    text = str(exc.value)
    assert "uv run tg login --account work" in text
    assert "main" in text                    # what there is at all
    assert "owner" in text                   # and who does it


def test_the_refusal_with_no_account_at_all_also_carries_the_command(daemon, data_dir):
    with pytest.raises(RuntimeError) as exc:
        daemon.service(None)
    assert "uv run tg login" in str(exc.value)


async def test_switching_with_persist_lands_on_disk(daemon, data_dir):
    signed_in(daemon, "main", "work")
    res = await daemon.account_use(use="work", persist=True)
    assert res["persisted"] is True
    assert config.default_account() == "work"
    assert daemon.service(None).account == "work"


async def test_switching_without_persist_changes_nothing_on_disk(daemon, data_dir):
    signed_in(daemon, "main", "work")
    res = await daemon.account_use(use="work")
    assert res["persisted"] is False
    assert config.default_account() == "main"
    assert not config.SETTINGS_FILE.exists()


async def test_switching_to_an_account_that_is_not_signed_in_is_refused(daemon, data_dir):
    signed_in(daemon, "main")
    with pytest.raises(ValueError) as exc:
        await daemon.account_use(use="work", persist=True)
    assert "uv run tg login --account work" in str(exc.value)
    assert config.default_account() == "main"   # a failed attempt wrote nothing


# ------------------------------------------------------- the list of accounts


async def test_the_account_list_shows_who_is_active_and_where_the_files_lie(daemon, data_dir):
    signed_in(daemon, "main", "work")
    config.set_default_account("work")
    out = await daemon.accounts(daemon.services["main"])

    assert out["using"] == "main"            # this client was switched one-off
    assert out["default"] == "work"          # and something else lies on disk
    assert out["using_source"] == "client choice"

    rows = {r["account"]: r for r in out["accounts"]}
    assert rows["main"]["active"] is True and rows["work"]["active"] is False
    assert rows["work"]["default"] is True and rows["main"]["default"] is False
    assert rows["work"]["session"].endswith("session-work.session")
    assert rows["work"]["index"].endswith("index-work.db")
    assert rows["work"]["memory"].endswith("memory-work")
    assert "uv run tg login" in out["add"]


async def test_the_list_shows_the_access_level_of_each_account(daemon, data_dir):
    """One has Premium, the other does not — otherwise the list does not answer
    the question "and what can I do from this account"."""
    signed_in(daemon, "main", "work")
    daemon.services["work"].premium = True
    out = await daemon.accounts()
    rows = {r["account"]: r for r in out["accounts"]}
    assert rows["main"]["access"]["premium"] is False
    assert rows["work"]["access"]["premium"] is True
    assert rows["work"]["access"]["available"] > 0
    assert "tools" in rows["work"]["access"]["text"]


async def test_the_access_level_can_be_skipped(daemon, data_dir):
    signed_in(daemon, "main")
    out = await daemon.accounts(access=False)
    assert "access" not in out["accounts"][0]
    assert daemon.services["main"].calls == []   # there was no request to Telegram


async def test_a_failed_request_does_not_hide_the_other_accounts(daemon, data_dir):
    signed_in(daemon, "main", "work")

    async def boom(full: bool = False) -> dict:
        raise RuntimeError("the network dropped")

    daemon.services["work"].limits = boom
    out = await daemon.accounts()
    rows = {r["account"]: r for r in out["accounts"]}
    assert rows["main"]["access"]["known"] is True
    assert rows["work"]["access"]["known"] is False
    assert "the network dropped" in rows["work"]["access"]["why"]


# --------------------------------------------------- access level per account


async def test_the_summary_over_all_accounts_separates_own_from_shared(daemon, data_dir):
    signed_in(daemon, "main", "work")
    daemon.services["work"].premium = True
    out = await daemon.capabilities(daemon.services["main"], all_accounts=True)

    assert set(out["accounts"]) == {"main", "work"}
    assert out["accounts"]["main"]["premium"] is False
    assert out["accounts"]["work"]["premium"] is True
    assert out["using"] == "main"
    # The local setup is one per installation and stands once, instead of being
    # rewritten under every account: the bot and the keys do not belong to a
    # Telegram session.
    assert out["shared"]["bot"]["ok"] is False
    for row in out["accounts"].values():
        assert all(r["nature"] != "local" for r in row["restricted_account"])


async def test_a_single_account_answers_as_before(daemon, data_dir):
    signed_in(daemon, "main", "work")
    out = await daemon.capabilities(daemon.services["work"], chat="Pete")
    assert out == {"account": "work", "chat": "Pete"}


# ------------------------------------------------- rules: binding to an account


def test_a_pattern_without_a_label_applies_in_every_account(daemon, data_dir):
    """An old rules.json must work as it worked: an update must not switch off
    the rules the owner set up before the second account existed."""
    signed_in(daemon, "main", "work")
    for account in ("main", "work"):
        assert daemon._chat_matches(["mom"], 1, "Mom", account) is True


def test_a_pattern_with_a_label_applies_only_in_its_own_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    assert daemon._chat_matches(["work:mom"], 1, "Mom", "work") is True
    assert daemon._chat_matches(["work:mom"], 1, "Mom", "main") is False


def test_a_colon_in_a_chat_title_is_not_taken_for_a_label(daemon, data_dir):
    """"Lunch: who is coming" is a title, not an account; otherwise the rule
    would silently stop firing, and there would be nothing to notice it by."""
    signed_in(daemon, "main", "work")
    assert daemon._chat_matches(["lunch: who is coming"], 1, "Lunch: who is coming", "main") is True


def test_an_alert_on_a_watched_chat_knows_the_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    daemon.rules["alert_on_private"] = False
    daemon.rules["watch_chats"] = ["work:Mom"]
    assert daemon.alert_reason(make_event(chat="Mom", account="work")) == "watch"
    assert daemon.alert_reason(make_event(chat="Mom", account="main")) is None


def test_a_muted_chat_is_muted_only_in_its_own_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    daemon.rules["mute_chats"] = ["main:Mom"]
    assert daemon.alert_reason(make_event(chat="Mom", account="main")) is None
    assert daemon.alert_reason(make_event(chat="Mom", account="work")) == "private"


def test_an_inbox_filter_can_be_bound_to_an_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    rule = {"chat": "Mom", "action": ["read"], "account": "work"}
    assert daemon.auto_rule_matches(rule, make_event(chat="Mom", account="work")) is True
    assert daemon.auto_rule_matches(rule, make_event(chat="Mom", account="main")) is False


def test_an_inbox_filter_without_a_label_works_in_every_account(daemon, data_dir):
    signed_in(daemon, "main", "work")
    rule = {"chat": "Mom", "action": ["read"]}
    for account in ("main", "work"):
        assert daemon.auto_rule_matches(rule, make_event(chat="Mom", account=account))


def test_the_confirmation_whitelist_is_per_account_too(daemon, data_dir):
    """"me" in the personal and in the work account are two different Saved Messages."""
    signed_in(daemon, "main", "work")
    where = {"raw": "me", "id": None, "name": "Saved Messages", "saved": True}
    assert daemon._confirm_whitelisted(where, ["main:me"], "main") is True
    assert daemon._confirm_whitelisted(where, ["main:me"], "work") is False
    assert daemon._confirm_whitelisted(where, ["me"], "work") is True


def test_a_label_of_a_nonexistent_account_is_not_taken_for_a_label(daemon, data_dir):
    """Otherwise removing an account would quietly turn a rule into a dead one."""
    signed_in(daemon, "main")
    assert daemon._scope_of("work:mom") == (None, "work:mom")


# ------------------------------------------------------ the reply of a write call


class FakeRequest:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


async def rpc(daemon: Daemon, payload: dict) -> dict:
    return json.loads((await daemon.handle_call(FakeRequest(payload))).text)


async def test_the_send_reply_names_the_account(daemon, data_dir, monkeypatch):
    """The agent must know where it wrote, not only where it meant to: the
    default could have been changed in another session, and a message cannot be
    taken back."""
    signed_in(daemon, "main", "work")

    async def send(**params: Any) -> dict:
        return {"sent": True, "message_id": 7}

    monkeypatch.setattr(
        daemon, "dispatch_table", lambda svc: {"send": send, "dialogs": svc.dialogs}
    )
    out = await rpc(daemon, {"method": "send", "params": {"chat": "Pete", "text": "x"},
                             "account": "work"})
    assert out["result"]["account"] == "work"


async def test_a_label_in_the_params_beats_the_client_choice(daemon, data_dir, monkeypatch):
    """A one-off "and what is in the work account" must not require switching
    there and back — and must not reach a core method as an extra argument."""
    signed_in(daemon, "main", "work")
    monkeypatch.setattr(daemon, "dispatch_table", lambda svc: {"dialogs": svc.dialogs})
    out = await rpc(daemon, {"method": "dialogs", "params": {"limit": 5, "account": "work"},
                             "account": "main"})
    assert out["ok"] is True
    assert daemon.services["work"].calls == [("dialogs", {"limit": 5})]
    assert daemon.services["main"].calls == []


async def test_changing_the_default_lands_in_the_actions_log(daemon, data_dir, monkeypatch):
    signed_in(daemon, "main", "work")
    monkeypatch.setattr(daemon, "dispatch_table", lambda svc: {"account_use": daemon.account_use})
    out = await rpc(daemon, {"method": "account_use",
                             "params": {"use": "work", "persist": True}})
    assert out["ok"] is True
    assert "account_use" in config.ACTIONS_LOG.read_text()


# ----------------------------------------------------------------- isolation


def test_dossiers_of_different_accounts_do_not_mix(data_dir):
    """The same person in two accounts is two different conversations."""
    chat_id = -100500
    main = memory.MemoryStore(config.memory_dir("main"))
    work = memory.MemoryStore(config.memory_dir("work"))
    main.write(chat_id, {"chat": "Pete"}, "personal")
    work.write(chat_id, {"chat": "Pete"}, "work-related")

    assert main.read(chat_id)[1] == "personal"
    assert work.read(chat_id)[1] == "work-related"
    assert main.path(chat_id) != work.path(chat_id)
    # Removing one does not touch the other — this is what they were split into
    # separate folders for.
    main.drop(chat_id)
    assert main.exists(chat_id) is False
    assert work.exists(chat_id) is True


def _indexed(text: str) -> dict:
    dt = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
    return {
        "msg_id": 1, "ts": int(dt.timestamp()), "date": dt.isoformat(),
        "from_id": 555, "from_name": "Pete", "out": False, "media": None, "text": text,
    }


def test_indexes_of_different_accounts_do_not_mix(data_dir):
    main = index.MessageIndex(config.index_path("main"))
    work = index.MessageIndex(config.index_path("work"))
    main.add(555, "Pete", "user", [_indexed("agreed on wednesday")])

    assert main.search("agreed")["total"] == 1
    assert work.exists() is False           # the other index is not even created
    work.add(555, "Pete", "user", [_indexed("invoice for payment")])
    assert main.search("invoice")["total"] == 0
    assert work.search("agreed")["total"] == 0


def test_the_service_looks_at_the_files_of_its_own_account(service, data_dir):
    service.account = "work"
    assert service._index_store().path == config.index_path("work")
    assert service._memory_store().root == config.memory_dir("work")
