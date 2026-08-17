# What to check before a release

A list for whoever tags and publishes. It does not duplicate CI: CI checks the
sources, while a release checks what CI does not have and never will — a
from-scratch install on someone else's machine, a live account, and whatever
leaves for the public repository together with the code. The order is not
accidental: cheap and static first, then the install, then the live check, and
the tag only at the very end.

Every step is a command you can see and repeat. None of this needs an agent; if
a check required "asking the model", the check is badly described.

## 0. Clean tree

```bash
git status --short     # empty
git branch --show-current
```

A release is built from what is in the repository, not from what happens to lie
next to it uncommitted. An uncommitted file the project cannot work without is
the cheapest release mistake and the most annoying one: everyone except the
author will be missing it.

## 1. The three mandatory checks

```bash
uv run ruff check
uv run pytest -q
uv run python scripts/selfcheck.py     # exit code 0
```

CI runs the same three. The numbers from `pytest` and `selfcheck` are worth
memorising: the tool counter and the test count are the only defence against
"fixed it in one layer out of three". The tool counters in README and in `docs/`
are reconciled by `selfcheck` itself, there is no need to recount them by hand.

## 2. Both ends of the declared Python

`requires-python = ">=3.11"` is a promise, and it is only verified by running:

```bash
UV_PYTHON=3.11 uv sync --locked && UV_PYTHON=3.11 uv run pytest -q
UV_PYTHON=3.13 uv sync --locked && UV_PYTHON=3.13 uv run pytest -q
uv lock --check                        # the lock agrees with pyproject.toml
```

If the floor in `pyproject.toml` moved, the matrix in `.github/workflows/ci.yml`
has to repeat its bounds. Once they diverge, CI is green on versions nobody
promised and silent about the one that was promised.

## 3. Install from scratch

The most important step, and the only one that cannot be borrowed from CI: on
the author's machine there is already a `.env`, a session, a virtual environment
and a configured client, and because of them exactly those breakages stay
invisible that the first outsider will run into.

A copy of the repository without anything personal and without the environment:

```bash
rsync -a --exclude='data/' --exclude='.env' --exclude='.venv/' --exclude='.git/' \
      --exclude='.pytest_cache/' --exclude='.ruff_cache/' --exclude='__pycache__/' \
      ./ /tmp/tg-release-check/
cd /tmp/tg-release-check
```

In the copy:

```bash
uv sync                                # installs without reaching Telegram
uv run python -c "import tgagent"       # the package imports
uv run tg --help                        # the list of commands
uv run tg doctor                        # says "not configured" instead of crashing
uv run tg init < /dev/null              # explains what is missing and exits with 1
uv run pytest -q                        # tests are green on a clean machine too
```

What is actually being checked here:

- `tg doctor` on an empty install must be a **report**, not a traceback. A person
  with nothing configured is the normal case, not a failure.
- `tg init` without a terminal (`< /dev/null`) must name the step that cannot be
  done without a human, and the command that does it. A silent crash costs the
  most here: this is the first command anyone runs.
- `pytest` in the copy checks that the tests lean neither on `.env`, nor on the
  session file, nor on the author's `data/`. If they go red in a clean copy, then
  they were green on someone else's data.

Delete the copy after the check: `rm -rf /tmp/tg-release-check`. It is left with
a `.env` full of keys if the wizard got past the first step.

Additionally, if the release is going anywhere besides `git clone`:

```bash
uv build       # sdist and wheel build
```

The wheel contains only the `tgagent` package. `agents/*.md`, the autostart
templates and `scripts/` are not in it, and installing from a wheel is not
described in the documentation — there is one supported path: clone the
repository.

## 4. Live check on an account

By hand only and on your own machine only. CI does not have this and never will:
the check needs the session file, that is, access to a personal Telegram without
a password and without 2FA.

```bash
uv run tg daemon restart
uv run tg daemon logs -n 30        # no tracebacks after the "rpc: listening" line
uv run python scripts/smoke.py     # "failures: 0"
```

`smoke.py` prints **real** chat names, contacts and phone numbers from the
account it runs on. Its output does not go into an issue, does not get pasted
into a PR and is not shown in a screen recording: only the last line, with the
failure counter, is fit for a report.

Skipped (`skip`) checks in `smoke.py` are normal: some tools depend on Premium,
on keys and on suitable messages being there. What to look at is `FAIL`.

The daemon is left running after the check — otherwise the agent's next run
starts with an error about an unavailable socket.

## 5. Nothing personal in the repository

The repository is public, and this has to be checked against the working tree
every time: most often a leak arrives not in the code but in a fresh example in
the documentation, copied off a real chat.

```bash
grep -rInE 'СВОЙ_ТЕЛЕФОН|СВОЙ_ID|СВОЙ_USERNAME|СВОЁ_ИМЯ' \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude=uv.lock .
grep -rInE '[0-9]{8,10}:[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9_-]{20,}|gsk_[A-Za-z0-9]{20,}' \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude=uv.lock .
grep -rInE '\b[0-9a-f]{32}\b' \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude=uv.lock .
grep -rIn '/Users/\|/home/' \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude=uv.lock .
```

Substitute your own real values into the first command — phone number, numeric
id, `@username`, profile name, the names of two or three real chats. They must
not be in this file itself, which is why there are placeholders here: otherwise
the checklist would become the very leak it is looking for.

What may show up and is fine: made-up examples (`+79991234567`,
`1234567890:AA...`, `@username`, ids of the `222222222` kind), the `YOUR_USER`
path placeholders in the autostart templates, and the author's name in
`LICENSE`, `pyproject.toml`, `README.md`. This file finds itself too — it holds
the same samples the search runs on. Everything else is to be gone through one
by one.

Separately — the files added since the previous release:

```bash
git status --short          # new and changed
git diff --stat             # the size of the edits
```

A new file is checked by eye in full, not with grep: grep will not catch a
retold conversation or a screenshot of settings.

## 6. Publication surfaces

- the version in `pyproject.toml` is bumped and matches what the tag will say;
- the links in `[project.urls]` open, and the repository behind them exists;
- `README.md` describes the install the way it went in step 3, not the way it
  went three releases ago;
- `SECURITY.md` names a live channel for vulnerability reports;
- `LICENSE` is in place, the year and the name are current.

## 7. Tag

Only once everything above is green:

```bash
git tag -a vX.Y.Z -m "..."
git push origin vX.Y.Z
```

The tag goes on the commit that was checked, not on "almost the same one". If
something was fixed after the checks — steps 1, 3 and 4 are repeated, they are
cheap.

## What this list does not check

- **Git history.** A secret deleted from the working tree stays in the history;
  cleaning the history is a separate operation, and it is done before the first
  publication.
- **Behaviour under load and Telegram's limits.** FloodWait only reproduces on a
  live account and only over time.
- **Windows.** Not supported by construction: MCP talks to the daemon over a
  unix socket.
