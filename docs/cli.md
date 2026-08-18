# The `tg` command line

`tg` is the owner's side of the agent: setting up, signing in, running the daemon and
looking at the state. It is deliberately not the agent's side — the model talks to the
daemon over MCP, and the commands that ask for a code, a password or a token are meant to
be typed by a human at their own terminal.

Two entry points are declared in `pyproject.toml`: `tg` (`tgagent.cli:main`) is this
command line, `tg-mcp` (`tgagent.mcp_server:main`) is the MCP server, which is started by
the client and not by you — see [mcp.md](mcp.md).

Run it from the project directory as `uv run tg <command>`; inside the container as
`docker compose exec tgagent tg <command>`. Every command exits with 0 on success and 1 on
failure, so they chain in a script. Everything `tg` prints is in the language set by
`TG_LANG` ([configuration.md](configuration.md#interface-language)).

`--account LABEL` appears on most commands: it picks which signed-in account to work with
(`main` by default, e.g. `work`, `second`). The commands that do not take it are the ones
that are not about a particular account: `setup` and `link-bot` write `.env`, `accounts`
lists them all, and the daemon controls (`start`, `run`, `stop`, `restart`, `logs`) act on
the one process that holds every account at once — only `daemon status` takes the flag,
because it is `tg status` under another name.

## At a glance

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
uv run tg daemon start|run|stop|restart|status|logs
uv run tg call dialogs '{"limit": 5}' # poke a daemon method bypassing MCP
uv run tg logout                      # revoke the session and wipe the files
```

## Installing and diagnosing

### `tg init [--account LABEL]`

The setup wizard: walk through everything up to a working state — `api_id`/`api_hash`,
the sign-in, the notification bot, the daemon, registering the MCP server in Claude Code,
the subagents in `~/.claude/agents`, autostart. It implements nothing of its own: it looks
at the state of the installation and calls the commands below, which is why running it
again is safe and works as "repair the installation". The walkthrough and what it asks for
are in [install.md](install.md).

### `tg doctor [--account LABEL]`

Installation diagnostics: what is in place, what is broken, what to do. The project
directory, the Python version, `uv`, permissions on `.env` and `data/`, the keys, the
signed-in accounts, whether the daemon is alive and answers on the socket, whether the MCP
server is registered, whether the subagents match the repository, whether autostart is set
up. Every line where something is wrong comes with the command that closes it. Keys, the
phone number and the account name are deliberately absent from the report, so it can be
attached to an issue as is. It shares its state snapshot with `tg init`, so the two can
never disagree about whether a step is done.

### `tg status [--account LABEL]`

State of the installation, as a table: the signed-in accounts and which one is the
default, whether `.env` exists (or the variables come from the environment, the usual case
in docker), whether `api_id`/`api_hash` are set, whether there is a session, whether the
bot token and the alert chat are configured, whether writing is allowed, whether the
daemon is running and whether the socket is there. If the socket exists, the daemon's own
`status` answer is printed after it as JSON.

### `tg capabilities [--account LABEL]`

What is available and what is missing — the same digest `tg setup` and `tg login` print at
the end. With the daemon running it is the full one: only the server knows the
subscription and Telegram's own caps. Without the daemon the local half is shown (keys,
the bot, the installed extras, the write mode) with an honest note about what is missing
from it.

## Keys and the bot

### `tg setup`

Enter `api_id`/`api_hash` and the bot token. `api_id` is typed in the clear, `api_hash`
and the token through `getpass`, that is, not shown as you type; the shape of the input is
checked before anything is written. The values go into `.env` together with
`TG_ALLOW_WRITE=1`, and the file is left with permissions 600. If a bot token was given,
the command goes straight on to the linking below. It ends with the capabilities digest.

### `tg link-bot`

Link the `chat_id` for alerts. It takes the token from the config, checks it, prints the
bot's `@username` and waits up to two minutes for you to press Start in the chat with it;
the first message that arrives gives up the `chat_id`, which is written to `.env` as
`TG_ALERT_CHAT_ID` and confirmed by a message back into that chat. Without a token in the
config the command refuses.

## Signing in

### `tg login [--account LABEL]`

Sign in to a Telegram account, interactively: the phone number, the code from Telegram
and — if 2FA is on — the cloud password, which is read through `getpass`. The session file
is created with permissions 600 the moment it appears, not at the end. An interrupted
sign-in does not leave a half-made session behind: a file the command created itself is
removed, so nothing later mistakes it for a signed-in account. Ctrl-C and Ctrl-D are
treated as leaving the dialogue, not as a crash. If the account is already authorized it
says so and prints the capabilities digest.

Signing in through `tg init` is better: the wizard turns Telegram's own codes
(`PHONE_CODE_EXPIRED`, `API_ID_INVALID`, `AUTH_KEY_DUPLICATED`, `PHONE_NUMBER_BANNED` and
the rest) into an explanation and the exact next command, while `tg login` on its own
shows the Telethon exception as is.

### `tg send-code PHONE [--account LABEL]`

Sign-in step 1: request the code for that phone number. The phone and the returned
`phone_code_hash` are written to `data/login_state.json` (mode 600, one per account) so
that the next step can be a separate process. If the account is already authorized,
nothing is requested.

### `tg sign-in --code CODE [--password PASSWORD] [--account LABEL]`

Sign-in step 2: confirm the code from step 1. `--code` is required. `--password` is the
2FA password if it is enabled; without the flag, and with 2FA on, the password is asked
for through `getpass`. On success the intermediate state file is removed and the session
gets permissions 600.

### `tg password [--account LABEL]`

Step 3: enter the cloud 2FA password — the tail of a sign-in that stopped at it. It
refuses to run without a real tty, precisely so that the password cannot arrive as an
argument from the agent and settle in its context or in the shell history. It shows the
hint stored on the account, if there is one, and gives three attempts.

### `tg logout [--account LABEL]`

Revoke the session and delete the files. It stops the daemon first, then revokes the
session on Telegram's side and removes `.session` and `.session-journal`. If the account
being removed was the default, the default goes back to `main` rather than staying as a
pointer to nothing. The local index and the chat dossiers are kept and their paths printed
— they survive a re-login, and wiping correspondence silently is not this command's job.

## Accounts

### `tg accounts [--default LABEL]`

Which accounts are signed in: the label and the path to the session file, with the current
default marked, plus the ready-made commands for adding another one and changing the
default. With no accounts at all it prints how to sign in and exits 1.

`--default LABEL` remembers that account as the default for all clients and restarts — it
is written to `data/settings.json`. An account that is not signed in is refused rather
than recorded. Since the daemon holds every account at once, the change needs no restart
of it.

## The daemon

### `tg daemon start`

Start the daemon in the background. Keys and a session are checked first, before the
start: without them the daemon is guaranteed to die, and the check turns eighteen seconds
and a traceback into one line with the command that fixes it. A stale socket file is
removed, the process is detached from the terminal, its output goes to `data/daemon.log`,
and the command waits up to 18 seconds for the socket to appear. If it does not, the tail
of the log is printed. An already running daemon is left alone.

### `tg daemon run`

Run the daemon in the foreground — this is what the container and a launchd/systemd unit
execute.

### `tg daemon stop`

SIGTERM to the pid from `data/daemon.pid`, then up to 6 seconds of waiting for it to go.
Shutdown is graceful: the daemon closes the Telegram client and removes the socket and the
pid file itself. If there is no live daemon, a stale socket file is cleaned up anyway.

### `tg daemon restart`

`stop` followed by `start`. This is the answer to most daemon-shaped problems: new code
after a `git pull`, a lost network connection, a socket left behind by a dead process.

### `tg daemon status [--account LABEL]`

The same as `tg status`. It exists because that is what people type first, and accepting
both forms is cheaper than explaining which one is the wrong one.

### `tg daemon logs [-n N]`

The last `N` lines of `data/daemon.log`, 40 by default. This is the only place a full
traceback stays — the agent gets errors as explanations, not as tracebacks. Look through
it before attaching it to an issue: the account name and id are in it by construction.

## Talking to the daemon directly

### `tg call METHOD [PARAMS] [--account LABEL]`

Call a daemon method directly, bypassing MCP. `PARAMS` is JSON, e.g. `'{"limit": 5}'`; the
result is printed as indented JSON, an error as `Error: ...` with exit code 1. The method
names are the daemon's, not the tools': `dialogs`, `history`, `whoami`, `index`, `memory`,
`events`, `actions` and the rest — a tool `tg_x` calls the method `x`, the one exception
being `tg_resolve`, which calls `resolve_link`.

This is the workbench command. It is what the documentation uses for the operations that
have no separate CLI wrapper:

```bash
uv run tg call dialogs '{"limit": 1}'                      # a live end-to-end check
uv run tg call whoami                                      # RPC only, answers from memory
uv run tg call events '{"limit": 10}'                      # what the watcher saw
uv run tg call actions '{"since": "-24h"}'                 # what the agent did
uv run tg call index '{"action":"drop"}'                   # wipe the local index
uv run tg call memory '{"chat":"Work","action":"drop"}'    # wipe a chat dossier
```

It goes through the same guards as the agent's calls — `TG_ALLOW_WRITE`, the rate limits,
the audit log — because they sit in the core and in the daemon, below whoever is calling.

## See also

- [install.md](install.md) — the install path these commands make up
- [mcp.md](mcp.md) — the other entry point: connecting clients, subagents, diagnostics
- [configuration.md](configuration.md) — everything these commands write into `.env` and `rules.json`
- [troubleshooting.md](troubleshooting.md) — what a failing command usually means
