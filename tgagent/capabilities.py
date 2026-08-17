"""What this installation can do, and why it cannot do the rest.

An agent's limits come in four different natures, and they must not be mixed,
because each is cured differently:

* **subscription** — Telegram Premium: it is bought, there is no local way around it;
* **server** — Telegram's own caps from `help.getAppConfig`: they are not curable at all,
  but they have to be known before promising anything to the owner;
* **local setup** — a key in `.env`, a linked bot, an installed extra:
  fixed in a minute by the owner's own hands;
* **rights in a chat** — adminship, chat bans, slowmode: they depend on the chat, not on
  the account, and they do not carry over to another chat.

Here live the "which tool needs what" tables, the checks of the local half (by fact, not
by the manifest) and the human text of the digest. There are no calls to Telegram here:
`TelegramService.capabilities` makes them, because only the server knows the subscription
and the limits. The split is needed for the CLI: `tg setup` and `tg login` show the local
half even without a running daemon.
"""

from __future__ import annotations

import importlib.util
import re
from typing import Any

from . import config

# How many tools the MCP server hands out in total. As a number, not as a count on the
# spot: counting would require importing mcp_server, and that drags the whole MCP into
# the daemon process and into the CLI for the sake of one number. A mismatch is caught by
# scripts/selfcheck.py.
TOOLS_TOTAL = 79

NATURES = {
    "subscription": "Telegram Premium subscription",
    "server": "cap on the Telegram side",
    "local": "setup of this installation",
    "chat": "rights in a particular chat",
}

# Tools that run entirely into TG_ALLOW_WRITE: in the core they have an unconditional
# `_assert_write()` at the start of the method. The list is cross-checked against the core
# in selfcheck — otherwise a new writing tool would silently count as available while
# writing is off.
WRITE_TOOLS = frozenset({
    "tg_send", "tg_send_file", "tg_send_location", "tg_schedule", "tg_draft",
    "tg_react", "tg_pin_message", "tg_poll", "tg_click", "tg_send_sticker",
    "tg_topic_create", "tg_topic_edit", "tg_bot_edit", "tg_block",
    "tg_contact_edit", "tg_create_group", "tg_invite", "tg_moderate",
    "tg_chat_edit", "tg_leave", "tg_folder_edit", "tg_edit", "tg_delete",
    "tg_forward", "tg_mark_read", "tg_notify", "tg_mute", "tg_archive", "tg_pin",
})

# For these only part of the work writes: without writing they can still read, but not
# change anything. The value is the argument that turns the call into a writing one.
PARTIAL_WRITE_TOOLS = {
    "tg_stories": "mark_read=true",
    "tg_scheduled": "cancel_ids",
    "tg_sessions": "terminate",
}

# Everything that talks to the owner goes through the BotFather bot: without a token and a
# linked chat_id there is nobody to talk to.
BOT_TOOLS = frozenset({"tg_alert", "tg_ask", "tg_remind"})

# Chat dossiers are written by an external model. Reading what is already written works
# without a key too, so the tool is not blocked entirely, only trimmed.
OPENAI_TOOLS = {"tg_memory": 'action="update"'}

# Tools the server can switch off with a flag in the app configuration.
SERVER_FLAG_TOOLS = {
    "tg_translate": (
        "translations_manual_enabled",
        "translation by Telegram is turned off on the server for this account",
    ),
}

# Rights in a chat: which tool needs which right. The right keys are fields of
# ChatAdminRights and ChatBannedRights, exactly as Telegram sends them.
CHAT_TOOL_RIGHTS = {
    "tg_send": "send_messages",
    "tg_send_file": "send_media",
    "tg_send_sticker": "send_stickers",
    "tg_poll": "send_polls",
    "tg_react": "send_reactions",
    "tg_pin_message": "pin_messages",
    "tg_invite": "invite_users",
    "tg_chat_edit": "change_info",
    "tg_topic_create": "manage_topics",
    "tg_topic_edit": "manage_topics",
    "tg_moderate": "ban_users",
    "tg_admin_log": "admin",
}

