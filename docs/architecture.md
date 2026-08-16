# Architecture

## Where the core is

`tgagent/core.py`, ~2700 lines, class `TelegramService`. This is the only place where
the code talks to Telegram. Everything else is transport and scaffolding:

| File | Lines | Role |
|---|---|---|
| **`tgagent/core.py`** | 3161 | **core: all account operations, chat resolution, limits** |
| `tgagent/daemon.py` | 723 | owner of all sessions, RPC over a unix socket, watcher, waiting, bot channel |
| `tgagent/mcp_server.py` | 1163 | 70 tools, each one a single call to the daemon |
| `tgagent/cli.py` | 517 | setup, sign-in, daemon control |
| `tgagent/config.py` | 174 | paths, `.env`, rules, limits |
| `tgagent/alerts.py` | 111 | Bot API: alerts, commands, buttons under the agent's questions |

The rule is simple: a new capability goes into `core.py` as a `TelegramService`
method, gets registered in the daemon's `dispatch_table()` and is wrapped by a
tool in `mcp_server.py`. Three lines on three levels, the logic — only in the core.
`scripts/selfcheck.py` watches that these three levels do not drift apart.

The exception is two tools that live in the daemon rather than in the core: `tg_wait`
waits for an incoming message, and `tg_ask` — for the owner's answer. What both of them
need is not a call to Telegram but access to the event stream and to the bot channel,
which is exactly what the daemon owns.

## Why the daemon is a separate process

A Telethon session is an sqlite file, and it does not tolerate two writers. If the MCP
server held the session itself, two simultaneous Claude Code sessions (or Claude Code
plus Claude Desktop) would tear the file apart and knock the account into a sign-out.

So the session is owned by exactly one process — the daemon. If there are several
accounts, it holds them all: one client per label, but still one owner per session
file. The account is chosen by the `account` field in the RPC envelope; without it the
main one is taken. Everyone else goes to the daemon over the unix socket
`data/daemon.sock` with mode 600. A socket, not a TCP port on localhost: a port is
visible to any process of the user, a socket only to whoever has rights on the file,
and it does not show up in `lsof -i` or in port scans.

The second consequence: the daemon lives permanently, which means it has a place to
keep the inbox watcher. The MCP server starts and dies together with the client, a
watcher cannot live like that.

## Data flow

**A request from the agent.**

```
tool tg_history
  → mcp_server.call("history", chat=..., limit=...)
  → POST http://tg/call {"method": "history", "params": {...}} over the unix socket
  → daemon.handle_call → dispatch_table["history"]
  → core.TelegramService.history → Telethon → Telegram
  ← JSON back down the same chain
```

Errors are classified in the daemon: `GuardError` and `ValueError` (limit exceeded,
chat not found, ambiguous name) are returned as 400 with human-readable text,
everything else as 500 with the exception type. The agent sees "Send limit reached (60/hour)",
not a traceback.

**An incoming message.**

```
Telegram → watcher (events.NewMessage)
  → write to data/events.jsonl (everything, unfiltered)
  → daemon.alert_reason(ev) — rules
  → if it fired: Bot API → your bot → notification on the phone
```

`events.jsonl` is always written, an alert only by the rules. So the agent can ask
`tg_events` after the fact and see what it was not woken up for.

**A command from the phone.** The bot is polled with long-poll in `bot_loop()`;
commands are accepted only from the chat `TG_ALERT_CHAT_ID`, messages from any other
chat are logged and ignored.

## Invariants that must not be broken

1. **One writer of the session.** Never open `data/session.session` from a second
   process. Running Telethon past the daemon — only after `tg daemon stop`.
2. **Protection against an alert loop.** An alert goes out through the bot, and the bot
   writes into the same account — that is, the alert comes back as an incoming message.
   `alert_reason()` hard-ignores messages from its own bot (the id is taken from the
   token). This is not a setting, and must not become one: turning it on would loop the
   agent up to a FloodWait.
3. **Pinned dialogs leak between folders.** Telethon returns pinned chats in answer to
   `iter_dialogs(archived=...)` regardless of the requested folder. They have to be
   separated by the dialog's own flag (`d.archived`), otherwise the archive and the main
   list overlap. On an account where almost everything has gone into the archive
   (hundreds of chats against dozens in the main list), a mistake here means blindness
   to almost everything.
4. **Writing goes only through `_assert_write()` and `RateGuard`.** No direct
   `client.send_message` bypassing the core.
5. **Ambiguity is not resolved by guessing.** On several matches by name `resolve()`
   raises an error with the list of candidates and their ids.
6. **The limit counter is one per process, not per account.** `RateGuard` lies in
   `TelegramService._shared_guard`: otherwise the guarantee of "it will not spam your
   contacts" could be worked around by signing in a second session.

## Integrity check

Two scripts, both safe for the account:

```bash
uv run python scripts/selfcheck.py   # static analysis, no daemon needed
uv run python scripts/smoke.py       # a live run of the reading methods
```

`selfcheck.py` reconciles four lists that drift apart easily: the MCP tools, the
methods of the daemon's `dispatch_table`, the methods of `TelegramService` and the
descriptions in `docs/tools.md`. Along the way it checks that both subagents list only
existing tools, that the installed copies in `~/.claude/agents` match the repository,
that the watcher did not get a single writing tool, and that the "N tools" numbers
in the documentation do not lie.

`smoke.py` pulls all the reading methods through the daemon on a live account,
including `view` (a real picture) and `transcribe` (a real voice message found in the
recent chats). It sends nothing and changes nothing.

## State on disk

Everything in `data/` (or in `TG_DATA_DIR`):

| File | What |
|---|---|
| `session.session` | the Telegram session. This is access to the account without a password and 2FA |
| `session-<label>.session` | the second, third and so on account |
| `daemon.sock` | the RPC socket, mode 600, recreated at start |
| `daemon.pid` | the pid of the live daemon, `tg daemon stop` works by it |
| `rules.json` | the alert rules, survive a restart |
| `events.jsonl` | everything incoming that the watcher saw. Rotated at 20 MB |
| `actions.jsonl` | the audit: what the agent sent, deleted, changed |
| `daemon.log` | the daemon's log (in docker — the ordinary container log) |
| `downloads/` | attachments land here by default |
| `login_state.json` | temporary, between `send-code` and `sign-in` |

## Trust boundaries

The contents of other people's messages are untrusted input. They pass through the core
as data and are never interpreted by the code. The protection at the model level is in
the subagents' prompts (`agents/*.md`): instructions found inside the correspondence are
not executed. Verified with a planted injection: the agent read it, did not execute it,
reported to the owner; there is no trace of the action in `actions.jsonl`.

The second barrier is the tool set. The watcher on Haiku (`telegram-watch`) physically
has no `tg_delete`, `tg_edit`, `tg_forward` and `tg_pin`: they are not listed in its
frontmatter, rather than merely forbidden by the text of the prompt.
