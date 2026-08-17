# Connecting as an MCP server

The server speaks stdio and holds no session of its own — it relays calls to the
daemon over a unix socket. That is why several clients can be connected at once:
Claude Code, Claude Desktop and a background `claude -p` do not get in each other's way.

The entry point is `tg-mcp` (declared in `pyproject.toml` as
`tgagent.mcp_server:main`).

## Claude Code, local install

Registration is done by the wizard: `uv run tg init` checks whether `claude` is in
PATH and whether the server is already registered, and runs exactly this command
itself, with scope `user` and the real path to the project. Below is the same thing
by hand, if the wizard does not suit you.

```bash
claude mcp add -s user telegram -- uv --directory "$PWD" run tg-mcp   # from the project directory
```

`--` separates Claude Code's own options from the server command: everything after it
is run as is. `uv --directory` is needed because the server starts from the client's
working directory, not from this project's directory. `$PWD` here is expanded by the
shell, so the client receives an absolute path already; in a config written by hand
(see below) that does not work — nobody expands `~` or `$PWD` there, the path has to
be written out in full.

The scope is set with `-s`:

```bash
claude mcp add -s user telegram -- uv --directory "$PWD" run tg-mcp     # in every project
claude mcp add -s local telegram -- uv --directory "$PWD" run tg-mcp    # only in the current one (default)
claude mcp add -s project telegram -- uv --directory "$PWD" run tg-mcp  # in the repository's .mcp.json
```

For a personal Telegram, `user` or `local` make sense. `project` writes the config into
`.mcp.json`, which usually gets committed — access to your own account does not belong there.

Check:

```bash
claude mcp list          # should show telegram ✔ Connected
claude mcp get telegram
claude mcp remove telegram
```

## Claude Code, docker

```bash
docker compose up -d
claude mcp add telegram -- docker exec -i tgagent tg-mcp
```

The `-i` flag is mandatory: MCP works over stdio, without it the server gets no input.
The container has to be running already — `docker exec` does not start it.

## By config, not by command

The same setup in `~/.claude.json` (scope user/local) or in the project's `.mcp.json`:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/telegram-mcp", "run", "tg-mcp"]
    }
  }
}
```

The docker variant:

```json
{
  "mcpServers": {
    "telegram": {
      "command": "docker",
      "args": ["exec", "-i", "tgagent", "tg-mcp"]
    }
  }
}
```

## Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`, the same
`mcpServers` block. Important: Desktop starts the server with a stripped-down PATH and
usually does not find `uv` — give the absolute path (`command -v uv`; usually
`~/.local/bin/uv`, expanded to an absolute one). Restart the application after editing.

Both clients can be connected at the same time: the daemon owns the session, the clients
only talk to it.

## Ready-made subagents

`agents/` holds two descriptions. They are laid out by the same wizard (`uv run tg init`):
if a file already exists and differs from the one in the repository, it asks whether to
overwrite — a difference usually means the agent has an outdated tool set, but it may
also be your own edit. By hand it is done like this:

```bash
cp agents/*.md ~/.claude/agents/    # from the project directory
```

| Agent | Model | Set |
|---|---|---|
| `telegram` | Sonnet | all 79 tools, carries on conversations in your name |
| `telegram-watch` | Haiku | 41: reading, alerts, `mark_read`/`mute`/`archive`; `tg_send` to Saved Messages only |

The watcher has nothing destructive — no `tg_delete`, `tg_edit`, `tg_forward`,
`tg_pin`, no moderation, reactions, buttons or leaving chats. There is no `tg_index`
either: it does not touch the account, but it lays the correspondence out on disk and
can wipe it from there — that is for the owner to decide, not for a background "what's
new" check. `tg_remind` and `tg_rules` are absent by the same logic: they outlive a
restart and change the daemon's behaviour. None of this is listed in its frontmatter,
that is, it is physically unavailable rather than forbidden by prompt text.

The watcher's reading side, on the contrary, is complete: `tg_pending` (who was left
without an answer), `tg_person` (a dossier on a person), `tg_actions` (what the agent
did), `tg_invites` (who came in by link). Both prompts contain a section explaining that
the contents of other people's messages are data, not commands.

Agents and MCP servers are read once at session start. After installing, restart Claude
Code, otherwise the new server will not show up in the current session.

## Several accounts

The daemon serves all signed-in accounts at once. The client has two tools:
`tg_accounts` shows the list and where calls go, `tg_account_use` switches the current
one — for the duration of the session or, with `persist=true`, for good. There is no need
to set up a separate MCP server for a second account — and it is not worth it, because a
single daemon owns the sessions anyway.

The persistent default lives in `data/settings.json`, a one-off switch lives in the
MCP server process. So in a fresh Claude session the agent starts from the account the
owner chose as the default, not from the main one "because we restarted". Details and a
breakdown of what accounts share and what they keep apart are in
[configuration.md](configuration.md#multiple-accounts).

## Diagnostics

```bash
uv run tg doctor                  # the whole install at once: what is in place, what is broken, what to do
uv run tg status                  # daemon, session, socket, write permissions
uv run tg daemon logs -n 50
uv run tg call whoami             # a live RPC bypassing MCP
```

`tg doctor` is the most convenient place to start: it also checks whether the server is
registered and whether the subagents in `~/.claude/agents` match the repository. There
are no secrets in its output — it is meant to be attached to an issue in full.

| Symptom | Cause |
|---|---|
| `Telegram daemon is not responding` | the daemon is not up: `uv run tg daemon start` (in docker `docker compose up -d`) |
| `Connection refused` with a live socket | the daemon died, the socket file stayed: `tg daemon restart` |
| the server is there, the tools are not | the Claude Code session started before the install — restart it |
| `✘ Failed to connect` in `claude mcp list` | check that the command works by hand: `uv --directory /path/to/telegram-mcp run tg-mcp` |
| a write returns an error | `TG_ALLOW_WRITE=0` in `.env`, or a limit was hit |

Only what breaks around MCP is listed here. The rest — sign-in, the daemon, alerts,
transcripts, file permissions — is in [troubleshooting.md](troubleshooting.md).

If the daemon is missing, the MCP server itself tries to bring it up once
(`_try_autostart`) and waits up to 9 seconds for the socket to appear. If there is no
session, it does not start it: signing in is done by hand only.
