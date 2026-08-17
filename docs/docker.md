# Docker

The container holds the daemon — the owner of the session. The MCP server is started
**inside the same container** via `docker exec`, not on the host.

Why: MCP talks to the daemon over a unix socket. On macOS a socket created inside the
container is not reachable from the host through a bind-mount — that is the boundary of
the Docker Desktop virtual machine, not a permission. Opening a TCP port instead would
mean exposing full access to the account to any process on the machine. `docker exec`
solves the problem without a port: the socket stays inside.

## Build and run

```bash
cd telegram-mcp                            # the directory the project is cloned into
cp .env.example .env && chmod 600 .env    # fill in TG_API_ID / TG_API_HASH / TG_BOT_TOKEN
docker compose build
```

Sign-in happens once, interactively — you type the code and the 2FA cloud password
yourself, and they end up neither in arguments nor in logs:

```bash
docker compose run --rm tgagent tg login
```

If a session is already in `./data` from a local run, this step is skipped — the
container picks it up as is (same directory).

```bash
docker compose up -d
docker compose logs -f
docker compose exec tgagent tg status
```

Connecting an MCP client:

```bash
claude mcp add telegram -- docker exec -i tgagent tg-mcp
```

## What lives where

| | |
|---|---|
| Image | code and dependencies, nothing else |
| `./data` → `/data` | session, `rules.json`, `settings.json`, `events.jsonl`, `actions.jsonl`, `reminders.json`, `digest.json`, `index.db`, `memory/`, downloads |
| `.env` | read by `docker compose` and passed as environment variables; not copied into the image |

`TG_DATA_DIR=/data` is baked into the image, so the same code works both from a checkout
and in the container. One volume holds all state — no separate mounts for reminders, the
digest and the index are needed, they sit in the same directory. The full list of files is
in [architecture.md](architecture.md#state-on-disk).

The newer features added no environment variables: the digest schedule (`digest_at`), inbox
filters (`auto`) and the write confirmation mode (`confirm_*`) live in `data/rules.json`,
that is on the volume, not in `.env`. So `docker compose restart` picks them up along with
the rest of the state, and editing `confirm_*` takes effect immediately — the daemon
re-reads those keys from disk on every writing call.

## Permissions

The container runs as root so that it can read session files with mode 600 created by your
user on the host. If you need an unprivileged process:

```yaml
    user: "1000:1000"
```

plus `sudo chown -R 1000:1000 ./data`. Note that after this a local run via `uv run` into
the same directory will no longer be able to write without changing the permissions back.

## Everyday operations

```bash
docker compose restart tgagent            # re-read .env and rules.json
docker compose exec tgagent tg call structure '{"sample": 3}'
docker compose exec tgagent tg daemon logs -n 50
docker compose down                       # stop; the data stays in ./data
```

Updating the code:

```bash
git pull && docker compose build && docker compose up -d
```

The dependency layer is rebuilt only when `uv.lock` changes, so an ordinary rebuild takes
seconds.

## Health and restart

`HEALTHCHECK` in the image calls `tg call whoami` — that is, it checks not "the process is
alive" but "the daemon answers on the socket and the session is authorized". No request to
Telegram is made, so the check does not catch a broken network connection: that case is
covered in [troubleshooting.md](troubleshooting.md). The `restart: unless-stopped` policy
brings the container up after a machine reboot; with this setup no separate autostart
(launchd on macOS, systemd on Linux) is needed.

Shutdown is graceful: the daemon catches SIGTERM, closes the Telegram client, removes the
socket and the pid file. `stop_grace_period: 20s` gives it the time to do so.

## Audio transcription in the container

Two engines work out of the box in the image: Telegram's built-in transcription (needs
nothing) and Groq (needs only `GROQ_API_KEY` in `.env`). The local model does not: the image
has neither ffmpeg nor the weights, and dragging a gigabyte and a half into the container by
default would be wrong.

If you need the local engine inside the container too, add this to the `Dockerfile` before
`uv sync`:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
```

and change `uv sync --locked --no-dev` to `uv sync --locked --no-dev --extra local-whisper`.
The model cache is worth moving onto the volume, otherwise the weights are downloaded on
every rebuild: `- ./data/models:/root/.cache/huggingface`.

## Backup

The only thing to copy is `./data` — and treat the copy like the account password:
`session.session` grants sign-in without a password and without 2FA. Logs are rotated
(json-file, 10 MB × 3), `events.jsonl` at 20 MB inside the agent itself; `actions.jsonl` is
deliberately not rotated, it is an audit trail.

If the owner has set up the local index (`tg_index`), an `index.db` appears on the volume —
the text of the indexed conversations in a parseable form. In sensitivity it comes right
after the session file, and backing up the volume means backing up the conversations. If you
do not need it, drop it with
`docker compose exec tgagent tg call index '{"action":"drop"}'`,
details in [security.md](security.md#local-message-index).

To revoke access if the session leaked:

```bash
docker compose exec tgagent tg logout
```

or in the Telegram app → Settings → Devices → the `claude-tg-agent` session.
