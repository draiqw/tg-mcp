"""Language of everything a human reads.

The repository itself is English: comments, docstrings, logs, error messages the
model sees, documentation. That is not a preference, it is what makes the code
reviewable by anyone who finds it.

What the *owner* reads is a different matter. Setup prompts, the wizard, `tg
doctor`, alerts and questions delivered through the bot — those are read by one
person on their own machine, and forcing them into English buys nothing. They go
through this catalog instead, keyed by a short identifier, with every language
side by side so a missing or drifted translation is visible in the diff rather
than at runtime.

Adding a language means adding one more entry to `SUPPORTED` and one more line to
every message. That is deliberate: a language that covers half the messages is
worse than no language at all, and `scripts/selfcheck.py` fails on any gap.
"""

from __future__ import annotations

import re

# Order matters only for the fallback: the first entry is the default language.
SUPPORTED: tuple[str, ...] = ("en", "ru")
DEFAULT = "en"

_PLACEHOLDER = re.compile(r"\{(\w+)")


def language() -> str:
    """Which language the owner reads, from TG_LANG.

    Read on every call rather than cached: `.env` is edited by hand, and a
    setting that only takes effect after a daemon restart is a setting people
    report as broken.

    Imported late — `config` is loaded before everything else and itself needs
    `t()`; a module-level import here would close the circle.
    """
    from . import config

    raw = (config.env("TG_LANG", DEFAULT) or DEFAULT).strip().lower()
    # `ru_RU.UTF-8` and `en-GB` are what people paste out of their system locale.
    short = raw.replace("-", "_").split("_")[0].split(".")[0]
    return short if short in SUPPORTED else DEFAULT


def t(key: str, /, **kwargs: object) -> str:
    """One message in the owner's language.

    Never raises. A missing key or a bad substitution returns something readable
    instead of killing the alert that carried it: the daemon must survive its own
    bugs, and `selfcheck` catches these long before a human does.
    """
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    template = entry.get(language()) or entry.get(DEFAULT) or key
    if not kwargs:
        return template
    try:
        return template.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return template


def placeholders(template: str) -> set[str]:
    """Named substitutions in a template — for the parity check in selfcheck."""
    return set(_PLACEHOLDER.findall(template))


