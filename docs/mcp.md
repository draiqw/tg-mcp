# Connecting as an MCP server

The server speaks stdio and holds no session of its own — it relays calls to the
daemon over a unix socket. That is why several clients can be connected at once:
Claude Code, Claude Desktop and a background `claude -p` do not get in each other's way.

The entry point is `tg-mcp` (declared in `pyproject.toml` as
`tgagent.mcp_server:main`).

## Claude Code, local install

```bash
claude mcp add telegram -- uv --directory ~/tg-agent run tg-mcp
```

`--` separates Claude Code's own options from the server command: everything after it
is run as is. `uv --directory` is needed because the server starts from the client's
working directory, not from `~/tg-agent`.

The scope is set with `-s`:

```bash
claude mcp add -s user telegram -- uv --directory ~/tg-agent run tg-mcp     # in every project
claude mcp add -s local telegram -- uv --directory ~/tg-agent run tg-mcp    # only in the current one (default)
claude mcp add -s project telegram -- uv --directory ~/tg-agent run tg-mcp  # in the repository's .mcp.json
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
      "args": ["--directory", "/Users/YOU/tg-agent", "run", "tg-mcp"]
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
usually does not find `uv` — give the absolute path (`which uv`), for example
`/Users/YOU/.local/bin/uv`. Restart the application after editing.

Both clients can be connected at the same time: the daemon owns the session, the clients
only talk to it.

## Ready-made subagents

`agents/` holds two descriptions. Copy them to where the client looks for them:

```bash
cp ~/tg-agent/agents/*.md ~/.claude/agents/
```

| Agent | Model | Set |
|---|---|---|
| `telegram` | Sonnet | all 77 tools, carries on conversations in your name |
| `telegram-watch` | Haiku | 39: reading, alerts, `mark_read`/`mute`/`archive`; `tg_send` to Saved Messages only |

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
`tg_accounts` shows the list, `tg_account_use` switches the current one for the
duration of the session. There is no need to set up a separate MCP server for a second
account — and it is not worth it, because a single daemon owns the sessions anyway.

## Diagnostics

```bash
uv run tg status                  # daemon, session, socket, write permissions
uv run tg daemon logs -n 50
uv run tg call whoami             # a live RPC bypassing MCP
```

| Symptom | Cause |
|---|---|
| `Telegram daemon is not running` | the daemon is not up: `uv run tg daemon start` (in docker `docker compose up -d`) |
| `Connection refused` with a live socket | the daemon died, the socket file stayed: `tg daemon restart` |
| the server is there, the tools are not | the Claude Code session started before the install — restart it |
| `✘ Failed to connect` in `claude mcp list` | check that the command works by hand: `uv --directory ~/tg-agent run tg-mcp` |
| a write returns an error | `TG_ALLOW_WRITE=0` in `.env`, or a limit was hit |

If the daemon is missing, the MCP server itself tries to bring it up once
(`_try_autostart`) and waits up to 9 seconds for the socket to appear. If there is no
session, it does not start it: signing in is done by hand only.
