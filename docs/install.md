# Installing

From an empty directory to a working agent. The short path is three commands and a
wizard; everything conditional — a manual sign-in, a second account, docker, autostart
— is a branch off it.

## What you need

Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). Exactly one thing holds that
bar — `datetime.UTC`, an alias from 3.11; there is nothing from 3.12 or 3.13 in the code.

The system is macOS or Linux: the MCP server talks to the daemon over a unix socket, so
Windows is not supported (under WSL or docker it works).

Any directory will do: the project takes its paths from itself, and every command it
prints already contains the real path to this copy.

## The short path

```bash
git clone https://github.com/draiqw/tg-mcp && cd tg-mcp
uv sync
uv run tg init
```

`tg init` is the wizard that brings the installation to a working state: the application
keys, the sign-in, the notification bot, the daemon, registering the MCP server in Claude
Code and the subagents in `~/.claude/agents`. Every step explains what it is for and what
stops working without it.

## Without a clone

The same thing is installable as a package, if you would rather have `tg` on PATH than a
directory to `cd` into:

```bash
uv tool install --from git+https://github.com/draiqw/tg-mcp tgagent
tg init
```

It is installed from git rather than from PyPI, where the package is not published. Two
differences follow, and both are worth knowing before you sign in:

- **The state lives in `~/.tgagent`** — `.env` and `data/` with the session, the index and
  the dossiers. A clone keeps them in the clone; a package cannot, because "next to the
  code" means inside `site-packages`, which the next upgrade rewrites. Both locations are
  still overridable with `TG_ENV_FILE` and `TG_DATA_DIR`.
- **The commands lose their `uv run`.** Everything below is written for a clone; installed,
  drop the prefix — `tg login`, `tg daemon start`. The hints the program prints already
  know which of the two you have and spell themselves accordingly.

Updating is the same command again with `--force`; `~/.tgagent` is untouched by it, so the
sign-in survives.

Three properties of the wizard are worth knowing in advance:

- **Only `api_id`/`api_hash` and the sign-in are mandatory.** The bot, the model keys,
  local transcription and autostart are skipped with Enter.
- **The code from Telegram and the 2FA cloud password are typed by you.** The wizard does
  not ask for them, does not fill them in and does not store them — it hands that step
  over to `tg login`.
- **Running it again is safe.** The wizard first looks at what is already done and does
  only what is missing, so it also serves as "repair my installation".

## What you will need along the way

An application at my.telegram.org → API development tools (that is where `api_id` and
`api_hash` come from; without them only the Bot API is available, which means your own
chats are invisible) and, if you want alerts, a **separate** bot from @BotFather — an
existing one cannot be reused, its messages would become incoming messages for you and
raise an alert about the alert.

## If you would rather go step by step

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

Every one of these commands, with its flags and what it writes to disk, is in
[cli.md](cli.md). Next is [mcp.md](mcp.md): scope, Claude Desktop, the ready-made
subagents, diagnostics. The settings for alerts, filters and limits are in
[configuration.md](configuration.md).

## Optional extras

Local audio transcription is installed separately, because it drags a gigabyte and a half
of weights along with it:

```bash
uv sync --extra local-whisper
```

Installed as a package there is no project to sync, and the extra cannot be named either
— `tgagent[local-whisper]` would send uv looking for the package on PyPI. The two engines
behind the extra are asked for directly instead (drop `mlx-whisper` outside Apple
Silicon):

```bash
uv tool install --force --from git+https://github.com/draiqw/tg-mcp \
    --with mlx-whisper --with faster-whisper tgagent
```

`tg doctor` prints whichever of the two lines applies to you.

The engines, their keys and how the choice between them is made are in
[configuration.md](configuration.md#audio-transcription). Autostart after a reboot
(launchd on macOS, systemd on Linux) is offered by the wizard and described in
[configuration.md](configuration.md#autostart).

## Docker

The container holds the daemon, and the MCP server is started inside the same container
rather than on the host. Build, interactive sign-in, connecting a client, updating and
backup are all in [docker.md](docker.md) — including why the socket does not leave the
container.

## When the install is done

At the end the wizard prints `tg capabilities`: what is available, what is blocked and by
what exactly. An installation that already exists is taken apart by `uv run tg doctor` —
what is installed, what is running, where the files are and what permissions they have,
whether the daemon answers, whether MCP is registered, whether the subagents match the
repository. There are no keys, no phone number and no account name in its output, so it
can be attached to an issue in full. If something still does not work after it —
[troubleshooting.md](troubleshooting.md): the common breakages are listed there as they
look from outside.

One setting is worth knowing this early: `TG_LANG` in `.env` (`en` by default, `ru` also
available) picks the language of everything addressed to you — the setup hints, the `tg
init` wizard, `tg doctor`, everything `tg` prints, and the alerts, digests, reminders and
questions delivered through the bot. Logs, errors returned to Claude and all documentation
stay English; the value is read on every call, so editing `.env` needs no restart, and an
unsupported language falls back to `en`.