# Messages the owner reads. Keys are `where.what`, grouped by the module that
# prints them, in the order a person meets them: setup, login, status, daemon,
# bot.
MESSAGES: dict[str, dict[str, str]] = {
    # --- account -----------------------------------------------------------
    "account.add_more": {
        "en": "\nAdd another: {command}",
        "ru": "\nДобавить ещё: {command}",
    },
    "account.bad_label": {
        "en": "Bad account label: {account}",
        "ru": "Плохая метка аккаунта: {account}",
    },
    "account.change_default": {
        "en": "Change the default: uv run tg accounts --default <label>",
        "ru": "Сменить умолчание: uv run tg accounts --default <метка>",
    },
    "account.daemon_holds_all": {
        "en": "The daemon already holds every account, no need to restart it.",
        "ru": "Демон уже держит все аккаунты, перезапускать его не нужно.",
    },
    "account.default_mark": {
        "en": " ← default",
        "ru": " ← по умолчанию",
    },
    "account.default_reset_to_main": {
        "en": "It was the default account — the default is back to main.",
        "ru": "Он был аккаунтом по умолчанию — умолчание вернулось к main.",
    },
    "account.default_set": {
        "en": "Default account: {label}. Holds across restarts too.",
        "ru": "Аккаунт по умолчанию: {label}. Действует и после перезапуска.",
    },
    "account.index_kept": {
        "en": "The index and dossiers of this account are untouched: {index}, {memory}",
        "ru": "Индекс и досье этого аккаунта не тронуты: {index}, {memory}",
    },
    "account.label_placeholder": {
        "en": "<label>",
        "ru": "<метка>",
    },
    "account.none": {
        "en": "Not a single account. Sign in: {command}  (a second one: --account work)",
        "ru": "Ни одного аккаунта. Войти: {command}  (второй: --account work)",
    },
    "account.none_known": {
        "en": "none",
        "ru": "ни одного",
    },
    "account.not_logged_in": {
        "en": (
            "Account {label} is not signed in (available: {have}). The owner signs in themselves, "
            "the agent never sees the code: {command}"
        ),
        "ru": (
            "Аккаунт {label} не залогинен (есть: {have}). Вход делает владелец сам, агент кода не "
            "видит: {command}"
        ),
    },
    "account.session_files_removed": {
        "en": "Local session files deleted.",
        "ru": "Локальные файлы сессии удалены.",
    },
    "account.session_revoked": {
        "en": "Session revoked on the Telegram side.",
        "ru": "Сессия отозвана на стороне Telegram.",
    },

    # --- alert -------------------------------------------------------------
    "alert.account": {
        "en": "account {account}",
        "ru": "аккаунт {account}",
    },
    "alert.attachment": {
        "en": "attachment",
        "ru": "вложение",
    },
    "alert.on_message": {
        "en": "to message:",
        "ru": "на сообщение:",
    },
    "alert.open": {
        "en": "Open",
        "ru": "Открыть",
    },
    "alert.reaction": {
        "en": "Reaction",
        "ru": "Реакция",
    },
    "alert.someone": {
        "en": "someone",
        "ru": "кто-то",
    },
    "alert.tag_keyword": {
        "en": "Keyword",
        "ru": "Ключевое слово",
    },
    "alert.tag_mention": {
        "en": "Mention",
        "ru": "Упоминание",
    },
    "alert.tag_private": {
        "en": "DM",
        "ru": "ЛС",
    },
    "alert.tag_reply": {
        "en": "Reply to you",
        "ru": "Ответ вам",
    },
    "alert.tag_watch": {
        "en": "Watched chat",
        "ru": "Отслеживаемый чат",
    },
    "alert.transcript": {
        "en": "Transcript:",
        "ru": "Расшифровка:",
    },

    # --- bot ---------------------------------------------------------------
    "bot.action_failed": {
        "en": "error: {error}",
        "ru": "ошибка: {error}",
    },
    "bot.actions_title": {
        "en": "Recent actions",
        "ru": "Последние действия",
    },
    "bot.answer_taken": {
        "en": "Taken: {answer}",
        "ru": "Принято: {answer}",
    },
    "bot.api_failed": {
        "en": "Bot API refused {method}: {error}",
        "ru": "Bot API отказал на {method}: {error}",
    },
    "bot.chat_id_missing": {
        "en": (
            "TG_ALERT_CHAT_ID is not set: there is nowhere to send the notification. Press Start "
            "in your bot and run `uv run tg link-bot`."
        ),
        "ru": (
            "TG_ALERT_CHAT_ID не задан: некуда слать уведомление. Нажми Start у своего бота и "
            "запусти `uv run tg link-bot`."
        ),
    },
    "bot.error": {
        "en": "Error: {error}",
        "ru": "Ошибка: {error}",
    },
    "bot.help": {
        "en": (
            "<b>Telegram agent</b>\n/status — state\n/can — what the agent can reach and what is "
            "missing\n/unread — unread\n/actions — what the agent did\n/rules — current alert "
            "rules\n/watch &lt;chat&gt; — alert on every message of a chat\n/mute &lt;chat&gt; — "
            "do not alert about a chat\n/pause, /resume — switch alerts off/on"
        ),
        "ru": (
            "<b>Telegram-агент</b>\n/status — состояние\n/can — что агенту доступно и чего не "
            "хватает\n/unread — непрочитанное\n/actions — что агент делал\n/rules — текущие "
            "правила алертов\n/watch &lt;чат&gt; — алертить по всем сообщениям чата\n/mute "
            "&lt;чат&gt; — не алертить про чат\n/pause, /resume — выключить/включить алерты"
        ),
    },
    "bot.muted": {
        "en": "No more alerts about: {chat}",
        "ru": "Больше не алерчу про: {chat}",
    },
    "bot.no_actions": {
        "en": "The agent did nothing.",
        "ru": "Агент ничего не делал.",
    },
    "bot.no_unread": {
        "en": "Nothing unread.",
        "ru": "Непрочитанного нет.",
    },
    "bot.paused": {
        "en": "Alerts are paused. /resume to bring them back.",
        "ru": "Алерты на паузе. /resume чтобы вернуть.",
    },
    "bot.question_stale": {
        "en": "This question is no longer current",
        "ru": "Этот вопрос уже неактуален",
    },
    "bot.resumed": {
        "en": "Alerts are active again.",
        "ru": "Алерты снова активны.",
    },
    "bot.rules_active": {
        "en": "active",
        "ru": "активны",
    },
    "bot.rules_paused": {
        "en": "paused",
        "ru": "пауза",
    },
    "bot.status": {
        "en": (
            "<b>Status</b>\nAccount: {account}\nUptime: {uptime} min\nAlerts: {alerts}\nRules: "
            "{rules}"
        ),
        "ru": (
            "<b>Статус</b>\nАккаунт: {account}\nАптайм: {uptime} мин\nАлертов: {alerts}\nПравила: "
            "{rules}"
        ),
    },
    "bot.token_missing": {
        "en": (
            "TG_BOT_TOKEN is not set: the notification channel is not configured. Create a bot "
            "with @BotFather and run `uv run tg setup`."
        ),
        "ru": (
            "TG_BOT_TOKEN не задан: канал уведомлений не настроен. Заведи бота у @BotFather и "
            "запусти `uv run tg setup`."
        ),
    },
    "bot.watching": {
        "en": "Watching: {chat}",
        "ru": "Слежу за: {chat}",
    },

    # --- cli ---------------------------------------------------------------
    "cli.arg_account": {
        "en": "account label: main by default, for example work or second",
        "ru": "метка аккаунта: main по умолчанию, например work или second",
    },
    "cli.arg_call_params": {
        "en": "JSON, e.g. '{\"limit\": 5}'",
        "ru": "JSON, напр. '{\"limit\": 5}'",
    },
    "cli.arg_default_account": {
        "en": "remember this account as the default for all clients and restarts",
        "ru": "запомнить этот аккаунт как умолчание для всех клиентов и перезапусков",
    },
    "cli.arg_password": {
        "en": "2FA password, if it is enabled",
        "ru": "пароль 2FA, если включён",
    },
    "cli.cmd_accounts": {
        "en": "which accounts are signed in",
        "ru": "какие аккаунты залогинены",
    },
    "cli.cmd_call": {
        "en": "call a daemon method directly",
        "ru": "вызвать метод демона напрямую",
    },
    "cli.cmd_capabilities": {
        "en": "what is available and what is missing",
        "ru": "что доступно и чего не хватает",
    },
    "cli.cmd_daemon": {
        "en": "daemon control",
        "ru": "управление демоном",
    },
    "cli.cmd_daemon_run": {
        "en": "in the foreground (for docker/launchd)",
        "ru": "в переднем плане (для docker/launchd)",
    },
    "cli.cmd_daemon_status": {
        "en": "same as tg status",
        "ru": "то же, что tg status",
    },
    "cli.cmd_doctor": {
        "en": "installation diagnostics: what is in place, what is broken, what to do",
        "ru": "диагностика установки: что стоит, что сломано, что делать",
    },
    "cli.cmd_init": {
        "en": "setup wizard: walk through everything up to a working state",
        "ru": "мастер установки: провести через всё до рабочего состояния",
    },
    "cli.cmd_link_bot": {
        "en": "link the chat_id for alerts",
        "ru": "привязать chat_id для алертов",
    },
    "cli.cmd_login": {
        "en": "sign in to a Telegram account",
        "ru": "войти в Telegram-аккаунт",
    },
    "cli.cmd_logout": {
        "en": "revoke the session and delete the files",
        "ru": "отозвать сессию и удалить файлы",
    },
    "cli.cmd_password": {
        "en": "step 3: enter the cloud 2FA password",
        "ru": "шаг 3: ввести облачный пароль 2FA",
    },
    "cli.cmd_send_code": {
        "en": "sign-in step 1: request the code",
        "ru": "шаг 1 входа: запросить код",
    },
    "cli.cmd_setup": {
        "en": "enter api_id/api_hash and the bot token",
        "ru": "ввести api_id/api_hash и токен бота",
    },
    "cli.cmd_sign_in": {
        "en": "sign-in step 2: confirm the code",
        "ru": "шаг 2 входа: подтвердить код",
    },
    "cli.cmd_status": {
        "en": "state of the installation",
        "ru": "состояние установки",
    },
    "cli.description": {
        "en": "Telegram agent control",
        "ru": "Управление Telegram-агентом",
    },
    "cli.error": {
        "en": "Error: {error}",
        "ru": "Ошибка: {error}",
    },
    "cli.metavar_label": {
        "en": "LABEL",
        "ru": "МЕТКА",
    },
    "cli.no_file": {
        "en": "(no file {path})",
        "ru": "(нет файла {path})",
    },

    # --- confirm -----------------------------------------------------------
    "confirm.account": {
        "en": "account: {account}",
        "ru": "аккаунт: {account}",
    },
    "confirm.allow": {
        "en": "allow",
        "ru": "разрешить",
    },
    "confirm.ask_title": {
        "en": "A question from the agent",
        "ru": "Вопрос от агента",
    },
    "confirm.chat": {
        "en": "chat: {chat}",
        "ru": "чат: {chat}",
    },
    "confirm.deny": {
        "en": "deny",
        "ru": "отказать",
    },
    "confirm.more": {
        "en": "more: {rest}",
        "ru": "ещё: {rest}",
    },
    "confirm.no": {
        "en": "no",
        "ru": "нет",
    },
    "confirm.saved_messages": {
        "en": "Saved Messages",
        "ru": "Избранное",
    },
    "confirm.wants": {
        "en": "The agent wants: {method}",
        "ru": "Агент хочет: {method}",
    },
    "confirm.yes": {
        "en": "yes",
        "ru": "да",
    },

    # --- daemon ------------------------------------------------------------
    "daemon.already_running": {
        "en": "The daemon is already running (pid {pid}).",
        "ru": "Демон уже работает (pid {pid}).",
    },
    "daemon.connected": {
        "en": "The agent is connected to Telegram. /help — what I can do.",
        "ru": "Агент подключён к Telegram. /help — что умею.",
    },
    "daemon.no_sessions": {
        "en": "There is no Telegram session at all.",
        "ru": "Нет ни одной сессии Telegram.",
    },
    "daemon.not_running": {
        "en": "The daemon is not running.",
        "ru": "Демон не запущен.",
    },
    "daemon.start_timeout": {
        "en": "The daemon did not come up in 18 seconds. Last lines of the log:",
        "ru": "Демон не поднялся за 18 секунд. Последние строки лога:",
    },
    "daemon.started": {
        "en": "Daemon started (pid {pid}). Log: {log}",
        "ru": "Демон запущен (pid {pid}). Лог: {log}",
    },
    "daemon.stop_timeout": {
        "en": "Did not stop on SIGTERM.",
        "ru": "Не остановился по SIGTERM.",
    },
    "daemon.stopped": {
        "en": "Daemon stopped.",
        "ru": "Демон остановлен.",
    },

    # --- digest ------------------------------------------------------------
    "digest.by_rules": {
        "en": "By rules",
        "ru": "По правилам",
    },
    "digest.counts": {
        "en": "Chats: {chats} · messages: {messages}",
        "ru": "Чатов: {chats} · сообщений: {messages}",
    },
    "digest.filters_fired": {
        "en": "Filters fired: {count}",
        "ru": "Сработало фильтров: {count}",
    },
    "digest.hit_keyword": {
        "en": "word “{word}”",
        "ru": "слово «{word}»",
    },
    "digest.hit_watched": {
        "en": "watched chat",
        "ru": "отслеживаемый чат",
    },
    "digest.reactions": {
        "en": "Reactions to your messages: {count}",
        "ru": "Реакций на твои сообщения: {count}",
    },
    "digest.title": {
        "en": "Digest",
        "ru": "Дайджест",
    },
    "digest.top": {
        "en": "Most of all",
        "ru": "Больше всего",
    },
    "digest.unread": {
        "en": "Unread: {total} in {chats} chats",
        "ru": "Непрочитано: {total} в {chats} чатах",
    },

    # --- doctor ------------------------------------------------------------
    "doctor.accounts": {
        "en": "signed in: {accounts}; default {default}",
        "ru": "залогинены: {accounts}; по умолчанию {default}",
    },
    "doctor.accounts_none": {
        "en": "not a single signed-in account",
        "ru": "ни одного залогиненного аккаунта",
    },
    "doctor.agent_differs": {
        "en": "{name}: differs from the repository",
        "ru": "{name}: отличается от репозитория",
    },
    "doctor.agent_fix": {
        "en": "uv run tg init — it will ask again and update; by hand: cp {src} {dst}",
        "ru": "uv run tg init — переспросит и обновит; вручную: cp {src} {dst}",
    },
    "doctor.agent_missing": {
        "en": "{name}: not installed",
        "ru": "{name}: не установлен",
    },
    "doctor.agent_no_source": {
        "en": "{name}: no source, nothing to install",
        "ru": "{name}: нет исходника, ставить нечего",
    },
    "doctor.agent_no_source_fix": {
        "en": (
            "the installation is incomplete — the subagents live in the repository: git clone "
            "https://github.com/draiqw/tg-mcp"
        ),
        "ru": (
            "установка неполная — субагенты лежат в репозитории: git clone "
            "https://github.com/draiqw/tg-mcp"
        ),
    },
    "doctor.agent_same": {
        "en": "{name}: matches the repository",
        "ru": "{name}: совпадает с репозиторием",
    },
    "doctor.all_good": {
        "en": "everything is in place",
        "ru": "всё на месте",
    },
    "doctor.api_fix": {
        "en": "uv run tg init (or uv run tg setup)",
        "ru": "uv run tg init (или uv run tg setup)",
    },
    "doctor.api_set": {
        "en": "api_id/api_hash are set",
        "ru": "api_id/api_hash заданы",
    },
    "doctor.api_unset": {
        "en": "api_id/api_hash are not set",
        "ru": "api_id/api_hash не заданы",
    },
    "doctor.autostart_fix": {
        "en": "uv run tg init will offer to install it",
        "ru": "uv run tg init предложит его поставить",
    },
    "doctor.autostart_missing": {
        "en": "there is no autostart ({kind}): after a reboot the daemon has to be raised by hand",
        "ru": "автозапуска ({kind}) нет: после перезагрузки демон поднимать руками",
    },
    "doctor.autostart_ok": {
        "en": "daemon autostart is installed ({kind})",
        "ru": "автозапуск демона установлен ({kind})",
    },
    "doctor.bad_count": {
        "en": "bad: {n}",
        "ru": "плохо: {n}",
    },
    "doctor.bot_fix": {
        "en": "uv run tg init: without the bot there are no alerts, no digest and no tg_ask",
        "ru": "uv run tg init: без бота нет алертов, дайджеста и tg_ask",
    },
    "doctor.bot_link_fix": {
        "en": "uv run tg link-bot — press Start in the chat with the bot",
        "ru": "uv run tg link-bot — нажми Start в чате с ботом",
    },
    "doctor.bot_missing": {
        "en": "the notification bot is not set up",
        "ru": "бот уведомлений не настроен",
    },
    "doctor.bot_no_chat": {
        "en": "the bot token is there, chat_id is not linked",
        "ru": "токен бота есть, chat_id не привязан",
    },
    "doctor.bot_ok": {
        "en": "the notification bot is set up",
        "ru": "бот уведомлений настроен",
    },
    "doctor.claude_missing": {
        "en": "claude not found in PATH",
        "ru": "claude не найден в PATH",
    },
    "doctor.claude_missing_fix": {
        "en": (
            "if the client is a different one this is normal; the registration command is printed "
            "by uv run tg init"
        ),
        "ru": "если клиент другой — это нормально; команду регистрации печатает uv run tg init",
    },
    "doctor.daemon_running": {
        "en": "running, pid {pid}",
        "ru": "работает, pid {pid}",
    },
    "doctor.daemon_stopped": {
        "en": "not running",
        "ru": "не запущен",
    },
    "doctor.data": {
        "en": "data directory: {path}",
        "ru": "каталог данных: {path}",
    },
    "doctor.data_absent": {
        "en": " (not created yet)",
        "ru": " (ещё не создан)",
    },
    "doctor.data_fix": {
        "en": "chmod 700 {path} — the session, the index and the dossiers are there",
        "ru": "chmod 700 {path} — там сессия, индекс и досье",
    },
    "doctor.data_mode": {
        "en": ", mode {mode}",
        "ru": ", права {mode}",
    },
    "doctor.env_fix": {
        "en": "chmod 600 {path} — it holds full access to the account",
        "ru": "chmod 600 {path} — в нём полный доступ к аккаунту",
    },
    "doctor.env_from_environment": {
        "en": "no .env, the values are taken from the environment",
        "ru": ".env нет, значения берутся из окружения",
    },
    "doctor.env_missing": {
        "en": "no .env",
        "ru": ".env нет",
    },
    "doctor.env_mode": {
        "en": ".env is there, mode {mode}",
        "ru": ".env есть, права {mode}",
    },
    "doctor.env_ok": {
        "en": ".env is there",
        "ru": ".env есть",
    },
    "doctor.fix_hint": {
        "en": "most of this is fixed by the wizard: uv run tg init",
        "ru": "большую часть этого чинит мастер: uv run tg init",
    },
    "doctor.groq_set": {
        "en": "GROQ_API_KEY is set",
        "ru": "GROQ_API_KEY задан",
    },
    "doctor.groq_unset": {
        "en": "GROQ_API_KEY is not set",
        "ru": "GROQ_API_KEY не задан",
    },
    "doctor.login_pending": {
        "en": (
            "the sign-in is not finished: the code was accepted, the cloud password was not "
            "entered"
        ),
        "ru": "вход не завершён: код принят, облачный пароль не введён",
    },
    "doctor.login_pending_fix": {
        "en": "uv run tg password — the password is typed only by hand, from a live terminal",
        "ru": "uv run tg password — пароль вводится только руками, с живого терминала",
    },
    "doctor.mcp_missing": {
        "en": "MCP server {name} is not registered",
        "ru": "MCP-сервер {name} не зарегистрирован",
    },
    "doctor.mcp_registered": {
        "en": "MCP server {name} is registered",
        "ru": "MCP-сервер {name} зарегистрирован",
    },
    "doctor.no_secrets": {
        "en": (
            "The report has no keys, no phone number and no account name — it can be attached to "
            "an issue as is."
        ),
        "ru": (
            "В отчёте нет ключей, телефона и имени аккаунта — его можно приложить к issue как "
            "есть."
        ),
    },
    "doctor.openai_set": {
        "en": "OPENAI_API_KEY is set",
        "ru": "OPENAI_API_KEY задан",
    },
    "doctor.openai_unset": {
        "en": "OPENAI_API_KEY is not set — chat dossiers are not updated",
        "ru": "OPENAI_API_KEY не задан — досье на чаты не обновляются",
    },
    "doctor.root": {
        "en": "project directory: {path}",
        "ru": "каталог проекта: {path}",
    },
    "doctor.rpc_bad": {
        "en": "RPC does not answer: {error}",
        "ru": "RPC не отвечает: {error}",
    },
    "doctor.rpc_fix": {
        "en": "uv run tg daemon restart, then uv run tg daemon logs",
        "ru": "uv run tg daemon restart, потом uv run tg daemon logs",
    },
    "doctor.rpc_ok": {
        "en": "RPC answers, live sessions: {sessions}",
        "ru": "RPC отвечает, живых сессий: {sessions}",
    },
    "doctor.section_accounts": {
        "en": "accounts",
        "ru": "аккаунты",
    },
    "doctor.section_daemon": {
        "en": "daemon",
        "ru": "демон",
    },
    "doctor.section_install": {
        "en": "installation",
        "ru": "установка",
    },
    "doctor.section_keys": {
        "en": "keys",
        "ru": "ключи",
    },
    "doctor.section_optional": {
        "en": "optional",
        "ru": "необязательное",
    },
    "doctor.session_fix": {
        "en": (
            "chmod 600 {path} — this file is itself the sign-in to the account without a password "
            "and without 2FA"
        ),
        "ru": "chmod 600 {path} — этот файл и есть вход в аккаунт без пароля и без 2FA",
    },
    "doctor.session_mode": {
        "en": "session file {account}: mode {mode}",
        "ru": "файл сессии {account}: права {mode}",
    },
    "doctor.session_ok": {
        "en": "session file {account} is in place",
        "ru": "файл сессии {account} на месте",
    },
    "doctor.socket_stale": {
        "en": "the socket file is left over from a dead daemon",
        "ru": "файл сокета остался от умершего демона",
    },
    "doctor.status_bad": {
        "en": "bad",
        "ru": "плохо",
    },
    "doctor.status_ok": {
        "en": "ok",
        "ru": "ок",
    },
    "doctor.status_skip": {
        "en": "skip",
        "ru": "мимо",
    },
    "doctor.uv_fix": {
        "en": "install uv: https://docs.astral.sh/uv/ — everything is started with it",
        "ru": "поставь uv: https://docs.astral.sh/uv/ — им запускается всё",
    },
    "doctor.uv_missing": {
        "en": "uv not found in PATH",
        "ru": "uv не найден в PATH",
    },
    "doctor.whisper_missing": {
        "en": "there is no local transcription model",
        "ru": "локальной модели расшифровки нет",
    },
    "doctor.whisper_ok": {
        "en": "local transcription: {what}",
        "ru": "локальная расшифровка: {what}",
    },
    "doctor.write_off": {
        "en": "writing is off (TG_ALLOW_WRITE=0), the agent only reads",
        "ru": "запись выключена (TG_ALLOW_WRITE=0), агент только читает",
    },
    "doctor.write_on": {
        "en": "writing to the account is allowed",
        "ru": "запись в аккаунт разрешена",
    },

    # --- init --------------------------------------------------------------
    "init.agent_differs": {
        "en": "{name} is already there and differs from the version in the repository.",
        "ru": "{name} уже есть и отличается от версии в репозитории.",
    },
    "init.agent_differs_why": {
        "en": "A difference usually means an outdated tool set for the agent,",
        "ru": "Отличие обычно значит устаревший набор инструментов у агента,",
    },
    "init.agent_differs_yours": {
        "en": "but it may also be your own edit.",
        "ru": "но это может быть и твоя правка.",
    },
    "init.agent_installed": {
        "en": "installed",
        "ru": "установлен",
    },
    "init.agent_kept": {
        "en": "left as it was",
        "ru": "оставлен как был",
    },
    "init.agent_kept_skipped": {
        "en": "subagent {name} differs: {cmd}",
        "ru": "субагент {name} отличается: {cmd}",
    },
    "init.agent_overwrite_ask": {
        "en": "Overwrite {name}?",
        "ru": "Перезаписать {name}?",
    },
    "init.agent_replaced": {
        "en": "updated",
        "ru": "обновлён",
    },
    "init.agent_same": {
        "en": "already up to date",
        "ru": "уже актуален",
    },
    "init.agents_intro": {
        "en": (
            "Subagents are ready-made roles for Claude Code: telegram (all the tools)\nand "
            "telegram-watch (a trimmed set for background checks). The client reads them\nfrom "
            "{dir}."
        ),
        "ru": (
            "Субагенты — это готовые роли для Claude Code: telegram (все инструменты)\nи "
            "telegram-watch (урезанный набор для фоновых проверок). Клиент читает их\nиз {dir}."
        ),
    },
    "init.api_intro": {
        "en": (
            "App keys are access to MTProto, that is to the whole account.\nWithout them only the "
            "Bot API is left: you see just what was written to the\nbot, but not your own chats, "
            "history and search.\nWhere to get them: https://my.telegram.org → API development "
            "tools → create\nan application. Any name will do. The keys go into .env with mode "
            "600."
        ),
        "ru": (
            "Ключи приложения — это доступ к MTProto, то есть к аккаунту целиком.\nБез них "
            "останется только Bot API: видно лишь то, что написали боту,\nа свои чаты, история и "
            "поиск — нет.\nГде взять: https://my.telegram.org → API development tools → "
            "создать\nприложение. Название любое. Ключи лягут в .env с правами 600."
        ),
    },
    "init.autostart_ask": {
        "en": "Install autostart?",
        "ru": "Поставить автозапуск?",
    },
    "init.autostart_by_hand": {
        "en": "By hand: {cmd}",
        "ru": "Руками: {cmd}",
    },
    "init.autostart_installed": {
        "en": "Installed: {path}",
        "ru": "Установлен: {path}",
    },
    "init.autostart_intro": {
        "en": (
            "Autostart raises the daemon at system sign-in so that alerts and\nreminders work "
            "without Claude running. This is {kind} in\n{dir}, and it does not require "
            "administrator rights."
        ),
        "ru": (
            "Автозапуск поднимает демон при входе в систему, чтобы алерты и\nнапоминания работали "
            "без запущенного Claude. Это {kind} в\n{dir}, он не требует прав администратора."
        ),
    },
    "init.autostart_manual": {
        "en": (
            "copy {template} into {dir}, substitute your own paths in it and switch it on: "
            "{enable}"
        ),
        "ru": "скопируй {template} в {dir}, подставь в нём свои пути и включи: {enable}",
    },
    "init.autostart_no_template": {
        "en": "There is no template {path} — skipping.",
        "ru": "Нет шаблона {path} — пропускаю.",
    },
    "init.autostart_no_uv": {
        "en": "uv is not found in PATH — {kind} needs an absolute path to it.",
        "ru": "uv не найден в PATH — {kind} нужен абсолютный путь до него.",
    },
    "init.autostart_none": {
        "en": "this system has no autostart — keep the daemon in docker (see docs/docker.md)",
        "ru": "на этой системе автозапуска нет — держи демон в docker (см. docs/docker.md)",
    },
    "init.autostart_not_enabled": {
        "en": "The file is written ({path}), but switching it on did not work out: {why}",
        "ru": "Файл записан ({path}), но включить не вышло: {why}",
    },
    "init.autostart_unsupported": {
        "en": (
            "The wizard can install autostart on macOS (launchd) and Linux (systemd);\nthis system "
            "is {platform}. Keep the daemon in docker: there the role of autostart\nis played by "
            "restart: unless-stopped — see docs/docker.md."
        ),
        "ru": (
            "Автозапуск мастер умеет ставить на macOS (launchd) и Linux (systemd);\nэта система — "
            "{platform}. Держи демон в docker: там роль автозапуска\nиграет restart: "
            "unless-stopped — см. docs/docker.md."
        ),
    },
    "init.bot_intro": {
        "en": (
            "The bot is needed as a back channel: alerts about important incoming messages,\nthe "
            "scheduled digest, the agent's questions (tg_ask) and write confirmations\narrive in "
            "it. Start a SEPARATE bot for the agent: @BotFather → /newbot. Somebody\nelse's bot "
            "cannot be reused — its messages become incoming for you too and\ntrigger an alert, "
            "which the next alert will answer (the daemon ignores\nonly its own bot, known by its "
            "token).\nWithout the bot everything else works; alerts, the digest and tg_ask "
            "just\nsilently disappear."
        ),
        "ru": (
            "Бот нужен как обратный канал: в него приходят алерты о важных входящих,\nдайджест по "
            "расписанию, вопросы агента (tg_ask) и подтверждения записи.\nЗаводи ОТДЕЛЬНОГО бота "
            "под агента: @BotFather → /newbot. Чужого бота\nпереиспользовать нельзя — его "
            "сообщения станут входящими для тебя же и\nвызовут алерт, на который придёт следующий "
            "алерт (демон игнорирует\nтолько своего бота, известного по токену).\nБез бота всё "
            "остальное работает; молча пропадут алерты, дайджест и tg_ask."
        ),
    },
    "init.bot_link_ask": {
        "en": "Link it now (you have to press Start at the bot)?",
        "ru": "Привязать сейчас (надо нажать Start у бота)?",
    },
    "init.bot_link_skipped": {
        "en": "linking the bot's chat_id: press Start at your bot and run uv run tg link-bot",
        "ru": "привязка chat_id бота: нажми Start у своего бота и запусти uv run tg link-bot",
    },
    "init.bot_token_only": {
        "en": "The token is already there, only chat_id is missing.",
        "ru": "Токен уже есть, не хватает только chat_id.",
    },
    "init.check_all": {
        "en": "To check the whole installation: uv run tg doctor",
        "ru": "Проверить установку целиком: uv run tg doctor",
    },
    "init.daemon_failed": {
        "en": "The daemon did not come up. Common reasons:",
        "ru": "Демон не поднялся. Частые причины:",
    },
    "init.daemon_intro": {
        "en": (
            "The daemon owns the Telegram session and does all the work: the MCP server "
            "only\nforwards calls to it through a unix socket. Without the daemon the tools do "
            "not\nwork, and alerts, the digest and reminders do not exist."
        ),
        "ru": (
            "Демон владеет сессией Telegram и делает всю работу: MCP-сервер только\nпересылает ему "
            "вызовы через unix-сокет. Без демона инструменты не\nработают, а алерты, дайджест и "
            "напоминания не существуют."
        ),
    },
    "init.daemon_log": {
        "en": "Full log: uv run tg daemon logs -n 50 ({path})",
        "ru": "Полный лог: uv run tg daemon logs -n 50 ({path})",
    },
    "init.daemon_reason_login": {
        "en": "the sign-in is not finished: uv run tg password",
        "ru": "вход не завершён: uv run tg password",
    },
    "init.daemon_reason_running": {
        "en": (
            "the daemon is already started by another copy of the project — check: ps ax | grep "
            "tgagent"
        ),
        "ru": "демон уже запущен другой копией проекта — проверь: ps ax | grep tgagent",
    },
    "init.daemon_reason_socket": {
        "en": "a socket file is left over from the previous run: rm {path}",
        "ru": "остался файл сокета от прошлого запуска: rm {path}",
    },
    "init.detail_set": {
        "en": "set",
        "ru": "задан",
    },
    "init.finish": {
        "en": "Done. What is available now:",
        "ru": "Готово. Что теперь доступно:",
    },
    "init.groq_why": {
        "en": (
            "Groq transcribes voice messages, video notes, music and video. Without it what "
            "is\nleft is Telegram's built-in transcription (voice messages and video notes "
            "only,\nPremium required) and the local model, if it is installed."
        ),
        "ru": (
            "Groq расшифровывает голосовые, кружки, музыку и видео. Без него остаётся\nвстроенная "
            "расшифровка Telegram (только голосовые и кружки, нужен Premium)\nи локальная модель, "
            "если её поставить."
        ),
    },
    "init.install_failed": {
        "en": "Did not install: {why}",
        "ru": "Не поставилось: {why}",
    },
    "init.interrupted": {
        "en": "Interrupted. What is already done is saved — run uv run tg init again.",
        "ru": "Прервано. Уже сделанное сохранено — запусти uv run tg init заново.",
    },
    "init.key_prompt": {
        "en": "{name} (input hidden, Enter to skip)",
        "ru": "{name} (ввод скрыт, Enter чтобы пропустить)",
    },
    "init.local_whisper_ask": {
        "en": "Install it now (uv sync --extra local-whisper)?",
        "ru": "Поставить сейчас (uv sync --extra local-whisper)?",
    },
    "init.local_whisper_intro": {
        "en": (
            "The local transcription model works without the internet and without keys, but\nit "
            "takes up space and time to install. What gets installed is decided by the\nsystem: on "
            "Apple Silicon it is mlx-whisper, on other hardware faster-whisper."
        ),
        "ru": (
            "Локальная модель расшифровки работает без интернета и без ключей, но\nзанимает место "
            "и время на установку. Что поставится, решает система:\nна Apple Silicon это "
            "mlx-whisper, на остальном железе faster-whisper."
        ),
    },
    "init.login_failed": {
        "en": "Sign-in did not go through: {why}",
        "ru": "Вход не прошёл: {why}",
    },
    "init.login_interrupted": {
        "en": "Sign-in interrupted. To repeat: uv run tg login",
        "ru": "Вход прерван. Повторить: uv run tg login",
    },
    "init.login_intro": {
        "en": (
            "Now comes the sign-in to Telegram. The code arrives in the app itself (not SMS),\nand "
            "you type it — the wizard does not request the code, does not fill it in and\ndoes not "
            "store it. If two-step verification is on, it will ask for the cloud\npassword: that "
            "too is typed by hand and is written down nowhere."
        ),
        "ru": (
            "Сейчас будет вход в Telegram. Код придёт в само приложение (не SMS),\nи вводишь его "
            "ты — мастер код не запрашивает, не подставляет и не хранит.\nЕсли включена "
            "двухэтапная аутентификация, спросит облачный пароль:\nон тоже вводится руками и "
            "никуда не записывается."
        ),
    },
    "init.login_pending": {
        "en": (
            "The sign-in was started earlier and stopped at the 2FA cloud password.\nThe password "
            "is typed only from a live terminal and is saved nowhere:"
        ),
        "ru": (
            "Вход начат раньше и остановился на облачном пароле 2FA.\nПароль вводится только с "
            "живого терминала и нигде не сохраняется:"
        ),
    },
    "init.login_session": {
        "en": (
            "After the sign-in {session} will appear — that is full access to the account\nwithout "
            "a password and without 2FA. The file must not be copied to other machines:\nTelegram "
            "will see two copies of one session and revoke it."
        ),
        "ru": (
            "После входа появится {session} — это полный доступ к\nаккаунту без пароля и без 2FA. "
            "Файл нельзя копировать на другие машины:\nTelegram увидит две копии одной сессии и "
            "отзовёт её."
        ),
    },
    "init.mark_done": {
        "en": "done",
        "ru": "уже есть",
    },
    "init.mark_optional": {
        "en": "optional",
        "ru": "по желанию",
    },
    "init.mark_required": {
        "en": "required",
        "ru": "нужно",
    },
    "init.mcp_already": {
        "en": "The server is already registered — I am not adding it a second time.",
        "ru": "Сервер уже зарегистрирован — второй раз не добавляю.",
    },
    "init.mcp_by_config": {
        "en": "with a config — see docs/mcp.md",
        "ru": "конфигом — см. docs/mcp.md",
    },
    "init.mcp_done": {
        "en": "Registered. Claude Code reads the servers when a session starts —",
        "ru": "Зарегистрирован. Claude Code читает серверы при старте сессии —",
    },
    "init.mcp_failed": {
        "en": "It did not work out: {why}",
        "ru": "Не получилось: {why}",
    },
    "init.mcp_intro": {
        "en": (
            "The server has to be declared to the client once: the command and the "
            "project\ndirectory the wizard knows itself. The user scope means the server will be "
            "available\nin all projects; local would limit it to the current directory."
        ),
        "ru": (
            "Сервер надо один раз объявить клиенту: команду и каталог проекта\nмастер знает сам. "
            "Область видимости user — сервер будет доступен во всех\nпроектах; local ограничил бы "
            "его текущим каталогом."
        ),
    },
    "init.mcp_no_claude": {
        "en": (
            "Claude Code (`claude`) is not in PATH — there is nothing to register the server "
            "with.\nIf the client is a different one (Claude Desktop, your own), set it up by "
            "hand:"
        ),
        "ru": (
            "Claude Code (`claude`) в PATH нет — регистрировать сервер нечем.\nЕсли клиент другой "
            "(Claude Desktop, свой), настрой его вручную:"
        ),
    },
    "init.mcp_no_uv": {
        "en": (
            "uv is not found in the PATH of this shell. The command below is started by the "
            "client:"
        ),
        "ru": "uv не найден в PATH этого шелла. Команда ниже запускается клиентом:",
    },
    "init.mcp_no_uv_note": {
        "en": (
            "if uv is not visible to it either, the server will not start — put uv into the common "
            "PATH."
        ),
        "ru": "если uv не виден и ему, сервер не стартует — поставь uv в общий PATH.",
    },
    "init.mcp_restart_client": {
        "en": "an already open one will have to be restarted, otherwise it will have no tools.",
        "ru": "уже открытую придётся перезапустить, иначе инструментов в ней не будет.",
    },
    "init.mcp_run_by_hand": {
        "en": "Run the command above by hand and see what it says.",
        "ru": "Выполни команду выше руками и посмотри, что она скажет.",
    },
    "init.memory_key_why": {
        "en": (
            "Chat dossiers (tg_memory) are written by a language model — that is the only "
            "place\nwhere pieces of the correspondence leave the machine. Without the key the "
            "dossiers\nsimply are not updated, everything else works as before. base_url can later "
            "be\npointed at a local model — then the correspondence does not leave the "
            "machine\n(see .env.example)."
        ),
        "ru": (
            "Досье на чаты (tg_memory) пишет языковая модель — это единственное место,\nгде куски "
            "переписки уходят с машины. Без ключа досье просто не обновляются,\nостальное работает "
            "как прежде. base_url можно позже увести на локальную\nмодель — тогда переписка машину "
            "не покидает (см. .env.example)."
        ),
    },
    "init.needs_terminal_keys": {
        "en": "There is no terminal, and the keys are typed by hand. Open a terminal and run:",
        "ru": "Терминала нет, а ключи вводятся руками. Открой терминал и выполни:",
    },
    "init.needs_terminal_login": {
        "en": "The sign-in needs a terminal. Open a terminal and run:",
        "ru": "Вход требует терминала. Открой терминал и выполни:",
    },
    "init.no_answer": {
        "en": "{cmd} did not answer in {timeout} s",
        "ru": "{cmd} не ответил за {timeout} с",
    },
    "init.no_command": {
        "en": "no {cmd} command",
        "ru": "нет команды {cmd}",
    },
    "init.nothing_to_do": {
        "en": "Everything is already set up, there is nothing to change.",
        "ru": "Всё уже настроено, менять нечего.",
    },
    "init.optional_continue": {
        "en": "This is an optional step, the installation continues.",
        "ru": "Это необязательный шаг, установка продолжается.",
    },
    "init.optional_tail": {
        "en": "(optional, Enter to skip)",
        "ru": "(необязательно, Enter — пропустить)",
    },
    "init.required_step_failed": {
        "en": "Without this step you cannot go on. Fix it and run the wizard again: uv run tg init",
        "ru": "Без этого шага дальше нельзя. Почини и запусти мастер заново: uv run tg init",
    },
    "init.skipped": {
        "en": "Skipped — {cost}.",
        "ru": "Пропущено — {cost}.",
    },
    "init.skipped_list": {
        "en": "Skipped (and how to switch it on if needed):",
        "ru": "Пропущено (и как включить, если понадобится):",
    },
    "init.step_agents_cost": {
        "en": "there will be no ready-made telegram and telegram-watch subagents",
        "ru": "не будет готовых субагентов telegram и telegram-watch",
    },
    "init.step_agents_done": {
        "en": "match the repository",
        "ru": "совпадают с репозиторием",
    },
    "init.step_agents_title": {
        "en": "subagents in ~/.claude/agents",
        "ru": "субагенты в ~/.claude/agents",
    },
    "init.step_api_cost": {
        "en": "without them nothing works: MTProto will not let you in, only the Bot API is left",
        "ru": "без них не работает ничего: MTProto не пустит, останется только Bot API",
    },
    "init.step_api_done": {
        "en": "already in .env",
        "ru": "уже в .env",
    },
    "init.step_api_title": {
        "en": "app keys (api_id/api_hash)",
        "ru": "ключи приложения (api_id/api_hash)",
    },
    "init.step_autostart_cost": {
        "en": "after a reboot the daemon will have to be raised by hand",
        "ru": "после перезагрузки демон придётся поднимать руками",
    },
    "init.step_autostart_done": {
        "en": "{target} installed",
        "ru": "{target} установлен",
    },
    "init.step_autostart_title": {
        "en": "daemon autostart at system sign-in",
        "ru": "автозапуск демона при входе в систему",
    },
    "init.step_bot_cost": {
        "en": (
            "there will be no alerts about incoming messages, no digest, no tg_ask and no write "
            "confirmation"
        ),
        "ru": "не будет алертов о входящих, дайджеста, tg_ask и подтверждения записи",
    },
    "init.step_bot_done": {
        "en": "token present, chat_id linked",
        "ru": "токен есть, chat_id привязан",
    },
    "init.step_bot_fix": {
        "en": "uv run tg setup, then uv run tg link-bot",
        "ru": "uv run tg setup, затем uv run tg link-bot",
    },
    "init.step_bot_title": {
        "en": "notification bot",
        "ru": "бот уведомлений",
    },
    "init.step_daemon_cost": {
        "en": "without the daemon not a single tool works: the session belongs to it",
        "ru": "без демона не работает ни один инструмент: сессией владеет он",
    },
    "init.step_daemon_done": {
        "en": "running, pid {pid}",
        "ru": "работает, pid {pid}",
    },
    "init.step_daemon_title": {
        "en": "daemon",
        "ru": "демон",
    },
    "init.step_failed": {
        "en": "The step did not pass: {why}",
        "ru": "Шаг не прошёл: {why}",
    },
    "init.step_groq_cost": {
        "en": "transcripts will be left only through Telegram Premium or a local model",
        "ru": "расшифровка останется только через Telegram Premium или локальную модель",
    },
    "init.step_groq_title": {
        "en": "Groq key for audio transcripts",
        "ru": "ключ Groq для расшифровки звука",
    },
    "init.step_local_whisper_cost": {
        "en": (
            "without it and without Groq only Telegram will transcribe audio, and only with "
            "Premium"
        ),
        "ru": "без неё и без Groq звук расшифрует только Telegram, и то с Premium",
    },
    "init.step_local_whisper_title": {
        "en": "local transcription model",
        "ru": "локальная модель расшифровки",
    },
    "init.step_login_cost": {
        "en": "without the sign-in the daemon has nothing to work with: there is no account",
        "ru": "без входа демону нечем работать: аккаунта нет",
    },
    "init.step_login_done": {
        "en": "session in place: {session}",
        "ru": "сессия на месте: {session}",
    },
    "init.step_login_title": {
        "en": "sign-in to the account",
        "ru": "вход в аккаунт",
    },
    "init.step_mcp_cost": {
        "en": "Claude Code will not see the tools — the server is unknown to it",
        "ru": "Claude Code не увидит инструментов — сервер ему неизвестен",
    },
    "init.step_mcp_done": {
        "en": "the telegram server is registered",
        "ru": "сервер telegram зарегистрирован",
    },
    "init.step_mcp_title": {
        "en": "registering the server with Claude Code",
        "ru": "регистрация сервера в Claude Code",
    },
    "init.step_memory_key_cost": {
        "en": "tg_memory will not be able to update chat dossiers (reading ready ones — it will)",
        "ru": "tg_memory не сможет обновлять досье на чаты (читать готовые — сможет)",
    },
    "init.step_memory_key_title": {
        "en": "model key for chat dossiers (OPENAI_API_KEY)",
        "ru": "ключ модели для досье (OPENAI_API_KEY)",
    },
    "init.where_to_get": {
        "en": "Where to get it: {where}",
        "ru": "Где взять: {where}",
    },
    "init.wizard_intro": {
        "en": (
            "Setup wizard. It looks at what is already done and does only what is missing —\na "
            "repeat run breaks nothing and works as \"fix my installation\".\nOnly the app keys "
            "and the sign-in are required; the rest is skipped with Enter."
        ),
        "ru": (
            "Мастер установки. Смотрит, что уже сделано, и делает только недостающее —\nповторный "
            "запуск ничего не ломает и годится как «почини установку».\nОбязательны только ключи "
            "приложения и вход; остальное пропускается по Enter."
        ),
    },
    "init.written_to": {
        "en": "Written to {path}",
        "ru": "Записано в {path}",
    },
    "init.yes_words": {
        "en": "y yes",
        "ru": "д да",
    },

    # --- login -------------------------------------------------------------
    "login.aborted": {
        "en": "\nSign-in interrupted, nothing was saved. Retry: {command}",
        "ru": "\nВход прерван, ничего не сохранено. Повторить: {command}",
    },
    "login.already": {
        "en": "Already signed in: {name} (@{username}). The session is in place.",
        "ru": "Уже авторизован: {name} (@{username}). Сессия на месте.",
    },
    "login.already_short": {
        "en": "Already signed in as {name} (@{username}).",
        "ru": "Уже авторизован как {name} (@{username}).",
    },
    "login.cloud_password_prompt": {
        "en": "Cloud 2FA password (attempt {attempt}/3, hidden input): ",
        "ru": "Облачный пароль 2FA (попытка {attempt}/3, ввод скрыт): ",
    },
    "login.code_prompt": {
        "en": "Code: ",
        "ru": "Код: ",
    },
    "login.code_sent": {
        "en": "Code sent to Telegram (not SMS — check the app).",
        "ru": "Код отправлен в Telegram (не SMS — смотри в приложении).",
    },
    "login.code_sent_to": {
        "en": "Code sent to {phone} (to the Telegram app). Next: tg sign-in --code XXXXX",
        "ru": "Код отправлен на {phone} (в приложение Telegram). Дальше: tg sign-in --code XXXXX",
    },
    "login.done": {
        "en": "\nDone: signed in as {name} (@{username}, id {user_id}).",
        "ru": "\nГотово: вошёл как {name} (@{username}, id {user_id}).",
    },
    "login.hint_api_id_flood": {
        "en": (
            "Telegram considers these api_id/api_hash leaked into public access and has limited "
            "them. Create a new application on my.telegram.org and enter its keys: uv run tg setup"
        ),
        "ru": (
            "эти api_id/api_hash Telegram считает утёкшими в публичный доступ и ограничил. Создай "
            "на my.telegram.org новое приложение и введи его ключи: uv run tg setup"
        ),
    },
    "login.hint_api_id_invalid": {
        "en": (
            "Telegram did not accept api_id/api_hash. Check that they were copied from "
            "my.telegram.org → API development tools in full: uv run tg setup"
        ),
        "ru": (
            "Telegram не принял api_id/api_hash. Проверь, что они скопированы с my.telegram.org → "
            "API development tools целиком: uv run tg setup"
        ),
    },
    "login.hint_auth_key_duplicated": {
        "en": (
            "the session file was used from another machine at the same time, and Telegram revoked "
            "it. data/session.session must not be copied between machines — sign in again: uv run "
            "tg login"
        ),
        "ru": (
            "файл сессии использовался с другой машины одновременно, и Telegram его отозвал. "
            "Копировать data/session.session между машинами нельзя — войди заново: uv run tg login"
        ),
    },
    "login.hint_auth_key_unregistered": {
        "en": (
            "the session was revoked on the Telegram side (usually it was closed in the device "
            "list). Sign in again: uv run tg login"
        ),
        "ru": (
            "сессия отозвана на стороне Telegram (обычно её закрыли в списке устройств). Войди "
            "заново: uv run tg login"
        ),
    },
    "login.hint_code_empty": {
        "en": "no code was entered. Start the sign-in again: uv run tg login",
        "ru": "код не введён. Запусти вход заново: uv run tg login",
    },
    "login.hint_code_expired": {
        "en": (
            "the code has gone stale — Telegram keeps it for a few minutes. Request a new one: uv "
            "run tg login"
        ),
        "ru": "код протух — Telegram держит его несколько минут. Запроси новый: uv run tg login",
    },
    "login.hint_code_invalid": {
        "en": (
            "the code did not fit. It arrives in the Telegram app itself (not SMS) and is typed "
            "without spaces. Start the sign-in again: uv run tg login"
        ),
        "ru": (
            "код не подошёл. Он приходит в само приложение Telegram (не SMS) и вводится без "
            "пробелов. Запусти вход заново: uv run tg login"
        ),
    },
    "login.hint_password_invalid": {
        "en": (
            "the cloud password of two-step verification did not fit. This is the Telegram "
            "password (Settings → Privacy and Security → Two-Step Verification), not the password "
            "of your mail or Apple ID. To repeat only the password: uv run tg password"
        ),
        "ru": (
            "облачный пароль двухэтапной аутентификации не подошёл. Это пароль Telegram (Settings "
            "→ Privacy and Security → Two-Step Verification), а не пароль от почты или Apple ID. "
            "Повторить только ввод пароля: uv run tg password"
        ),
    },
    "login.hint_password_needed": {
        "en": (
            "the account has two-step verification on: only the password is left. Type it yourself "
            "in the terminal: uv run tg password"
        ),
        "ru": (
            "у аккаунта включена двухэтапная аутентификация: остался только пароль. Введи его сам "
            "в терминале: uv run tg password"
        ),
    },
    "login.hint_phone_banned": {
        "en": "this number is banned in Telegram — signing in with it is impossible.",
        "ru": "этот номер заблокирован в Telegram — вход по нему невозможен.",
    },
    "login.hint_phone_invalid": {
        "en": (
            "Telegram did not accept the number. It needs the international format with a plus, "
            "for example +79991234567."
        ),
        "ru": (
            "Telegram не принял номер. Нужен международный формат с плюсом, например +79991234567."
        ),
    },
    "login.need_send_code": {
        "en": "Run tg send-code <phone> first.",
        "ru": "Сначала tg send-code <phone>.",
    },
    "login.need_tty": {
        "en": (
            "A real terminal is required: open a terminal and run there\n  cd {root} && uv run tg "
            "password"
        ),
        "ru": (
            "Нужен настоящий терминал: открой терминал и выполни там\n  cd {root} && uv run tg "
            "password"
        ),
    },
    "login.next_daemon": {
        "en": "Next: uv run tg daemon start",
        "ru": "Дальше: uv run tg daemon start",
    },
    "login.onboarding_title": {
        "en": "What you can do",
        "ru": "Что тебе доступно",
    },
    "login.password_2fa_prompt": {
        "en": "2FA password (hidden input): ",
        "ru": "Пароль 2FA (ввод скрыт): ",
    },
    "login.password_explain": {
        "en": (
            "This is the Telegram cloud password for two-step verification (Settings → Privacy and "
            "Security → Two-Step Verification),\nnot the password of your Apple ID, your mail, or "
            "a code from an SMS.\n"
        ),
        "ru": (
            "Это облачный пароль двухэтапной аутентификации Telegram (Settings → Privacy and "
            "Security → Two-Step Verification),\nа не пароль от Apple ID, почты или код из SMS.\n"
        ),
    },
    "login.password_failed": {
        "en": (
            "\nNot signed in. If the password is forgotten — reset it in the Telegram "
            "app:\nSettings → Privacy and Security → Two-Step Verification → Forgot "
            "password,\nthen repeat: uv run tg send-code <your number>"
        ),
        "ru": (
            "\nНе вошли. Если пароль забыт — сбрось его в приложении Telegram:\nSettings → Privacy "
            "and Security → Two-Step Verification → Forgot password,\nзатем повтори: uv run tg "
            "send-code <твой номер>"
        ),
    },
    "login.password_hint": {
        "en": "Telegram password hint: {hint}",
        "ru": "Подсказка Telegram к паролю: {hint}",
    },
    "login.password_prompt": {
        "en": "Two-factor password (hidden input): ",
        "ru": "Пароль двухфакторной защиты (ввод скрыт): ",
    },
    "login.password_wrong": {
        "en": "Wrong password.",
        "ru": "Пароль неверный.",
    },
    "login.phone_prompt": {
        "en": "Phone in +79991234567 format: ",
        "ru": "Телефон в формате +79991234567: ",
    },
    "login.premium": {
        "en": "Telegram Premium: {value}",
        "ru": "Telegram Premium: {value}",
    },
    "login.premium_no": {
        "en": "no",
        "ru": "нет",
    },
    "login.premium_yes": {
        "en": "yes",
        "ru": "есть",
    },
    "login.session_file": {
        "en": "Session: {path}",
        "ru": "Сессия: {path}",
    },
    "login.signed_in": {
        "en": "Signed in as {name} (@{username}, id {user_id}).",
        "ru": "Вошёл как {name} (@{username}, id {user_id}).",
    },

    # --- reminder ----------------------------------------------------------
    "reminder.due": {
        "en": "due {at}",
        "ru": "срок {at}",
    },
    "reminder.no_reply": {
        "en": ", and no reply from “{chat}” ever came",
        "ru": ", ответа из «{chat}» так и не было",
    },
    "reminder.title": {
        "en": "Reminder",
        "ru": "Напоминание",
    },

    # --- rules -------------------------------------------------------------
    "rules.auto_action": {
        "en": "auto[{i}]: action — one or several of {actions}",
        "ru": "auto[{i}]: action — одно или несколько из {actions}",
    },
    "rules.auto_action_unknown": {
        "en": "; do not know: {unknown}",
        "ru": "; не знаю: {unknown}",
    },
    "rules.auto_folder_needed": {
        "en": "auto[{i}]: the folder action needs a folder in the folder field",
        "ru": "auto[{i}]: действию folder нужна папка в поле folder",
    },
    "rules.auto_no_condition": {
        "en": (
            "auto[{i}]: at least one condition is needed ({conditions}) — a rule without "
            "conditions would fire on every incoming message"
        ),
        "ru": (
            "auto[{i}]: нужно хотя бы одно условие ({conditions}) — правило без условий сработало "
            "бы на каждое входящее"
        ),
    },
    "rules.auto_not_list": {
        "en": "auto: expected a list of rules",
        "ru": "auto: нужен список правил",
    },
    "rules.auto_not_object": {
        "en": "auto[{i}]: a rule is an object, not {kind}",
        "ru": "auto[{i}]: правило — это объект, а не {kind}",
    },
    "rules.auto_type": {
        "en": "auto[{i}]: type — one of {types}",
        "ru": "auto[{i}]: type — одно из {types}",
    },
    "rules.digest_at_format": {
        "en": "digest_at: {value} — expected the format HH:MM",
        "ru": "digest_at: {value} — нужен формат ЧЧ:ММ",
    },
    "rules.digest_at_range": {
        "en": "digest_at: {value} — there is no such time",
        "ru": "digest_at: {value} — такого времени не бывает",
    },

    # --- setup -------------------------------------------------------------
    "setup.add_groq_key": {
        "en": "add GROQ_API_KEY to .env (console.groq.com/keys)",
        "ru": "добавь GROQ_API_KEY в .env (console.groq.com/keys)",
    },
    "setup.add_openai_key": {
        "en": "add OPENAI_API_KEY to .env",
        "ru": "добавь OPENAI_API_KEY в .env",
    },
    "setup.alert_channel_linked": {
        "en": "Alert channel connected.",
        "ru": "Канал алертов подключён.",
    },
    "setup.api_bad_format": {
        "en": "   api_id must be a number, api_hash a long string. Aborted.",
        "ru": "   api_id должен быть числом, api_hash — длинной строкой. Прервано.",
    },
    "setup.api_hash_prompt": {
        "en": "   TG_API_HASH (hidden input): ",
        "ru": "   TG_API_HASH (ввод скрыт): ",
    },
    "setup.api_where": {
        "en": "{head}: https://my.telegram.org → API development tools",
        "ru": "{head}: https://my.telegram.org → API development tools",
    },
    "setup.bot_confirmed": {
        "en": "   Bot confirmed: @{username}",
        "ru": "   Бот подтверждён: @{username}",
    },
    "setup.bot_head": {
        "en": "notification bot",
        "ru": "бот для уведомлений",
    },
    "setup.bot_linked": {
        "en": "   Done: alerts will go to chat_id {chat_id}",
        "ru": "   Готово: алерты пойдут в chat_id {chat_id}",
    },
    "setup.bot_not_started": {
        "en": "   Gave up waiting. Press Start and run `uv run tg link-bot`.",
        "ru": "   Не дождался. Нажми Start и запусти `uv run tg link-bot`.",
    },
    "setup.bot_start": {
        "en": "\n3) Open https://t.me/{username} and press Start (waiting up to 120 seconds)...",
        "ru": "\n3) Открой https://t.me/{username} и нажми Start (жду до 120 секунд)...",
    },
    "setup.bot_token_missing": {
        "en": "TG_BOT_TOKEN is not set — run `uv run tg setup`.",
        "ru": "TG_BOT_TOKEN не задан — запусти `uv run tg setup`.",
    },
    "setup.bot_token_prompt": {
        "en": "   TG_BOT_TOKEN (hidden input, Enter to skip): ",
        "ru": "   TG_BOT_TOKEN (ввод скрыт, Enter чтобы пропустить): ",
    },
    "setup.bot_where": {
        "en": "{head}: @BotFather → /newbot → copy the token",
        "ru": "{head}: @BotFather → /newbot → скопируй токен",
    },
    "setup.env_missing": {
        "en": (
            "{name} is set neither in the environment nor in {env_file}. Run the setup in your own "
            "terminal: cd {root} && uv run tg init"
        ),
        "ru": (
            "{name} не задан ни в окружении, ни в {env_file}. Пройди установку в своём терминале: "
            "cd {root} && uv run tg init"
        ),
    },
    "setup.intro": {
        "en": (
            "Telegram agent setup. Values are written to .env (chmod 600), nothing leaves the "
            "machine.\n"
        ),
        "ru": (
            "Настройка Telegram-агента. Значения пишутся в .env (chmod 600), ничего не уходит "
            "наружу.\n"
        ),
    },
    "setup.no_api_keys": {
        "en": (
            "The app keys (TG_API_ID/TG_API_HASH) are not set — you get them at my.telegram.org. "
            "The whole setup: cd {root} && uv run tg init"
        ),
        "ru": (
            "Ключи приложения (TG_API_ID/TG_API_HASH) не заданы — их берут на my.telegram.org. "
            "Установка целиком: cd {root} && uv run tg init"
        ),
    },
    "setup.no_session": {
        "en": (
            "There is no Telegram session at all. The owner signs in themselves, the agent never "
            "sees the code: {command}"
        ),
        "ru": (
            "Нет ни одной сессии Telegram. Вход делает владелец сам, агент кода не видит: "
            "{command}"
        ),
    },
    "setup.onboarding_title": {
        "en": "What is set up so far",
        "ru": "Что уже настроено",
    },
    "setup.step_bot": {
        "en": "\n2) notification bot",
        "ru": "\n2) бот для уведомлений",
    },
    "setup.written": {
        "en": "\n   Written to {path}",
        "ru": "\n   Записано в {path}",
    },

    # --- status ------------------------------------------------------------
    "status.alert_chat_unlinked": {
        "en": "not linked — tg link-bot",
        "ru": "не привязан — tg link-bot",
    },
    "status.bot_not_configured": {
        "en": "not configured",
        "ru": "не настроен",
    },
    "status.bot_token_set": {
        "en": "token set",
        "ru": "токен задан",
    },
    "status.creds_missing": {
        "en": "MISSING",
        "ru": "НЕТ",
    },
    "status.creds_set": {
        "en": "set",
        "ru": "заданы",
    },
    "status.daemon_no_answer": {
        "en": "The daemon did not answer ({error}).\n\n",
        "ru": "Демон не ответил ({error}).\n\n",
    },
    "status.daemon_not_started": {
        "en": (
            "The daemon is not running — run `uv run tg daemon start`, then `uv run tg "
            "capabilities`.\n\n"
        ),
        "ru": (
            "Демон не запущен — запусти `uv run tg daemon start`, потом `uv run tg "
            "capabilities`.\n\n"
        ),
    },
    "status.daemon_old_code": {
        "en": (
            "The daemon is running an old version of the code — restart it: uv run tg daemon "
            "restart.\n\n"
        ),
        "ru": "Демон работает на старой версии кода — перезапусти: uv run tg daemon restart.\n\n",
    },
    "status.daemon_running": {
        "en": "running (pid {pid})",
        "ru": "работает (pid {pid})",
    },
    "status.daemon_stopped": {
        "en": "not running",
        "ru": "не запущен",
    },
    "status.env_from_environment": {
        "en": "no file, values from the environment",
        "ru": "файла нет, значения из окружения",
    },
    "status.env_missing": {
        "en": "MISSING — run tg setup",
        "ru": "НЕТ — запусти tg setup",
    },
    "status.no": {
        "en": "no",
        "ru": "нет",
    },
    "status.no_accounts": {
        "en": "none",
        "ru": "нет",
    },
    "status.row_accounts": {
        "en": "accounts",
        "ru": "аккаунты",
    },
    "status.row_alert_chat": {
        "en": "alert chat",
        "ru": "chat алертов",
    },
    "status.row_bot": {
        "en": "bot",
        "ru": "бот",
    },
    "status.row_daemon": {
        "en": "daemon",
        "ru": "демон",
    },
    "status.row_default": {
        "en": "default",
        "ru": "по умолчанию",
    },
    "status.row_session": {
        "en": "session",
        "ru": "сессия",
    },
    "status.row_socket": {
        "en": "socket",
        "ru": "сокет",
    },
    "status.row_write": {
        "en": "write",
        "ru": "запись",
    },
    "status.rpc_no_answer": {
        "en": "\nRPC is not answering: {error}",
        "ru": "\nRPC не отвечает: {error}",
    },
    "status.session_missing": {
        "en": "MISSING — run tg login",
        "ru": "НЕТ — запусти tg login",
    },
    "status.session_pending_2fa": {
        "en": "sign-in unfinished — needs tg password (2FA)",
        "ru": "вход не завершён — нужен tg password (2FA)",
    },
    "status.write_allowed": {
        "en": "allowed",
        "ru": "разрешена",
    },
    "status.write_off": {
        "en": "off",
        "ru": "выключена",
    },
    "status.yes": {
        "en": "yes",
        "ru": "есть",
    },
}
