# tg-agent — the daemon that owns the Telegram session.
# The MCP server is started inside this same container via `docker exec`, so it
# can reach the daemon over the unix socket: the socket lives inside the
# container and is never exposed outside.

FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TG_DATA_DIR=/data

WORKDIR /app

# Dependencies as a separate layer: rebuilt only when uv.lock changes.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# LICENSE goes into the image together with the code: MIT requires the licence
# text to accompany any copy of the program, and an image is a copy.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY tgagent ./tgagent

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Session, logs, rules and downloads live on the volume only — not in the image.
VOLUME ["/data"]

# The daemon is up and answering on the socket.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD tg call whoami >/dev/null || exit 1

CMD ["tg", "daemon", "run"]