# How to name a right in human words: both in the listing of admin rights and in the
# explanation of a refusal.
RIGHT_NAMES = {
    "send_messages": "send messages",
    "send_media": "send attachments",
    "send_stickers": "send stickers",
    "send_gifs": "send GIFs",
    "send_polls": "create polls",
    "send_reactions": "add reactions",
    "embed_links": "links with previews",
    "pin_messages": "pin messages",
    "invite_users": "add members",
    "change_info": "change the chat description",
    "manage_topics": "manage forum topics",
    "ban_users": "ban and restrict",
    "delete_messages": "delete other people's messages",
    "edit_messages": "edit other people's messages",
    "post_messages": "post to the channel",
    "add_admins": "appoint admins",
    "manage_call": "manage the call",
    "post_stories": "post stories",
    "edit_stories": "edit stories",
    "delete_stories": "delete stories",
    "anonymous": "post anonymously",
    "admin": "be an admin",
}


# ------------------------------------------------------------------- local


def local_whisper() -> str | None:
    """Whether a local transcription model is installed — by fact, not by pyproject.

    `find_spec` asks the interpreter itself whether the module can be found in this
    environment: an entry in the dependencies proves nothing about it, the extra might not
    have been installed. A full import will not do: mlx-whisper drags a model along with
    it and several seconds of startup, and the answer is needed on every digest render.
    """
    for name in ("mlx_whisper", "faster_whisper"):
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, ValueError):
            continue
    return None


def _restart_hint(key: str) -> str:
    """Whether the daemon has to be restarted after editing .env.

    A key that is not in the environment yet will be picked up by the daemon on the next
    call: `load_dotenv` does not overwrite what is already set, but it does add what is
    missing. A value that has already been read, however, cannot be changed that way —
    only by a restart.
    """
    return " and restart the daemon (uv run tg daemon restart)" if config.env(key) else ""


def local_state() -> dict:
    """The local half of the access level — by facts, not by intentions."""
    token = bool(config.bot_token())
    chat_id = bool(config.alert_chat_id())
    if token and chat_id:
        bot_fix = None
    elif token:
        bot_fix = "press Start in your bot and run uv run tg link-bot"
    else:
        bot_fix = "create a bot with @BotFather and run uv run tg setup"

    confirm = config.load_confirm()
    mode = str(confirm.get("confirm_writes") or "off")
    whisper = local_whisper()

    return {
        "write": {
            "ok": config.allow_write(),
            "what": "writing to the account",
            "detail": "allowed" if config.allow_write() else "off (TG_ALLOW_WRITE=0)",
            "fix": None if config.allow_write()
            else "set TG_ALLOW_WRITE=1 in .env and restart the daemon (uv run tg daemon restart)",
        },
        "confirm_writes": {
            # This is not a breakage but a deliberate choice by the owner, so ok=True even
            # when the mode is on: the digest must not call for "fixing" a limit that was
            # put in place on purpose.
            "ok": True,
            "what": "write confirmation",
            "mode": mode,
            "detail": {
                "off": "off, writing calls go straight through",
                "outgoing": "asks about everything outsiders can see",
                "all": "asks about every writing call",
            }.get(mode, mode),
            "needs_bot": mode != "off",
            "fix": None,
        },
        "bot": {
            "ok": token and chat_id,
            "what": "notification bot",
            "detail": ("configured" if token and chat_id
                       else "token is there, chat_id is not linked" if token
                       else "not configured"),
            # A separate wording for explaining a refusal: in the table "not configured"
            # stands under its own heading, while in a blocking reason such a line would
            # be left without a subject.
            "why": ("the channel for talking to the owner does not work: "
                    + ("the bot token is there, but chat_id is not linked" if token
                       else "the notification bot is not configured")),
            "fix": bot_fix,
        },
        "openai": {
            "ok": bool(config.openai_key()),
            "what": "OPENAI_API_KEY",
            "detail": "set" if config.openai_key() else "not set",
            "fix": None if config.openai_key()
            else "add OPENAI_API_KEY to .env" + _restart_hint("OPENAI_API_KEY"),
        },
        "groq": {
            "ok": bool(config.groq_key()),
            "what": "GROQ_API_KEY",
            "detail": "set" if config.groq_key() else "not set",
            "fix": None if config.groq_key()
            else "add GROQ_API_KEY to .env (console.groq.com/keys)"
            + _restart_hint("GROQ_API_KEY"),
        },
        "local_whisper": {
            "ok": bool(whisper),
            "what": "local transcription model",
            "detail": whisper or "not installed",
            "fix": None if whisper else "uv sync --extra local-whisper",
        },
    }


