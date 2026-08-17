# When something does not work

## Start with `tg doctor`

```bash
uv run tg doctor
```

It walks the whole installation: the project directory, the Python version, `uv`,
permissions on `.env` and `data/`, the keys, the signed-in accounts, whether the
daemon is alive, whether it answers over the socket, whether the MCP server is
registered in Claude Code, whether the subagents match the repository, whether
autostart is set up. Every line where something is wrong comes with the command
that closes it.

Keys, the phone number and the account name are deliberately absent from the
report — you can attach it to an issue as is. Most of its remarks are closed by
`uv run tg init`: the wizard looks at the same state and does only what is
missing, so running it again is safe and works as "repair the installation".

A one-line live check is `uv run tg call dialogs '{"limit": 1}'`: it goes the
whole way through, from the CLI over the socket to Telegram and back. That is
exactly why it is not `whoami`: that one answers from the daemon's memory and on
a broken connection will still say "all good".

What `tg doctor` does not see: it asks the daemon about its own state and does
not go to Telegram. A daemon that answers over the socket but has lost its
network connection looks healthy to it. That case is the first row of the table
below.

## Common breakages

| What you see | What is happening | What to do |
|---|---|---|
| `ConnectionError: Cannot send requests while disconnected` | the daemon is alive and answers over RPC, but its Telethon client dropped off the network (machine sleep, network change, disconnect). `tg status` and `tg doctor` are green meanwhile | `uv run tg daemon restart` |
| `The Telegram agent is not set up yet, so the tools do not work` | no keys or no session at all — the installation was not finished | `uv run tg init`; the sign-in is still done by the owner by hand |
| `The Telegram daemon does not answer` | the daemon is not up | `uv run tg daemon start`, then `uv run tg daemon logs`; in docker — `docker compose up -d` |
| `Connection refused` with `data/daemon.sock` present | the daemon died, the socket file stayed | `uv run tg daemon restart` |
| `the daemon does not know method '...'` | after `git pull` the daemon keeps running the old code | `uv run tg daemon restart` |
| no `tg_*` at all in Claude, though `claude mcp list` shows `telegram` | the client session started before the installation: MCP servers and subagents are read once at startup | restart Claude Code |
| `✘ Failed to connect` in `claude mcp list` | the client cannot start the server command | run it by hand: `uv --directory /absolute/path/to/tg-mcp run tg-mcp` — it must wait for input, not exit |
| Claude Desktop does not find `uv` | Desktop starts the server with a stripped-down PATH | give the absolute path to `uv` (`command -v uv`) and restart the app |
| a writing tool returns an error | `TG_ALLOW_WRITE=0`, `confirm_writes` on with no answer from the owner, or a limit was hit | `uv run tg capabilities` names the reason; limits and modes are in [configuration.md](configuration.md) |
| `Chats matching '...': N` | the name matched several chats; the agent does not guess | take the `id` from the list of candidates in the error itself |
| `Send guard` / `Bulk send guard` | 60 messages or 15 different chats per hour — this is the agent's own cap, not Telegram's | wait an hour or edit `LIMITS` in `tgagent/config.py` and restart the daemon |
| an error about waiting some number of seconds (FloodWait) | Telegram's own limit: too often | wait the named time; repeating the request earlier only extends it |
| alerts do not arrive | the bot is not linked, `/pause`, quiet hours, the chat is in `mute_chats`, or the message does not match any rule | `/status` and `/rules` in the bot; `uv run tg call events '{"limit": 5}'` shows whether the watcher saw the message at all |
| `tg_transcribe` refuses | no engine at all: the built-in transcript needs Telegram Premium, Groq needs a key, the local one needs an installed model | `GROQ_API_KEY` in `.env` or `uv sync --extra local-whisper` |
| `tg_memory(action="update")` refuses | no `OPENAI_API_KEY` (or a compatible key under `TG_MEMORY_BASE_URL`) | add the key to `.env` and restart the daemon |
| nothing works after a machine reboot | there is no daemon autostart — it does not install itself | `uv run tg init` will offer launchd or systemd; see [configuration.md](configuration.md#autostart) |
| `Permission denied` on `.env` or `data/` | the directory was created by another user (a common story after running docker as root) | give ownership back: `chown` to yourself, `chmod 600 .env`, `chmod 700 data` |
| nothing starts on Windows | the MCP-to-daemon link goes over a unix socket | WSL or docker |
| the sign-in is stuck on the 2FA password | the password is read straight from the terminal and never gets into arguments or logs | `uv run tg password` in a real terminal, not from the agent |

It is better to sign in through the wizard: `uv run tg init` turns Telegram codes
(`PHONE_CODE_EXPIRED`, `API_ID_INVALID`, `AUTH_KEY_DUPLICATED`,
`PHONE_NUMBER_BANNED` and the rest) into an explanation and the exact next
command. A separately started `uv run tg login` does not do this — it shows the
Telethon exception as is, and you have to read the error code out of it yourself.

## Where to look

```bash
uv run tg daemon logs -n 100          # daemon tracebacks, Telethon errors
uv run tg call events '{"limit": 10}' # what the watcher saw in incoming messages
uv run tg call actions '{"since": "-24h"}'  # what the agent did and where it was refused
```

`data/daemon.log` is the only place where a full traceback stays: the agent gets
the error as an explanation, not as a traceback. `data/events.jsonl` is written
always, independently of the alert rules, so "did not wake me up" and "did not
see it" stay distinguishable. `data/actions.jsonl` is the audit of writing calls,
including the failed ones and the ones the owner declined.

## If none of this fit

Attaching the output of two commands to an issue is enough:

```bash
uv run tg doctor
uv run tg daemon logs -n 50
```

The first contains no secrets by construction. The second has to be looked
through and cleaned up before sending. Your name and id are certainly in the
log: the start line `telegram[main]: signed in as NAME (id 123456789)` is written
on every daemon start, that is, it will land in almost any slice of it. Plus chat
names and pieces of text inside Telethon tracebacks. Never attach `.env` — it
holds the application keys and the bot token.
