# tg-agent

Full access to a personal Telegram account for Claude Code and any other
MCP client: reading every chat and the structure of the account, search across the whole
correspondence, attachments, sending in your own name, drafts and scheduled messages, reactions,
polls, stickers, pressing buttons on bots, managing groups and forums,
several accounts at once and warnings through a separate bot.

The agent does not only read the correspondence, it also **looks** at pictures (`tg_view` returns the
image itself) and **listens** to sound: voice messages, video notes, music and video are transcribed
into text by Telegram's built-in transcription, through Groq Whisper or by a local model.
Long posts are retold by Telegram itself (`tg_summarize`), stories are read without
leaving a trace, and `tg_wait` and `tg_ask` let the agent wait for the message it needs
or ask the owner for permission right in the bot. `tg_remind` puts off
a reminder for hours ahead — a conditional one included, "if she still has not answered" —
and `tg_actions` shows the log of what the agent has already done.

Going through the inbox is not the same as going through the unread: `tg_pending` shows
the conversations that broke off — who was never answered and who never answered, the
read-and-forgotten ones included, which the unread counter no longer knows about.
`tg_person` collects a dossier on a person in one call instead of five: profile, flags, shared chats,
place in the top of your correspondents and the history of the DM.

The daemon also does what needs no Claude running at all: a digest into the bot on a schedule
(`digest_at`) and mail-style inbox filters (the `auto` section — a condition turns into
mark read, archive, mute, move to a folder or forward to Saved
Messages). Auto-replies are deliberately missing from the actions: a rule works
unsupervised, and it must not be able to write to an outside person.

For the chats the owner names, a local full-text index can be built
(`tg_index`, sqlite + FTS5): `tg_search(engine="local")` then searches instantly and can do
what the server-side search cannot do at all — filter by author, the slice "everything from
this person over this period", ranking by relevance and highlighting of the match. On its
own not a single chat gets indexed, the file sits with mode 600 and is wiped in one
call — see [docs/security.md](docs/security.md).

It works over MTProto (Telethon) rather than the Bot API — which is why the whole account is visible,
and not only the messages written to a bot.

## What is inside

```
MCP client (Claude Code, Claude Desktop, any other)
        │  stdio
        ▼
tgagent.mcp_server ──unix socket──▶ tgagent.daemon ──MTProto──▶ Telegram
     76 tools         /data/daemon.sock      │
                                             ├─ watcher: incoming → filters → alert
                                             ├─ digest on a schedule
                                             ├─ reminders and waiting
                                             └─ Bot API ──▶ your bot ──▶ you
```

The core is `tgagent/core.py`: one class `TelegramService`, every operation on the account
and every guard. Everything else is transport around it. In more detail:
[docs/architecture.md](docs/architecture.md).

## Quick start (locally)

```bash
git clone <repo> ~/tg-agent && cd ~/tg-agent
cp .env.example .env && chmod 600 .env

uv sync
uv run tg setup     # api_id/api_hash from my.telegram.org + a bot token
uv run tg login     # phone, code, with 2FA the cloud password
uv run tg daemon start
uv run tg status
```

Connecting to Claude Code:

```bash
claude mcp add telegram -- uv --directory ~/tg-agent run tg-mcp
```

Next is [docs/mcp.md](docs/mcp.md): scope, Claude Desktop, the ready-made
subagents, checking that everything came up.

## Quick start (docker)

```bash
cp .env.example .env && chmod 600 .env   # fill in TG_API_ID / TG_API_HASH / TG_BOT_TOKEN
docker compose build
docker compose run --rm tgagent tg login # sign in interactively, the session lands in ./data
docker compose up -d
```

The MCP client connects to the same container:

```bash
claude mcp add telegram -- docker exec -i tgagent tg-mcp
```

The details, including why MCP is started inside the container and not on the host, are in
[docs/docker.md](docs/docker.md).

## Documentation

| File | What it covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | the core, the layers, the invariants, the flow of data, what lives where |
| [docs/tools.md](docs/tools.md) | a reference for all 76 MCP tools with their parameters |
| [docs/configuration.md](docs/configuration.md) | environment variables, the three write modes, alert rules, the digest, inbox filters, limits |
| [docs/mcp.md](docs/mcp.md) | connecting as an MCP server, the subagents, diagnostics |
| [docs/docker.md](docs/docker.md) | build, sign-in inside the container, updating, backup |
| [docs/security.md](docs/security.md) | the threat model: what is protected, what is not, how to revoke access |

## Commands

```bash
uv run tg status                      # what is configured, what is not, the state of the daemon
uv run tg setup                       # the keys and the bot token
uv run tg login                       # the whole sign-in
uv run tg send-code +7XXXXXXXXXX      # the same in three steps, without the interactive part
uv run tg sign-in --code 12345
uv run tg password                    # the 2FA cloud password, only from a live tty
uv run tg link-bot                    # link the chat_id for alerts
uv run tg accounts                    # which accounts are signed in
uv sync --extra local-whisper         # local audio transcription (optional)
uv run tg login --account work        # add a second account
uv run tg daemon start|run|stop|restart|logs
uv run tg call dialogs '{"limit": 5}' # poke a daemon method bypassing MCP
uv run tg logout                      # revoke the session and wipe the files
```

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
- the contents of other people's messages in the subagents' prompts are declared to be data, not commands