# ------------------------------------------------------------ restrictions


def _row(tool: str, state: str, nature: str, why: str, fix: str | None) -> dict:
    return {"tool": tool, "state": state, "nature": nature, "why": why, "fix": fix}


def restrictions(
    local: dict, premium: bool | None = None, single: dict | None = None
) -> list[dict]:
    """Tools that do not work right now, or work only partly.

    `state`: `blocked` — calling it is pointless, `limited` — it works, but not all of it.
    `premium=None` means "nothing is known about the subscription here" (a digest without
    Telegram): then nothing is said about what runs into the subscription, instead of
    lying in either direction.
    """
    single = single or {}
    rows: list[dict] = []

    write = local["write"]
    if not write["ok"]:
        for tool in sorted(WRITE_TOOLS):
            rows.append(_row(tool, "blocked", "local",
                             "writing to the account is off", write["fix"]))
        for tool, arg in sorted(PARTIAL_WRITE_TOOLS.items()):
            rows.append(_row(tool, "limited", "local",
                             f"reading works, {arg} does not: writing is off", write["fix"]))

    bot = local["bot"]
    if not bot["ok"]:
        for tool in sorted(BOT_TOOLS):
            rows.append(_row(tool, "blocked", "local", bot["why"], bot["fix"]))
        # A confirmation mode without a bot locks writing entirely: the daemon is obliged
        # to ask, there is nobody to ask, and the call refuses. This has to be said
        # separately — otherwise allowed writing looks like it works.
        if write["ok"] and local["confirm_writes"]["needs_bot"]:
            for tool in sorted(WRITE_TOOLS):
                rows.append(_row(
                    tool, "blocked", "local",
                    f"confirm_writes={local['confirm_writes']['mode']}, "
                    "but there is nobody to ask for permission: the bot is not configured",
                    bot["fix"],
                ))

    if not local["openai"]["ok"]:
        for tool, what in sorted(OPENAI_TOOLS.items()):
            rows.append(_row(tool, "limited", "local",
                             f"{what} is not possible: the dossier is written by an "
                             "external model",
                             local["openai"]["fix"]))

    # Transcription: three engines, any one of them is enough. The built-in one is the
    # subscription, the other two are local setup, so the nature of the refusal depends on
    # what exactly is missing.
    if premium is not None:
        trial = single.get("transcribe_audio_trial_weekly_number")
        engines = []
        if premium or trial:
            engines.append("built-in Telegram transcription")
        if local["groq"]["ok"]:
            engines.append("Groq")
        if local["local_whisper"]["ok"]:
            engines.append(local["local_whisper"]["detail"])
        if not engines:
            # The nature of the refusal here is mixed, and the one to name is the one
            # closer to the matter: with Premium the built-in transcription would work by
            # itself, without it the matter is the unconfigured local engines.
            rows.append(_row(
                "tg_transcribe", "blocked",
                "local" if premium else "subscription",
                "not a single transcription engine: the built-in one needs Premium"
                + (f" (free transcriptions per week: {trial})"
                   if trial is not None else "")
                + ", Groq needs a key, the local one needs an installed model",
                "you need Telegram Premium, or GROQ_API_KEY in .env, "
                "or uv sync --extra local-whisper",
            ))

    for tool, (key, why) in sorted(SERVER_FLAG_TOOLS.items()):
        value = single.get(key)
        if value is not None and str(value).lower() not in ("enabled", "true", "1"):
            rows.append(_row(tool, "blocked", "server", why,
                             "not curable locally: this is what the Telegram server decided"))

    return rows


