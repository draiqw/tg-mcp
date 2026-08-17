# Architecture

## Where the core is

`tgagent/core.py`, ~4300 lines, class `TelegramService`. This is the only place where
the code talks to Telegram. Everything else is transport and scaffolding:

| File | Lines | Role |
|---|---|---|
| **`tgagent/core.py`** | 4385 | **core: all account operations, chat resolution, limits** |
| `tgagent/daemon.py` | 1655 | owner of all sessions, RPC over a unix socket, watcher, filters, digest, waiting, reminders, bot channel |
| `tgagent/mcp_server.py` | 1410 | 76 tools, each one a single call to the daemon |
| `tgagent/index.py` | 669 | local index of the correspondence: sqlite + FTS5, Russian morphology |
| `tgagent/cli.py` | 520 | setup, sign-in, daemon control |
| `tgagent/config.py` | 305 | paths, `.env`, rules, limits |
| `tgagent/alerts.py` | 125 | Bot API: alerts, commands, buttons under the agent's questions |

The rule is simple: a new capability goes into `core.py` as a `TelegramService`
method, gets registered in the daemon's `dispatch_table()` and is wrapped by a
tool in `mcp_server.py`. Three lines on three levels, the logic — only in the core.
`scripts/selfcheck.py` watches that these three levels do not drift apart.

`index.py` does not break the rule: it does not talk to Telegram and does not know
about Telethon at all. It is storage — sqlite, FTS5, a stemmer — used by the
`core.index()` method, exactly as `alerts.py` knows the Bot API and `config.py` knows
about paths. The logic of "what to download, how far and when to stop" stayed in the
core.

The exception is four tools that live in the daemon rather than in the core: `tg_wait`
waits for an incoming message, `tg_ask` — for the owner's answer, `tg_remind` defers a
reminder, `tg_actions` reads the action log. None of them needs a call to Telegram —
they need the event stream, the bot channel and the daemon's files, which is exactly
what the daemon owns.

For the same reason two things live in the daemon that have no tool at all:
inbox filters and the scheduled digest. They cannot be made into a tool — they work
when Claude is not running, and they are
configured by rules in `rules.json` through `tg_rules`. The filters' actions themselves
still go through ordinary core methods.

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

A writing method gets two extra steps in `handle_call`: `confirm_write()` before the
call (if `confirm_writes` is on — a question to the owner in the bot, a refusal and
silence both become an error of the call) and `append_action()` after it — a line in
`actions.jsonl`, no matter whether the call succeeded or not. Both stand in the daemon,
not in the core: the core knows nothing about the bot and must not know.

Errors are classified in the daemon: `GuardError` and `ValueError` (limit exceeded,
chat not found, ambiguous name) are returned as 400 with human-readable text,
everything else as 500 with the exception type. The agent sees "Send limit reached (60/hour)",
not a traceback.

**An incoming message.**

```
Telegram → watcher (events.NewMessage)
  → daemon.run_auto_rules(ev) — filters: condition → safe action
  → daemon.alert_reason(ev) — alert rules
  → write to data/events.jsonl (everything, unfiltered)
  → if it fired: Bot API → your bot → notification on the phone
```

`events.jsonl` is always written, an alert only by the rules. So the agent can ask
`tg_events` after the fact and see what it was not woken up for.

The filters (`rules.json`, the `auto` section) stand in this chain **before** the
alerts, and the order here carries meaning, it is not accidental: a filter that fired
suppresses the alert for the same message by default. Otherwise the owner would be
woken up about a chat that went into the archive at that very moment. The alert can be
brought back with the `alert` flag in the rule itself. A filter's actions are only
reversible ones and only inside its own account (`mark_read`, `archive`, `mute`,
`folder_edit`, `forward` to Saved Messages); they go through ordinary core methods, so
they fall under `_assert_write`, `RateGuard` and into `actions.jsonl` marked `auto`.
Details are in [configuration.md](configuration.md#inbox-filters).

**Digest.** `digest_loop()` ticks once every half minute and at the time set by the
schedule (`digest_at`) collects the period's digest from the same `events.jsonl` and
sends it through the bot. The last processed deadline lies in `data/digest.json`, so a
restart of the daemon does not lead to a second digest for the same deadline, and a
period missed because of a pause or quiet hours is not lost — it will land in the next
digest. An empty digest is not sent.

**A command from the phone.** The bot is polled with long-poll in `bot_loop()`;
commands are accepted only from the chat `TG_ALERT_CHAT_ID`, messages from any other
chat are logged and ignored.

**A reminder.** `reminder_loop()` ticks once every half minute and sends through the
bot everything whose time has come; the queue lies in `data/reminders.json`, so it
survives a restart. A reminder with `unless_reply` is cancelled in `feed_waiters()` —
in the same place where those waiting on `tg_wait` are woken: it is one and the same
incoming stream, and there is no point in listening to it a second time.

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
7. **The agent does not lift its own restrictions.** The `confirm_*` keys do not pass
   through `set_rules` (an explicit error, not a silent skip), and `save_rules` takes
   them from disk rather than from the dictionary it was handed: otherwise saving alert
   rules on top of a stale copy in the daemon's memory would switch off the confirmation
   mode by itself. The limits (`LIMITS`) are edited only by file for the same reason.
8. **Inbox filters cannot write to outsiders.** The list of actions is closed
   (`config.AUTO_ACTIONS`), it is checked on save, and the addressee for `save` is wired
   into the code — Saved Messages. A rule works without a human, so the price of a
   mistake in its condition must not include a message to a living person.

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
that the watcher did not get a single destructive tool, and that the "N tools" numbers
in the documentation do not lie.

A second layer is laid on top of that — what lives not in the three layers but next to
them, and therefore drifts apart especially quietly:

- the table of files on this page: both the set of modules and the number of lines in
  each. The line count reads as a fact, and it changes with any edit of the code;
- every key of `config.DEFAULT_RULES` is described in `docs/configuration.md`. A new
  rule that exists only in the code is one nobody can configure;
- the `CONFIRM_KEYS` have default values, `CONFIRM_OUTBOUND` consists of writing
  methods, and everything writing plus `AUDIT_ONLY` is in `dispatch_table`;
- the filters' actions (`AUTO_ACTIONS`) and the methods that are silent in `outgoing`
  mode are listed in the documentation by name.

The script stays a static analysis: it reads the sources through `ast` instead of
importing them, so it does not pull in Telethon and works without a running daemon.

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
| `actions.jsonl` | the audit: what the agent sent, deleted, changed; read through `tg_actions` |
| `reminders.json` | the active `tg_remind` reminders, survive a restart |
| `digest.json` | the last processed digest deadline and the start of the period for the next one |
| `index.db` | the local index of the correspondence (`tg_index`), mode 600. Created only by an explicit command of the owner, removed by `action="drop"` |
| `index-<label>.db` | the same for the second, third and so on account |
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
