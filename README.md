# telegram-mcp

An MCP server on top of a **personal** Telegram account: 79 tools, MTProto, not the Bot API.

## What it is

A wrapper around a personal Telegram account that hands it to an agent as a set of MCP
tools. It works over MTProto (Telethon) rather than the Bot API, which is why the whole
account is visible and not only what someone wrote to a bot. This is a personal tool for
one account and one owner, not a service: it keeps a live Telegram session on your machine
and writes to real people in your name.

The difference from wrappers around the Bot API is one of kind, not of degree. A bot sees
only the messages addressed to it, cannot read a conversation with a person, has no
history and does not exist until someone has pressed Start. Here the agent has the same
access you have in the app: every dialog, search across the whole correspondence,
attachments, folders, drafts, sending in your name. The price of that is the
[Risks](#risks) section below, and it has to be read before you start, not after.

## What it can do

The agent does not only read the correspondence, it also **looks** at pictures (`tg_view`
returns the image itself) and **listens** to sound: voice messages, video notes, music and
video are transcribed by Telegram's built-in transcription, through Groq Whisper or by a
local model. Long posts are retold by Telegram itself (`tg_summarize`), stories are read
without leaving a trace, and `tg_wait` and `tg_ask` let the agent wait for the message it
needs or ask the owner for permission right in the bot.

Going through the inbox is not the same as going through the unread: `tg_pending` shows
the conversations that broke off — who was never answered and who never answered, the
read-and-forgotten ones included, which the unread counter no longer knows about.
`tg_person` collects a dossier on a person in one call: profile, flags, shared chats,
place in the top of your correspondents, the history of the DM. `tg_memory` keeps a
standing dossier on a chat, so that an unfamiliar conversation does not have to start with
a thousand messages of history.

The daemon also does what needs no Claude running at all: alerts about important incoming
messages into your own bot, a digest on a schedule (`digest_at`), mail-style inbox filters
(mark read, archive, mute, move to a folder, move to Saved Messages) and reminders that
survive a restart. Auto-replies are deliberately missing from the filter actions: a rule
works unsupervised, and it must not be able to write to an outside person.

For the chats the owner names, a local full-text index is built (`tg_index`, sqlite +
FTS5): `tg_search(engine="local")` then searches instantly and can do what the server-side
search cannot do at all — filter by author, the slice "everything from this person over
this period", ranking by relevance and highlighting of the match.

The full reference is [docs/tools.md](docs/tools.md).

## What is inside

```
MCP client (Claude Code, Claude Desktop, any other)
        │  stdio
        ▼
tgagent.mcp_server ──unix socket──▶ tgagent.daemon ──MTProto──▶ Telegram
     79 tools         /data/daemon.sock      │
                                             ├─ watcher: incoming → filters → alert
                                             ├─ digest on a schedule
                                             ├─ reminders and waiting
                                             └─ Bot API ──▶ your bot ──▶ you
```

The core is `tgagent/core.py`: one class `TelegramService`, every operation on the account
and every guard. Everything else is transport around it. In more detail:
[docs/architecture.md](docs/architecture.md).

## Quick start

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Exactly one thing
holds that bar — `datetime.UTC`, an alias from 3.11; there is nothing from 3.12 or 3.13 in
the code. The system is macOS or Linux: the MCP server talks to the daemon over a unix
socket, so Windows is not supported (under WSL or docker it works).

Any directory will do: the project takes its paths from itself, and every command it
prints already contains the real path to this copy.

```bash
git clone https://github.com/draiqw/telegram-mcp && cd telegram-mcp
uv sync
uv run tg init
```

`tg init` is the wizard that brings the installation to a working state: the application
keys, the sign-in, the notification bot, the daemon, registering the MCP server in Claude
Code and the subagents in `~/.claude/agents`. Every step explains what it is for and what
stops working without it.

Three properties of the wizard are worth knowing in advance:

- **Only `api_id`/`api_hash` and the sign-in are mandatory.** The bot, the model keys,
  local transcription and autostart are skipped with Enter.
- **The code from Telegram and the 2FA cloud password are typed by you.** The wizard does
  not ask for them, does not fill them in and does not store them — it hands that step
  over to `tg login`.
- **Running it again is safe.** The wizard first looks at what is already done and does
  only what is missing, so it also serves as "repair my installation".

What you will need along the way: an application at my.telegram.org → API development
tools (that is where `api_id` and `api_hash` come from; without them only the Bot API is
available, which means your own chats are invisible) and, if you want alerts, a
**separate** bot from @BotFather — an existing one cannot be reused, its messages would
become incoming messages for you and raise an alert about the alert.

At the end the wizard prints `tg capabilities`: what is available, what is blocked and by
what exactly. An installation that already exists is taken apart by `uv run tg doctor` —
what is installed, what is running, where the files are and what permissions they have,
whether the daemon answers, whether MCP is registered, whether the subagents match the
repository. There are no keys, no phone number and no account name in its output, so it
can be attached to an issue in full. If something still does not work after it —
[docs/troubleshooting.md](docs/troubleshooting.md): the common breakages are listed there
as they look from outside.

### If you would rather go step by step

The wizard does nothing on its own — it calls the same commands, and any of them can be
run separately:

```bash
cp .env.example .env && chmod 600 .env
uv run tg setup        # api_id/api_hash and the bot token, typed hidden
uv run tg login        # phone, code from Telegram, cloud password if 2FA
uv run tg link-bot     # press Start in the chat with the bot, the command remembers chat_id
uv run tg daemon start # the daemon owns the session; without it the tools do not work
uv run tg status       # what is configured, what is not, whether the daemon is alive
claude mcp add -s user telegram -- uv --directory "$PWD" run tg-mcp
cp agents/*.md ~/.claude/agents/
```

Next is [docs/mcp.md](docs/mcp.md): scope, Claude Desktop, the ready-made subagents,
diagnostics. The settings for alerts, filters and limits are in
[docs/configuration.md](docs/configuration.md).

### Docker

```bash
cp .env.example .env && chmod 600 .env   # fill in TG_API_ID / TG_API_HASH / TG_BOT_TOKEN
docker compose build
docker compose run --rm tgagent tg login # sign in interactively, the session lands in ./data
docker compose up -d
claude mcp add telegram -- docker exec -i tgagent tg-mcp
```

The details, including why MCP is started inside the container and not on the host, are in
[docs/docker.md](docs/docker.md).

## What it costs

The agent itself is free, and in its basic form there is nobody to pay: MTProto, the
notification bot, server-side search, the local index, alerts, the digest, filters and
reminders cost nothing. A bill can appear in exactly two places, and both of them need a
key that is not there by default:

- **Chat dossiers** (`tg_memory`) go to an external model and are billed per token — by
  default `gpt-4o-mini` under the `OPENAI_API_KEY` key. This is the only thing that spends
  money by itself, with no Claude running, and that is why it is limited three times over:
  without a key the tool refuses, auto-refresh is off, and once it is on it runs into a cap
  per hour (`memory_max_per_hour`, 10 by default). `TG_MEMORY_BASE_URL` takes the calls to
  any compatible service, a local one included — free then.
- **Audio transcription** (`tg_transcribe`) — three engines at three different prices. The
  one built into Telegram is computed on its servers and is genuinely available with
  Premium (without a subscription Telegram gives a small free quota). Groq's free tier is
  limited by the number of requests, above it there is a paid plan. The local model costs
  no money at all: its price is a gigabyte and a half of weights and the time it takes to
  run.

Claude's own tokens do not belong here — they are counted by your client, not by the
agent. The keys and the caps are in [docs/configuration.md](docs/configuration.md).

## Risks

Read this before you start, not after. In full: [SECURITY.md](SECURITY.md) and
[docs/security.md](docs/security.md).

- **`data/session.session` is a sign-in to the account without a password and without
  2FA.** A copy of that file equals a stolen account. It is closed off by `.gitignore` and
  `.dockerignore`, but backups and syncing the directory to a cloud are on you.
- **The agent writes to real people.** With `TG_ALLOW_WRITE=1` it sends messages in your
  name, and the recipient does not know that it was not you.
- **The local index and the dossiers put the correspondence on disk**, and refreshing a
  dossier sends it to an external model. Neither of the two turns itself on: the chat has
  to be named explicitly, and every such call lands in the audit log.
- **Prompt injection** is an open problem. Other people's messages are declared to be data
  in the subagents' prompts and are never interpreted by the code, but that is not treated
  as a guarantee: what stands behind it is the limits, the audit log and the cut-down set
  of tools the cheap watcher gets.
- **There is a second person in the chat** who signed up for none of this.

## Guards

- 60 messages per hour, at most 15 different chats per hour (anti-broadcast), 50 deletions per hour
- `TG_ALLOW_WRITE=0` turns writing off completely
- `confirm_writes` — the middle mode: every writing action asks the owner in the bot, and
  silence counts as a refusal. It is edited in the file only: the agent must not be able
  to lift a restriction off itself
- every writing action is written to `data/actions.jsonl` and read back by `tg_actions`
- inbox filters cannot send anything to real people: the list of actions is closed
- an ambiguous chat name is not guessed: the tool returns a list of candidates
- a FloodWait from Telegram comes back as a clear error, not as a crash

## Commands

```bash
uv run tg init                        # the install wizard, also "repair the install"
uv run tg doctor                      # diagnostics: what is installed, what is broken, what to do
uv run tg status                      # what is configured, what is not, the state of the daemon
uv run tg capabilities                # what is available, what is not and what to do about it
uv run tg setup                       # the keys and the bot token
uv run tg login                       # the whole sign-in
uv run tg send-code +7XXXXXXXXXX      # the same in three steps, without the interactive part
uv run tg sign-in --code 12345
uv run tg password                    # the 2FA cloud password, only from a live tty
uv run tg link-bot                    # link the chat_id for alerts
uv run tg accounts                    # which accounts are signed in and which one is default
uv run tg login --account work        # add a second account
uv run tg accounts --default work     # change the default account for good
uv sync --extra local-whisper         # local audio transcription (optional)
uv run tg daemon start|run|stop|restart|logs
uv run tg call dialogs '{"limit": 5}' # poke a daemon method bypassing MCP
uv run tg logout                      # revoke the session and wipe the files
```

## Documentation

| File | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | the core, the layers, the invariants, the flow of data, what lives where |
| [docs/tools.md](docs/tools.md) | a reference for every MCP tool with its parameters |
| [docs/configuration.md](docs/configuration.md) | environment variables, the three write modes, alert rules, the digest, inbox filters, several accounts, limits |
| [docs/mcp.md](docs/mcp.md) | connecting as an MCP server, the subagents, diagnostics |
| [docs/troubleshooting.md](docs/troubleshooting.md) | what to do when it does not work: `tg doctor`, common breakages, where to look |
| [docs/docker.md](docs/docker.md) | build, sign-in inside the container, updating, backup |
| [docs/security.md](docs/security.md) | the threat model: what is protected, what is not, how to revoke access |

## Contributing and license

Patches are welcome — how to bring up the environment, what to run before a PR and why a
feature is added in three places at once are written up in
[CONTRIBUTING.md](CONTRIBUTING.md). For vulnerabilities see [SECURITY.md](SECURITY.md);
there is no need to open a public issue.

[MIT](LICENSE), © 2026 Roman Akramov.