def summary(rows: list[dict], partial: bool = False) -> dict:
    """The bottom line from above: how much is available, how much is not and because of what.

    `partial=True` — a digest without Telegram (the daemon is not up): there is nothing to
    say about the subscription and the server caps, and staying silent about it is not an
    option, otherwise "everything is available" will read as a verified fact.
    """
    blocked = sorted({r["tool"] for r in rows if r["state"] == "blocked"})
    limited = sorted({r["tool"] for r in rows if r["state"] == "limited"} - set(blocked))
    by_nature: dict[str, int] = {}
    for tool in blocked:
        nature = next(r["nature"] for r in rows if r["tool"] == tool and r["state"] == "blocked")
        by_nature[nature] = by_nature.get(nature, 0) + 1

    text = f"Available: {TOOLS_TOTAL - len(blocked)} tools out of {TOOLS_TOTAL}"
    if blocked:
        text += ", blocked " + str(len(blocked)) + " (" + ", ".join(
            f"{NATURES[n]}: {c}" for n, c in sorted(by_nature.items())
        ) + ")"
    if limited:
        text += f", with reservations {len(limited)}"
    text += "."
    if partial:
        text += (" Only the local part has been read: the subscription and the Telegram "
                 "caps are known to the server, and a running daemon is needed to ask.")

    # Every piece of advice once, even if it unlocks a dozen tools.
    steps: list[str] = []
    for row in rows:
        if row["fix"] and row["fix"] not in steps:
            steps.append(row["fix"])

    return {
        "tools_total": TOOLS_TOTAL,
        "available": TOOLS_TOTAL - len(blocked),
        "blocked": len(blocked),
        "limited": len(limited),
        "blocked_by": by_nature,
        "blocked_tools": blocked,
        "limited_tools": limited,
        "text": text,
        "next_steps": steps,
    }


def subscription_view(premium: bool, limits: dict) -> dict:
    """What the subscription gives (or would give) to this account specifically.

    Not an advertisement for Telegram, but two numbers side by side: how much is possible
    now and how much it would be at the other level. Limits where the subscription changes
    nothing are moved out separately — otherwise the list would read as "Premium gives you
    all of this".
    """
    differs, same = [], []
    for key, row in limits.items():
        # Computed from the pair, not from a ready-made `value`: two numbers side by side
        # are the answer, and which of them is in effect is decided by the subscription,
        # and one place must decide that.
        now = row.get("premium" if premium else "default", row.get("value"))
        other = row.get("default" if premium else "premium", row.get("value"))
        item = {"key": key, "what": row.get("what"), "now": now, "other": other}
        (same if now == other else differs).append(item)

    features = [
        {
            "what": "built-in transcription of voice messages and video messages",
            "available": premium,
            "why": None if premium else "Telegram computes it itself only for Premium",
        },
        {
            "what": "several reactions on one message",
            "available": premium and (limits.get("reactions_user_max", {})
                                      .get("premium") or 1) > 1,
            "why": None if premium else "without Premium one reaction is allowed",
        },
        {
            "what": "the full list of similar channels",
            "available": premium,
            "why": None if premium else "without Premium Telegram truncates the result",
        },
    ]
    return {
        "nature": NATURES["subscription"],
        "premium": premium,
        "differs": differs,
        "same_either_way": same,
        "features": features,
        "fix": None if premium else "you need Telegram Premium; there is no local way around it",
    }


