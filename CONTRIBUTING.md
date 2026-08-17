# How to contribute

The project is personal: it is a tool the owner uses every day, not a product with a
roadmap. Patches are welcome, but it is worth opening an issue first and agreeing on the
shape of the change — a tool nobody needs is easier not to write than to delete later.

Before you start, read [docs/architecture.md](docs/architecture.md): without
understanding why there is a separate daemon between the MCP server and Telegram, half
of the decisions in the code look redundant.

## Environment

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/); the system is macOS
or Linux. The 3.11 floor is there because of a single `datetime.UTC`; raising it (and
reaching for 3.12+ syntax) without a reason is not worth it — it cuts off everyone whose
system carries an older Python. CI runs the checks on both ends of the range, 3.11 and
3.13, so the two cannot drift apart quietly.

```bash
git clone git@github.com:draiqw/tg-mcp.git && cd tg-mcp
uv sync                      # dependencies + the dev group (pytest, ruff)
```

`uv.lock` is in the repository on purpose: this is an application, not a library, and a
reproducible environment matters more here than freedom of versions. The docker image is
built with `uv sync --locked` and fails if the lock has drifted from `pyproject.toml`.
When you change dependencies, commit `pyproject.toml` and `uv.lock` together.

To work with a live account (only if you actually need one) run `uv run tg init`, which
also walks you through the keys and the sign-in; to inspect an existing installation, run
`uv run tg doctor`. **Neither is required for the tests or for selfcheck.**

## Checks

```bash
uv run ruff check                    # linter
uv run pytest -q                     # tests
uv run python scripts/selfcheck.py   # layer integrity, exit code 0
```

CI runs these three commands on every push and pull request, and that is all it runs.

`scripts/smoke.py` is the fourth check, and it is **manual**: it drives the reading
methods through a live daemon on a real account. It is not in CI and never will be.

There is one thing to know about the tests: they do not go to the network, do not open
the session file and do not read the real `.env` — `tests/conftest.py` points
`TG_ENV_FILE` at a non-existent file and `TG_DATA_DIR` at a temporary directory, a fresh
one per test. A new test has to live by the same rules: no network, no session, no writes
into `data/`, only `tmp_path`. A test that needs a live account is not a test — it is a
piece of `smoke.py`.

`selfcheck.py` is a static pass over the sources through `ast`; it does not need the
daemon. If it complains, the layers or the documentation have drifted apart — it is not
"the script is out of date".

Before a tag is placed there are more checks: a from-scratch install, both ends of the
declared Python range, a live run, and a sweep of the tree for personal data. They are
collected in [docs/release.md](docs/release.md) — that is a list for whoever publishes,
not for whoever sends a PR.

## The rule of three layers

A new capability is three places and not one more:

1. a `TelegramService` method in `tgagent/core.py` — all the logic lives here;
2. a row in `dispatch_table()` in `tgagent/daemon.py`;
3. a tool in `tgagent/mcp_server.py` — a thin wrapper, one call to the daemon.

`selfcheck.py` makes sure those three lists do not drift apart, and along the way checks
what sits next to them: a writing method must be in `WRITE_METHODS`, documented rules
must be in `docs/configuration.md`, and subagents must list only tools that exist.

There is exactly one exception and it is already taken: `tg_wait`, `tg_ask`, `tg_remind`
and `tg_actions` live in the daemon because what they need is not Telegram but the event
stream, the bot channel and the daemon's own files. If it feels like your capability is
the fifth exception, it almost certainly is not.

**Every tool is documented in [docs/tools.md](docs/tools.md)** — with its parameters and
with what it actually does. This is not bureaucracy: the description in `docs/tools.md`
is exactly what the model uses to decide whether to call the tool. Selfcheck will not let
an undocumented tool through, and rightly so. Numbers like "79 tools" in the
documentation are checked against reality too — you will have to fix them.

A separate word about writing capabilities. Everything that changes the account must go
through `_assert_write` and `RateGuard` and land in `actions.jsonl`; everything that
shows a conversation to an outside party or puts it on disk must at least land in
`actions.jsonl` (`AUDIT_ONLY`). Sending a message to a live person without the owner
taking part is never added: see the closed list of filter actions in
[docs/security.md](docs/security.md).

## Language

The repository is in English: comments, docstrings, logs, documentation, test names,
identifiers, MCP tool descriptions, and the errors returned to Claude.

## Code style

- The line limit is 100 characters, the target is around 90. `ruff check` is mandatory.
- `ruff format` is **not** applied. The reason is spelled out in a comment in
  `pyproject.toml`: the formatter squeezes together the trailing comments that are
  aligned into a column in the rule and dispatcher tables, and the tables read worse for
  it. Looking at its opinion is fine (`uvx ruff format --diff`), treating it as the norm
  is not.
- The linter's rule set in `pyproject.toml` is picked for this project, and the same file
  says what is switched off deliberately and why. "Turn everything on" is not an
  improvement.
- Comments and docstrings are in English. A comment explains the **reason** rather than
  retelling the code: the "why it is like this" line is worth writing where the next
  person will want to "simplify" it and break it.
- No emoji in the code or in the prose of the documentation.

## Commits

Messages are in English, substantial, and explain the reason. The subject line says what
changed, lowercase, no full stop; the body says why and what follows from it. The subject
line is alive, not a `feat:`/`fix:` template. Look at `git log`: it explains which
problem was closed and which decision was taken, instead of listing the files touched.

An example from the history:

```
reactions: see them as they are placed, and place our own

A reaction is not a message, it does not arrive as an ordinary event, so the
daemon listens to the raw UpdateMessageReactions and takes only reactions to the
owner's messages. ...
```

One commit is one change. Nothing from `data/`, `.env`, the session files, or anything
else tied to a particular account goes into a commit: this is a public repository.

## Pull request

Describe what changes and why, in the same words as in the commit. Make sure all three
checks pass locally — CI runs exactly those, and nobody will fix a red CI on a PR for
its author.

If the change alters the observable behaviour of a tool, say so: the difference between
fixing a bug and breaking somebody's workflow is visible only from the description.
