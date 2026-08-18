# tg-mcp

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

The agent reads the correspondence, **looks** at pictures and **listens** to sound —
voice messages, video notes, music and video are transcribed by Telegram itself, by Groq
Whisper or by a local model. It can wait for a message, ask you for permission in your own
bot, show the conversations that broke off rather than the merely unread ones, collect a
dossier on a person or keep a standing one on a chat, and search a local full-text index
built for the chats you name. The daemon carries on when Claude is not running: alerts
about important incoming messages, a digest on a schedule, mail-style inbox filters and
reminders that survive a restart. All 79 tools, one by one, are in
[docs/tools.md](docs/tools.md).

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

Against all of that stand the guards, and they sit in the code rather than in a prompt: a
read-only mode, a mode where every write asks you in the bot, caps of 60 messages and 15
different chats per hour, an audit log of everything the agent sent —
[the full list](docs/security.md#what-is-protected-in-the-code).

## Quick start

Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), macOS or Linux.

```bash
git clone https://github.com/draiqw/tg-mcp && cd tg-mcp
uv sync
uv run tg init
```

`tg init` is the wizard that walks the installation to a working state: the application
keys, the sign-in, the notification bot, the daemon, registering the MCP server in Claude
Code and the subagents in `~/.claude/agents`. Only `api_id`/`api_hash` and the sign-in are
mandatory, the code from Telegram and the 2FA password are typed by you, and running it
again is safe — it does only what is missing, so it doubles as "repair my installation".
It ends by printing `uv run tg capabilities`: what is available, what is blocked and by
what exactly.

The manual path, the docker path and what to get ready in advance are in
[docs/install.md](docs/install.md). When something does not work, start with
`uv run tg doctor` and [docs/troubleshooting.md](docs/troubleshooting.md).

## Documentation

| File | What it covers |
|---|---|
| [docs/install.md](docs/install.md) | requirements, the wizard, the step-by-step path, optional extras |
| [docs/cli.md](docs/cli.md) | the `tg` command line: every subcommand, its flags and what it writes |
| [docs/mcp.md](docs/mcp.md) | connecting as an MCP server, the subagents, diagnostics |
| [docs/tools.md](docs/tools.md) | a reference for every MCP tool with its parameters |
| [docs/configuration.md](docs/configuration.md) | environment variables, the three write modes, alert rules, the digest, inbox filters, several accounts, limits, what it costs |
| [docs/docker.md](docs/docker.md) | build, sign-in inside the container, updating, backup |
| [docs/security.md](docs/security.md) | the threat model: what is protected, what is not, how to revoke access |
| [docs/architecture.md](docs/architecture.md) | the core, the layers, the invariants, the flow of data, what lives where |
| [docs/troubleshooting.md](docs/troubleshooting.md) | what to do when it does not work: `tg doctor`, common breakages, where to look |
| [docs/release.md](docs/release.md) | what to check before publishing a version |

## Contributing and license

Patches are welcome — how to bring up the environment, what to run before a PR and why a
feature is added in three places at once are written up in
[CONTRIBUTING.md](CONTRIBUTING.md). For vulnerabilities see [SECURITY.md](SECURITY.md);
there is no need to open a public issue.

[MIT](LICENSE), © 2026 Roman Akramov.