def build(whoami: dict, premium: bool, limits: dict, single: dict,
          local: dict | None = None) -> dict:
    """The whole digest, except rights in a chat: only Telegram knows them, and only per chat."""
    local = local if local is not None else local_state()
    rows = restrictions(local, premium=premium, single=single)
    return {
        "account": whoami.get("account"),
        "you": whoami,
        "summary": summary(rows),
        "subscription": subscription_view(premium, limits),
        "server_limits": {
            "nature": NATURES["server"],
            "source": "help.getAppConfig",
            "limits": limits,
            "single": single,
        },
        "local": {"nature": NATURES["local"], **local},
        "restricted": rows,
    }


# -------------------------------------------------------------- Telegram errors

# A limit learned in the middle of the work is the same limit: the agent needs not the
# name of the exception but what to do next. That is why typical server responses are
# translated here, in one place, and by the same logic as the digest above: the nature of
# the refusal is named (subscription, server cap, rights in a chat) and the way out.
#
# The key is the Telegram error code (CHAT_ADMIN_REQUIRED), not the Telethon class: the
# server adds codes more often than Telethon manages to create classes for them, and an
# unfamiliar error arrives as a string. How the code is extracted from the exception — see
# error_codes.
ERROR_HINTS: dict[str, str] = {
    # --- rights in a chat
    "CHAT_ADMIN_REQUIRED":
        "administrator rights in this chat are required: Telegram allows this action only "
        "to an admin. Ask the chat owner to grant the right — there is no way around it.",
    "CHAT_ADMIN_INVITE_REQUIRED":
        "only admins are allowed to add members to this chat. Send the person an invite "
        "link (tg_invites) instead of adding them.",
    "USER_ADMIN_INVALID":
        "this cannot be done to an admin or to the chat creator: their rights have to be "
        "removed first, and that is done by the chat owner.",
    "CHAT_WRITE_FORBIDDEN":
        "writing in this chat is not possible: the channel is read-only, or writing is "
        "closed for members. See tg_capabilities(chat=...) — it shows what is allowed "
        "right here.",
    "CHAT_SEND_PLAIN_FORBIDDEN":
        "the chat forbids text messages (usually a forum or a channel with comments is set "
        "up that way). Sending will only work with an attachment or into a forum topic.",
    "CHAT_SEND_MEDIA_FORBIDDEN":
        "the chat forbids attachments from members: a file cannot be sent here, text can.",
    "CHAT_SEND_STICKERS_FORBIDDEN": "the chat forbids stickers and GIFs from members.",
    "CHAT_SEND_GIFS_FORBIDDEN": "the chat forbids GIFs from members.",
    "CHAT_SEND_POLL_FORBIDDEN": "the chat forbids polls from members.",
    "CHAT_SEND_VOICES_FORBIDDEN": "the chat forbids voice messages from members.",
    "CHAT_FORWARDS_RESTRICTED":
        "copying is forbidden in this chat: Telegram does not allow forwarding or "
        "exporting its messages. What is left is to retell it in your own words.",
    "CHAT_RESTRICTED":
        "the chat is restricted by Telegram — members' actions in it are blocked not by us.",
    "USER_BANNED_IN_CHANNEL":
        "you have been restricted in this chat: writing and reacting are not possible "
        "until an admin lifts the restriction.",
    "CHANNEL_PRIVATE":
        "the chat is private and there is no access to it: either you are not a member, or "
        "you were removed. An invite link from a member is needed.",
    "MESSAGE_AUTHOR_REQUIRED": "only your own messages can be edited.",
    "MESSAGE_DELETE_FORBIDDEN":
        "other people's messages cannot be deleted here: in a group that needs admin "
        "rights, and in a channel — authorship.",
    "MESSAGE_EDIT_TIME_EXPIRED":
        "the edit window has expired: Telegram allows changing a message for 48 hours. "
        "What is left is to delete it and send it again.",
    "MESSAGE_NOT_MODIFIED":
        "the new text matches the previous one — Telegram considers such an edit empty.",
    "PIN_RESTRICTED": "members are not allowed to pin messages in this chat.",

    # --- subscription
    "PREMIUM_ACCOUNT_REQUIRED":
        "a Telegram Premium subscription is required: without it Telegram does not perform "
        "this action. There is no local way around it — either the subscription, or "
        "another path (for example, transcription through Groq or with a local model).",
    "PRIVACY_PREMIUM_REQUIRED":
        "this person accepts messages only from contacts and from accounts with Premium. "
        "Ask them to add you to their contacts — or write through a shared chat.",
    "VOICE_MESSAGES_FORBIDDEN":
        "this person has forbidden sending them voice messages (a Premium setting on their "
        "side). Send it as text or as an ordinary file.",

    # --- reactions
    "REACTION_INVALID":
        "such a reaction cannot be added in this chat: the chat does not allow all of them. "
        "The allowed ones are visible in tg_capabilities(chat=...).",
    "REACTIONS_TOO_MANY":
        "there are already as many of your own reactions on this message as Telegram "
        "allows (without Premium — one). Remove the extra one: tg_react without emoji.",
    "REACTION_EMPTY":
        "the reaction is empty: pass an emoji or nothing — then your own one is removed.",

    # --- account caps from help.getAppConfig
    "DIALOG_FILTERS_TOO_MUCH":
        "there are already as many folders as Telegram allows this account (the number is "
        "in tg_limits, key dialog_filters_limit; with Premium the ceiling is higher). "
        "Delete a folder you do not need and try again.",
    "FILTER_INCLUDE_EMPTY":
        "at least one chat must stay in the folder: Telegram does not accept an empty "
        "folder. Delete it entirely if it is no longer needed.",
    "FILTER_ID_INVALID": "there is no such folder: the list of folders comes from tg_folders.",
    "PINNED_DIALOGS_TOO_MUCH":
        "the number of pinned chats is already at the maximum (the number is in tg_limits, "
        "key dialogs_pinned_limit). Unpin one and try again.",
    "CHANNELS_TOO_MUCH":
        "the account is a member of the maximum number of groups and channels (tg_limits, "
        "key channels_limit). You will have to leave the ones you do not need.",
    "USER_CHANNELS_TOO_MUCH":
        "this person is already a member of the maximum number of groups and channels — "
        "they cannot be added, let them leave some of the chats.",
    "SCHEDULE_TOO_MUCH":
        "the number of scheduled messages in this chat is already at the maximum. Cancel "
        "some of them: tg_scheduled(cancel_ids=[...]).",
    "MESSAGE_TOO_LONG":
        "the message is longer than the Telegram limit (tg_limits, key "
        "message_length_limit). Split it into parts and send them one after another.",
    "MEDIA_CAPTION_TOO_LONG":
        "the caption for the file is longer than the Telegram limit (tg_limits, key "
        "caption_length_limit). Shorten the caption or send the text as a separate message "
        "after the file.",
    "FILE_PARTS_INVALID":
        "the file is too large for one upload: Telegram accepts as many 512 KB parts as "
        "written in tg_limits (key upload_max_fileparts) — that is about 2 GB. Split the "
        "file into parts.",
    "PHOTO_INVALID_DIMENSIONS":
        "Telegram rejects a picture with such dimensions: send it as a file "
        "(tg_send_file), then there will be no compression and no dimension checks.",
    "USERS_TOO_MUCH": "the chat is already at the maximum number of members — no room to add.",
    "ADMINS_TOO_MUCH": "the number of admins in this chat is already at the maximum.",

    # --- people and privacy
    "USER_PRIVACY_RESTRICTED":
        "this person's privacy settings do not allow adding them to a chat. Send them an "
        "invite link — they will join by themselves through it.",
    "USER_NOT_MUTUAL_CONTACT":
        "only a mutual contact can be added to a chat: they must have you in their "
        "contacts. What is left is an invite link.",
    "USER_IS_BLOCKED": "this person has blocked you: writing to them is not possible.",
    "YOU_BLOCKED_USER":
        "they are on your block list — unblock them first: "
        "tg_block(user=..., unblock=true).",
    "INPUT_USER_DEACTIVATED": "the account is deleted — there is nobody to write to.",
    "PEER_FLOOD":
        "Telegram has restricted the account for mass actions: this is anti-spam, not a "
        "breakage. Stop writing to strangers and wait — the restriction is lifted by "
        "itself, sometimes after a day.",

    # --- other
    "MSG_ID_INVALID": "there is no such message in this chat: check message_id.",
    "MESSAGE_ID_INVALID": "there is no such message in this chat: check message_id.",
    "STICKERSET_INVALID": "there is no such sticker set: the list comes from tg_stickers.",
    "TRANSCRIPTION_FAILED":
        "Telegram could not transcribe this recording. Try another engine: "
        "engine=\"groq\" or engine=\"local\".",
}

