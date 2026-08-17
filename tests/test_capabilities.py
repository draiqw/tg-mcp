"""The "what is available and why not" digest.

What is checked is not the formatting but the reason the tool exists at all: that the
four natures of restrictions do not get mixed up, that every refusal comes with an
action, that the limit numbers are taken from the server's answer instead of being made
up, and that rights in a chat are asked for only when a chat was asked about.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from typing import Any

import pytest
from telethon.tl import types

from tgagent import capabilities as caps
from tgagent import config

# The "ordinary/Premium" pairs exactly as TelegramService.limits hands them out.
LIMITS = {
    "dialog_filters_limit": {"default": 10, "premium": 20, "value": 10, "what": "folders"},
    "message_length_limit": {
        "default": 4096, "premium": 8192, "value": 4096, "what": "characters in a message",
    },
    "dialogs_pinned_limit": {
        "default": 5, "premium": 5, "value": 5, "what": "pinned chats",
    },
    "reactions_user_max": {
        "default": 1, "premium": 3, "value": 1, "what": "reactions per message",
    },
}
SINGLE = {
    "transcribe_audio_trial_weekly_number": 0,
    "translations_manual_enabled": "enabled",
}


def local(**over: Any) -> dict:
    """The local half with the needed skew: by default everything is configured."""
    base = {
        "write": {"ok": True, "what": "writing to the account", "detail": "allowed",
                  "fix": None},
        "confirm_writes": {"ok": True, "what": "write confirmation", "mode": "off",
                           "detail": "off", "needs_bot": False, "fix": None},
        "bot": {"ok": True, "what": "notification bot", "detail": "configured",
                "why": "the channel for talking to the owner does not work: "
                       "the notification bot is not configured",
                "fix": None},
        "openai": {"ok": True, "what": "OPENAI_API_KEY", "detail": "set", "fix": None},
        "groq": {"ok": True, "what": "GROQ_API_KEY", "detail": "set", "fix": None},
        "local_whisper": {"ok": True, "what": "local transcription model",
                          "detail": "mlx_whisper", "fix": None},
    }
    for key, patch in over.items():
        base[key] = {**base[key], **patch}
    return base


def by_tool(rows: list[dict]) -> dict[str, dict]:
    return {r["tool"]: r for r in rows}


def banned(**flags: bool) -> types.ChatBannedRights:
    return types.ChatBannedRights(until_date=None, **flags)


# ------------------------------------------------------------------- local


def test_local_half_is_read_from_the_environment(monkeypatch):
    monkeypatch.setattr(config, "bot_token", lambda: None)
    monkeypatch.setattr(config, "alert_chat_id", lambda: None)
    monkeypatch.setattr(config, "openai_key", lambda: None)
    monkeypatch.setattr(config, "groq_key", lambda: None)
    monkeypatch.setattr(caps, "local_whisper", lambda: None)
    state = caps.local_state()
    assert state["bot"]["ok"] is False
    assert "BotFather" in state["bot"]["fix"]
    assert state["openai"]["ok"] is False
    assert "OPENAI_API_KEY" in state["openai"]["fix"]
    # The digest is printed to the terminal and goes off to the bot, so the values of the
    # keys never show up in it under any circumstances — only "set / not set".
    assert state["openai"]["detail"] == "not set"


def test_a_configured_bot_needs_no_fixing(monkeypatch):
    monkeypatch.setattr(config, "bot_token", lambda: "t")
    monkeypatch.setattr(config, "alert_chat_id", lambda: "42")
    state = caps.local_state()
    assert state["bot"]["ok"] is True and state["bot"]["fix"] is None


def test_a_token_without_a_linked_chat_is_cured_differently(monkeypatch):
    monkeypatch.setattr(config, "bot_token", lambda: "t")
    monkeypatch.setattr(config, "alert_chat_id", lambda: None)
    assert "link-bot" in caps.local_state()["bot"]["fix"]


def test_local_model_is_checked_with_the_interpreter(monkeypatch):
    """Whether the extra is installed is a question for the environment, not for pyproject."""
    seen: list[str] = []

    def find_spec(name: str):
        seen.append(name)
        return object() if name == "faster_whisper" else None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert caps.local_whisper() == "faster_whisper"
    assert seen == ["mlx_whisper", "faster_whisper"]

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert caps.local_whisper() is None


# ------------------------------------------------------------------ natures


def test_writing_off_blocks_writing_tools_and_trims_mixed_ones():
    rows = by_tool(caps.restrictions(local(write={"ok": False, "fix": "turn on writing"}),
                                     premium=True, single=SINGLE))
    assert rows["tg_send"]["state"] == "blocked"
    assert rows["tg_send"]["nature"] == "local"
    assert rows["tg_send"]["fix"] == "turn on writing"
    # Reading with the same tools still works, and promising the opposite is not allowed.
    assert rows["tg_sessions"]["state"] == "limited"
    assert "terminate" in rows["tg_sessions"]["why"]
    assert "tg_history" not in rows


def test_unconfigured_bot_kills_only_the_conversation_with_the_owner():
    rows = by_tool(caps.restrictions(
        local(bot={"ok": False, "why": "there is no bot", "fix": "tg setup"}),
        premium=True, single=SINGLE,
    ))
    assert set(caps.BOT_TOOLS) <= set(rows)
    assert rows["tg_ask"]["state"] == "blocked"
    assert rows["tg_ask"]["fix"] == "tg setup"
    assert "tg_send" not in rows


def test_confirmation_mode_without_a_bot_locks_allowed_writing():
    rows = by_tool(caps.restrictions(
        local(bot={"ok": False, "why": "there is no bot", "fix": "tg setup"},
              confirm_writes={"mode": "all", "needs_bot": True}),
        premium=True, single=SINGLE,
    ))
    assert rows["tg_send"]["state"] == "blocked"
    assert "confirm_writes=all" in rows["tg_send"]["why"]
    assert rows["tg_send"]["fix"] == "tg setup"


def test_without_an_openai_key_the_dossier_is_trimmed_not_blocked():
    rows = by_tool(caps.restrictions(
        local(openai={"ok": False, "fix": "add OPENAI_API_KEY to .env"}),
        premium=True, single=SINGLE,
    ))
    assert rows["tg_memory"]["state"] == "limited"
    assert rows["tg_memory"]["nature"] == "local"
    assert "OPENAI_API_KEY" in rows["tg_memory"]["fix"]


@pytest.mark.parametrize(
    "premium, groq, whisper",
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_one_transcription_engine_is_enough(premium, groq, whisper):
    rows = caps.restrictions(
        local(groq={"ok": groq}, local_whisper={"ok": whisper}),
        premium=premium, single=SINGLE,
    )
    assert "tg_transcribe" not in by_tool(rows)


def test_without_a_single_engine_transcription_is_unavailable():
    rows = by_tool(caps.restrictions(
        local(groq={"ok": False}, local_whisper={"ok": False}),
        premium=False, single=SINGLE,
    ))
    row = rows["tg_transcribe"]
    assert row["state"] == "blocked"
    # Without Premium there is no built-in engine at all: the nature of the refusal is the
    # subscription, and editing .env does not cure it.
    assert row["nature"] == "subscription"
    assert "Premium" in row["fix"] and "GROQ_API_KEY" in row["fix"]


def test_with_premium_a_missing_transcription_engine_is_a_local_problem():
    rows = by_tool(caps.restrictions(
        local(groq={"ok": False}, local_whisper={"ok": False}),
        premium=True, single={"transcribe_audio_trial_weekly_number": 0},
    ))
    assert "tg_transcribe" not in rows      # the built-in one works, and on its own


def test_what_the_server_turned_off_is_named_by_its_own_nature():
    rows = by_tool(caps.restrictions(
        local(), premium=True, single={"translations_manual_enabled": "disabled"},
    ))
    assert rows["tg_translate"]["state"] == "blocked"
    assert rows["tg_translate"]["nature"] == "server"
    assert "locally" in rows["tg_translate"]["fix"]


def test_nothing_is_claimed_about_an_unknown_subscription():
    """A digest without the daemon does not know about Premium and must not guess."""
    rows = by_tool(caps.restrictions(
        local(groq={"ok": False}, local_whisper={"ok": False}), premium=None,
    ))
    assert "tg_transcribe" not in rows


# --------------------------------------------------- the bottom line from above


def test_the_summary_counts_by_nature_and_does_not_repeat_advice():
    rows = caps.restrictions(
        local(write={"ok": False, "fix": "turn on writing"},
              openai={"ok": False, "fix": "turn on writing"}),
        premium=True, single={"translations_manual_enabled": "disabled"},
    )
    summ = caps.summary(rows)
    assert summ["tools_total"] == caps.TOOLS_TOTAL
    assert summ["blocked"] == len(caps.WRITE_TOOLS) + 1          # writing + translation
    assert summ["available"] == caps.TOOLS_TOTAL - summ["blocked"]
    assert summ["blocked_by"] == {"local": len(caps.WRITE_TOOLS), "server": 1}
    assert summ["limited"] == len(caps.PARTIAL_WRITE_TOOLS) + 1
    # One and the same piece of advice for two dozen tools is still one piece of advice.
    assert summ["next_steps"].count("turn on writing") == 1


def test_a_configured_installation_has_nothing_blocked():
    summ = caps.summary(caps.restrictions(local(), premium=True, single=SINGLE))
    assert summ["blocked"] == 0 and summ["available"] == caps.TOOLS_TOTAL
    assert summ["next_steps"] == []


def test_a_partial_summary_admits_what_it_did_not_look_at():
    summ = caps.summary(caps.restrictions(local(), premium=None), partial=True)
    assert "daemon" in summ["text"]


# ------------------------------------------------------------- subscription


def test_subscription_shows_the_number_of_the_other_level():
    view = caps.subscription_view(premium=True, limits=LIMITS)
    folders = next(i for i in view["differs"] if i["key"] == "dialog_filters_limit")
    assert (folders["now"], folders["other"]) == (20, 10)
    # A limit the subscription does not move does not land in the list of its merits.
    assert [i["key"] for i in view["same_either_way"]] == ["dialogs_pinned_limit"]


def test_without_a_subscription_it_is_visible_what_it_would_give():
    view = caps.subscription_view(premium=False, limits=LIMITS)
    folders = next(i for i in view["differs"] if i["key"] == "dialog_filters_limit")
    assert (folders["now"], folders["other"]) == (10, 20)
    assert "Premium" in view["fix"]
    blocked = [f["what"] for f in view["features"] if not f["available"]]
    assert "several reactions on one message" in blocked


# -------------------------------------------------------------------- text


def test_the_text_names_both_the_reason_and_the_action():
    data = caps.build({"account": "main", "name": "Example", "username": "example"},
                      premium=False, limits=LIMITS, single=SINGLE,
                      local=local(write={"ok": False, "detail": "off",
                                         "fix": "set TG_ALLOW_WRITE=1"}))
    text = caps.render(data)
    assert "Example" in text
    assert "set TG_ALLOW_WRITE=1" in text
    assert "What to do:" in text
    # Twenty-nine identical lines in a row add nothing for the reader.
    assert text.count("reason: writing to the account is off") == 1
    assert "with Premium it would be 20" in text


def test_the_text_survives_without_the_telegram_half():
    rows = caps.restrictions(local(), premium=None)
    text = caps.render({"local": {"nature": caps.NATURES["local"], **local()},
                        "summary": caps.summary(rows, partial=True),
                        "restricted": rows})
    assert "Telegram Premium subscription" not in text
    assert "Local setup:" in text


# --------------------------------------------------------- rights in a chat


def test_right_names_are_real_telethon_fields():
    """A right is read through getattr, and a typo in the name stays silent.

    `getattr(rights, "manage_forum", False)` will return False, and instead of "the right
    is there" the tool will report itself unavailable — without a single error. Only a
    test can check this: selfcheck, by agreement, does not import Telethon.
    """
    import inspect

    from tgagent.core import TelegramService

    known = set()
    for cls in (types.ChatAdminRights, types.ChatBannedRights):
        known |= set(inspect.signature(cls.__init__).parameters) - {"self"}
    # "admin" is not an MTProto field but our own name for "has adminship been granted";
    # `_right_allowed` takes it apart in a separate branch, before any getattr.
    used = (set(caps.CHAT_TOOL_RIGHTS.values())
            | set(caps.RIGHT_NAMES)
            | set(TelegramService.ADMIN_ONLY_RIGHTS)
            | set(TelegramService.BROADCAST_ADMIN_ONLY)) - {"admin"}
    assert not used - known


def test_rights_add_up_in_an_understandable_order(service):
    admin = types.ChatAdminRights(
        change_info=False, post_messages=False, edit_messages=False,
        delete_messages=True, ban_users=False, invite_users=False,
        pin_messages=True, add_admins=False,
    )
    # The creator may do everything, even what is forbidden to the rest.
    assert service._right_allowed("send_messages", True, None, None,
                                  banned(send_messages=True))
    # Granted adminship overrides the general ban of the chat.
    assert service._right_allowed("pin_messages", False, admin, None,
                                  banned(pin_messages=True))
    # A personal restriction is stronger than both adminship and the default.
    assert not service._right_allowed("send_messages", False, admin,
                                      banned(send_messages=True), None)
    # A right a member cannot have at all is not handed out by silence.
    assert not service._right_allowed("ban_users", False, None, None, None)
    assert service._right_allowed("send_messages", False, None, None, None)


class FakeRightsClient:
    """A client in exactly the scope of `_chat_rights`: an entity and one full request."""

    def __init__(self, full: Any) -> None:
        self.full = full
        self.requests: list[str] = []

    async def get_entity(self, ent: Any) -> Any:
        return ent

    async def __call__(self, request: Any) -> Any:
        self.requests.append(type(request).__name__)
        return SimpleNamespace(full_chat=self.full)


@pytest.fixture
def chat_service(service, monkeypatch):
    """A service that hands back a chat known in advance and its full description."""

    def setup(entity: Any, full: Any):
        service.client = FakeRightsClient(full)
        service.me = types.User(id=1, first_name="Me")

        async def resolve(chat: Any) -> Any:
            return entity

        monkeypatch.setattr(service, "resolve", resolve)
        return service

    return setup


async def test_in_a_supergroup_it_is_visible_that_writing_is_not_allowed(chat_service):
    channel = types.Channel(
        id=1, title="Team", photo=None, date=None, megagroup=True,
        default_banned_rights=banned(send_messages=True, pin_messages=True),
    )
    full = SimpleNamespace(slowmode_seconds=30,
                           available_reactions=types.ChatReactionsNone())
    svc = chat_service(channel, full)
    out = await svc._chat_rights("Team", {"limits": LIMITS})

    assert out["nature"] == caps.NATURES["chat"]
    assert out["kind"] == "supergroup" and out["role"] == "member"
    assert out["can_write"] is False and "send messages" in out["why_not"]
    assert out["slowmode_sec"] == 30
    assert out["reactions"] == "disabled in this chat"
    tools = by_tool(out["tools"])
    assert tools["tg_send"]["available"] is False
    assert tools["tg_react"]["available"] is False      # reactions are off in the chat
    assert "admin" in tools["tg_moderate"]["fix"]
    # There are no forum tools at all in an ordinary supergroup.
    assert "tg_topic_create" not in tools
    # How many reactions will fit — a number from the server's answer, not out of thin air.
    assert out["reactions_per_message"] == 1


async def test_in_a_channel_writing_means_having_the_right_to_post(chat_service):
    admin = types.ChatAdminRights(
        change_info=False, post_messages=True, edit_messages=False,
        delete_messages=False, ban_users=False, invite_users=False,
        pin_messages=False, add_admins=False,
    )
    channel = types.Channel(
        id=2, title="Channel", photo=None, date=None, broadcast=True, admin_rights=admin,
    )
    full = SimpleNamespace(slowmode_seconds=None,
                           available_reactions=types.ChatReactionsAll())
    svc = chat_service(channel, full)
    out = await svc._chat_rights(2, {"limits": LIMITS})

    assert out["kind"] == "channel" and out["role"] == "admin"
    assert out["can_write"] is True
    tools = by_tool(out["tools"])
    assert tools["tg_send"]["right"] == "post_messages"
    assert tools["tg_chat_edit"]["available"] is False
    assert "post to the channel" in (out["admin_rights"] or [])


async def test_without_a_chat_nothing_is_asked_about_a_chat(service, monkeypatch):
    """The account level does not depend on a chat, so there must be no request about one."""
    service.me = types.User(id=1, first_name="Me", premium=True)
    service.client = FakeRightsClient(None)

    async def limits():
        return {"premium": True, "limits": LIMITS, "single": SINGLE}

    async def chat_rights(chat, lim):
        raise AssertionError("the chat was asked about")

    monkeypatch.setattr(service, "limits", limits)
    monkeypatch.setattr(service, "_chat_rights", chat_rights)

    data = await service.capabilities()
    assert "chat" not in data
    assert data["summary"]["tools_total"] == caps.TOOLS_TOTAL
    assert data["subscription"]["premium"] is True
    # The four natures lie in their own places instead of being dumped into one heap.
    assert {"subscription", "server_limits", "local", "restricted"} <= set(data)
    assert service.client.requests == []


async def test_advice_about_the_chat_lands_in_the_common_list_of_actions(service, monkeypatch):
    service.me = types.User(id=1, first_name="Me")

    async def limits():
        return {"premium": False, "limits": LIMITS, "single": SINGLE}

    async def chat_rights(chat, lim):
        return {"tools": [
            {"tool": "tg_moderate", "available": False, "fix": "admin rights are needed"},
            {"tool": "tg_chat_edit", "available": False, "fix": "admin rights are needed"},
            {"tool": "tg_send", "available": True, "fix": None},
        ]}

    monkeypatch.setattr(service, "limits", limits)
    monkeypatch.setattr(service, "_chat_rights", chat_rights)

    steps = (await service.capabilities(chat="Team"))["summary"]["next_steps"]
    assert steps.count("admin rights are needed") == 1


async def test_saved_messages_costs_not_a_single_request(chat_service):
    svc = chat_service("me", None)
    out = await svc._chat_rights("me", {"limits": LIMITS})
    assert out["kind"] == "saved" and out["can_write"] is True
    assert svc.client.requests == []