# Waiting errors: this is not a refusal but a "too often" or "too early", and the main
# thing in them is the number of seconds. They are kept separately, because they
# substitute it into the text.
WAIT_HINTS: dict[str, str] = {
    "FLOOD_WAIT":
        "Telegram asks to wait {seconds} s: too many requests in a row. "
        "This is not a refusal — repeat after a pause.",
    "FLOOD_PREMIUM_WAIT":
        "Telegram asks to wait {seconds} s: the rate for an account without Premium has "
        "been exceeded. This is not a refusal — repeat after a pause.",
    "SLOWMODE_WAIT":
        "slow mode is on in the chat: the next message will be accepted in "
        "{seconds} s. This is not a refusal — repeat after a pause.",
}


def error_codes(exc: Exception) -> list[str]:
    """Telegram codes an error is recognised by: the exact one first, then from the text.

    For errors that Telethon has a class for, the code is baked into its name
    (`ChatAdminRequiredError` → `CHAT_ADMIN_REQUIRED`). An unfamiliar error is not parsed
    by Telethon, but it leaves the code in the text (`DIALOG_FILTERS_TOO_MUCH`), so we look
    there as well: the list of codes on the server grows faster than the library does.
    """
    name = type(exc).__name__
    for tail in ("Error", "Exception"):
        name = name.removesuffix(tail)
    codes = [re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()]
    # The tail with the number of seconds is appended by the server to the code
    # (FLOOD_WAIT_42) — we cut it off, otherwise the wait would not be found in the table.
    codes += [re.sub(r"_\d+$", "", c) for c in re.findall(r"[A-Z][A-Z0-9_]{3,}", str(exc))]
    return codes


def explain_error(exc: Exception) -> str | None:
    """Human text for a typical Telegram error, or None if it is unfamiliar.

    None is a refusal to explain, not an empty explanation: an unfamiliar error is better
    shown as it is than replaced with an invented cause. Whoever asks decides what to do
    with None (the daemon hands back the error code, as before).
    """
    seconds = getattr(exc, "seconds", None)
    for code in error_codes(exc):
        if code in WAIT_HINTS and isinstance(seconds, int):
            return WAIT_HINTS[code].format(seconds=seconds)
        if code in ERROR_HINTS:
            return ERROR_HINTS[code]
    return None


# -------------------------------------------------------------------- text


def _fmt(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value)


def render(data: dict) -> str:
    """The same digest as human text: for the CLI, onboarding and the bot.

    One function for all three places on purpose: diverging wordings in the sign-in and in
    the bot would read as different states of the installation.
    """
    out: list[str] = []
    you = data.get("you") or {}
    if you:
        name = you.get("name") or "?"
        at = f" (@{you['username']})" if you.get("username") else ""
        out.append(f"Account: {name}{at}, label {you.get('account')}.")

    summ = data.get("summary")
    if summ:
        out.append(summ["text"])

    sub = data.get("subscription")
    if sub:
        out.append("")
        out.append("Telegram Premium subscription: " + ("yes." if sub["premium"] else "no."))
        head = ("What it gives you specifically:" if sub["premium"]
                else "What would change with it:")
        if sub["differs"]:
            out.append("  " + head)
            for item in sub["differs"]:
                other = ("without Premium it would be" if sub["premium"]
                         else "with Premium it would be")
                out.append(f"    {item['what']}: {_fmt(item['now'])} "
                           f"({other} {_fmt(item['other'])})")
        blocked_feats = [f for f in sub["features"] if not f["available"]]
        if blocked_feats:
            out.append("  Not available without the subscription:")
            for f in blocked_feats:
                out.append(f"    {f['what']} — {f['why']}")
        if sub["same_either_way"]:
            out.append("  Does not depend on the subscription: " + ", ".join(
                f"{i['what']} {_fmt(i['now'])}" for i in sub["same_either_way"]
            ) + ".")

    local = data.get("local")
    if local:
        out.append("")
        out.append("Local setup:")
        rows = [(v["what"], v) for k, v in local.items() if isinstance(v, dict)]
        width = max((len(w) for w, _ in rows), default=0)
        for what, item in rows:
            mark = "" if item.get("ok") else "  <-"
            out.append(f"  {what.ljust(width)}  {item.get('detail')}{mark}")

    restricted = data.get("restricted") or []
    blocked = [r for r in restricted if r["state"] == "blocked"]
    limited = [r for r in restricted if r["state"] == "limited"]
    if blocked:
        out.append("")
        out.append("Blocked:")
        for line in _group(blocked):
            out.append("  " + line)
    if limited:
        out.append("")
        out.append("Works only partly:")
        for line in _group(limited):
            out.append("  " + line)

    chat = data.get("chat")
    denied: list[dict] = []
    if chat:
        out.append("")
        out.append(f"In the chat “{chat.get('title')}” ({chat.get('kind')}):")
        out.append(f"  role: {chat.get('role')}")
        out.append("  writing: " + ("allowed" if chat.get("can_write") else
                                    f"not allowed — {chat.get('why_not')}"))
        if chat.get("slowmode_sec"):
            out.append(f"  slowmode: {chat['slowmode_sec']} s between messages")
        out.append(f"  reactions: {chat.get('reactions')}")
        denied = [t for t in chat.get("tools", []) if not t["available"]]
        if denied:
            out.append("  will not work here:")
            for t in denied:
                tail = f". {t['fix']}" if t.get("fix") else ""
                out.append(f"    {t['tool']} — {t.get('why') or 'not allowed'}{tail}")

    steps = (summ or {}).get("next_steps") or []
    if steps:
        out.append("")
        out.append("What to do:")
        for i, step in enumerate(steps, 1):
            out.append(f"  {i}. {step}")
    if summ and not steps and not blocked and not denied:
        out.append("")
        out.append("Nothing to fix: everything that is configured locally is configured.")
    return "\n".join(out)


def _group(rows: list[dict]) -> list[str]:
    """The same reason — one line with a list of tools.

    Twenty-nine lines of "writing is off" in a row add nothing for the reader, while they
    hide the reason and the cure.
    """
    order: list[tuple[str, str | None]] = []
    bucket: dict[tuple[str, str | None], list[str]] = {}
    for row in rows:
        key = (row["why"], row["fix"])
        if key not in bucket:
            bucket[key] = []
            order.append(key)
        bucket[key].append(row["tool"])
    lines = []
    for why, fix in order:
        tools = bucket[(why, fix)]
        names = ", ".join(tools) if len(tools) <= 6 else (
            ", ".join(tools[:6]) + f" and {len(tools) - 6} more"
        )
        lines.append(f"{names}\n    reason: {why}" + (f"\n    what to do: {fix}" if fix else ""))
    return lines
