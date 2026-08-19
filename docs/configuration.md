# Configuration

## Environment variables

They live in `.env` next to the code (or wherever `TG_ENV_FILE` points). The
template is [`.env.example`](../.env.example). The file is read both when the
daemon starts and by the CLI; permissions must be 600, and `tg setup` sets them
itself. To fill them in without remembering the variable names, use the wizard:
`uv run tg init` asks only for what is missing, and `uv run tg doctor` then shows
which of this list is set and what permissions the file has.

"Next to the code" means the clone. Installed as a package there is no such
place — next to the code is `site-packages`, and the next upgrade rewrites it —
so `.env` and `data/` move to `~/.tgagent` instead; see
[install.md](install.md#without-a-clone). `tg doctor` prints the paths it
actually used, which is the answer to "where is my session".

| Variable | Required | What it is |
|---|---|---|
| `TG_API_ID` | yes | app id from my.telegram.org |
| `TG_API_HASH` | yes | the hash from the same place |
| `TG_BOT_TOKEN` | for alerts | bot token from @BotFather |
| `TG_ALERT_CHAT_ID` | for alerts | where to send; filled in by `tg link-bot` |
| `TG_ALLOW_WRITE` | no (1 by default) | `0` — read-only mode |
| `TG_LANG` | no (`en` by default) | language of what the owner reads: `en` or `ru` |
| `TG_DATA_DIR` | no | where the state lives; `./data` in a clone, `~/.tgagent/data` installed, `/data` in docker |
| `TG_ENV_FILE` | no | a non-standard location of `.env` itself |

`TG_API_ID` and `TG_API_HASH` are the MTProto keys. Without them only the Bot API
is available, that is, you see exactly what was written to the bot and nothing
from your own chats.

Get a **separate** bot for the agent. The daemon hard-ignores messages from the
bot whose token is in its config — otherwise an alert flies back in as an
incoming message and causes the next alert, up to FloodWait.

### Interface language

```bash
TG_LANG=ru
```

`en` by default. It picks the language of the text addressed to the owner: setup
hints, the `tg init` wizard, `tg doctor`, everything `tg` prints, and the alerts,
digests, reminders and questions that arrive through the bot.

Nothing else moves with it. Logs, the errors returned to Claude and all of
`docs/` are always English: those are read by a developer, a reviewer or the
model, and a translated traceback only makes the problem harder to search for.

The value is re-read on every call, so an edited `.env` takes effect without
restarting the daemon. Values like `ru_RU.UTF-8` or `en-GB` are understood — that
is what people paste out of their system locale — and an unsupported language
falls back to `en` instead of failing.

Both languages live side by side in `tgagent/i18n.py`, one catalog keyed by a
short identifier. `scripts/selfcheck.py` fails on any gap in it: a language that
covers half the messages is worse than no language at all.

### Read-only mode

```bash
TG_ALLOW_WRITE=0
```

`send`, `send_file`, `edit`, `delete`, `forward`, `mark_read`, `mute`, `archive`,
`pin` start returning an error without reaching Telegram. The check sits in the
core (`_assert_write`), not in the agent's prompt, so the model cannot talk its
way around it. It is re-read on every call — editing `.env` and restarting the
daemon is enough.

Between "read only" and "anything goes" there is a third option — ask permission
for every action, see [write confirmation mode](#write-confirmation-mode).

### Write confirmation mode

The middle mode between "read" and "anything goes": every writing action asks the
owner for permission in the bot and does not run without an answer. This is what
makes it possible to run the agent autonomously at all — on a schedule, in
`/loop`, from cron: nobody is sitting next to it, and yet a human still makes the
decision.

It lives in `data/rules.json`, next to the alert rules:

```json
{
  "confirm_writes": "off",
  "confirm_whitelist": ["me"],
  "confirm_timeout_sec": 90
}
```

| Key | Meaning |
|---|---|
| `confirm_writes` | `off` (default) — as before; `outgoing` — ask only about what other people see or what cannot be rolled back; `all` — about every writing action |
| `confirm_whitelist` | chats not to ask about: an id, a piece of the title, or `me`. By default only Saved Messages |
| `confirm_timeout_sec` | how long to wait for an answer, 10–110 seconds; silence is a refusal |

`off` is the default on purpose: an update must not change the behaviour of an
installation that is already set up.

In `outgoing` mode the question is asked about `send`, `send_file`,
`send_location`, `send_sticker`, `poll`, `schedule`, `forward`, `edit`, `delete`,
`react`, `click`, `pin_message`, `block`, `invite`, `moderate`, `create_group`,
`chat_edit`, `leave`, `topic_create`, `topic_edit`, `bot_edit`, `contact_edit`,
`sessions` (only `terminate`), `stories` (only `mark_read`), `scheduled` (only
`cancel_ids`). Whereas `mark_read`, `mute`, `archive`, `pin`, `draft`,
`folder_edit`, `notify`, `remind` and `rules` go through silently: they show
nothing to other people, they are reversible, and they live inside your own
account. The split is exactly this way because "ask about everything" quickly
turns into "press allow without looking", and that is worse than not asking at
all. In `all` mode these are asked about too.

Two exceptions hold in both modes. `tg_alert` and `tg_ask` never ask — they are
the channel the question itself travels through, and confirming a question with a
question would loop. And `tg_stories`, `tg_scheduled`, `tg_sessions` and
`account_use` count as writing only because of a single argument: without
`mark_read`, `cancel_ids`, `terminate` and `persist` such a call changes nothing,
and no question is asked.

Inbox filters do not go through the mode at all — see
[inbox filters](#inbox-filters).

The question shows what exactly is about to go out: the method, the chat (with an
id, if it could be resolved), the beginning of the text — no more than 350
characters — and the remaining arguments such as a file path or a list of ids.
The answer is a button, allow or deny, or plain text to the bot. A refusal and
silence both produce a call error, "the owner did not confirm", rather than a
quiet success, and both outcomes land in `actions.jsonl`: afterwards you can see
what the agent tried to do and got refused.

The whitelist is needed, otherwise the mode is useless for drafts and notes to
self: Saved Messages is in it from the start. Matching is by
`me`/`saved`/`saved messages`, by numeric id or by a piece of the chat title —
the same as in `watch_chats`.

The 110-second cap on waiting is not arbitrary: the MCP client waits for the
daemon's answer no longer than 120 seconds, and a permission granted later would
lead to a message being sent that the agent has already been told "the network
dropped" about.

If the bot is not configured (`TG_BOT_TOKEN`/`TG_ALERT_CHAT_ID` empty) while the
mode is on, there is nobody to ask, and writing is refused with an explicit
message saying so. An unrecognised value of `confirm_writes` also forbids
writing, rather than silently letting it through.

It is edited **by hand only**, in the file. The `confirm_*` keys do not go
through `tg_rules` — an attempt to change them returns an error, and saving the
alert rules (`tg_rules`, `/watch`, `/mute`) does not overwrite them with values
from the daemon's memory. The reason is the same as for the limits: the agent
must not be able to lift a restriction off itself. An edit to the file takes
effect immediately, there is no need to restart the daemon — the mode's settings
are re-read from disk on every writing call.

## What it costs

The agent itself is free, and in its basic form there is nobody to pay: MTProto, the
notification bot, server-side search, the local index, alerts, the digest, filters and
reminders cost nothing. A bill can appear in exactly two places, and both of them need a
key that is not there by default:

- **Chat dossiers** (`tg_memory`) go to an external model and are billed per token — by
  default `gpt-4o-mini` under the `OPENAI_API_KEY` key. This is the only thing that spends
  money by itself, with no Claude running, and that is why it is limited three times over:
  without a key the tool refuses, auto-refresh is off, and once it is on it runs into a cap
  per hour (`memory_max_per_hour`, 10 by default). `TG_MEMORY_BASE_URL` takes the calls to
  any compatible service, a local one included — free then.
- **Audio transcription** (`tg_transcribe`) — three engines at three different prices. The
  one built into Telegram is computed on its servers and is genuinely available with
  Premium (without a subscription Telegram gives a small free quota). Groq's free tier is
  limited by the number of requests, above it there is a paid plan. The local model costs
  no money at all: its price is a gigabyte and a half of weights and the time it takes to
  run.

Claude's own tokens do not belong here — they are counted by your client, not by the
agent. The keys and the caps for both are in the two sections that follow.

## Chat dossiers

| Variable | What |
|---|---|
| `OPENAI_API_KEY` | the key the dossiers are written with; without it `tg_memory(action="update")` refuses to work |
| `TG_MEMORY_MODEL` | the model, `gpt-4o-mini` by default |
| `TG_MEMORY_BASE_URL` | the API address, `https://api.openai.com/v1` by default; any compatible service goes here too |
| `TG_MEMORY_FIRST` | how many messages to take on the first pass, 300 |
| `TG_MEMORY_MAX_NEW` | cap on new messages per update, 400 |
| `TG_MEMORY_MAX_CHARS` | how many characters to ask the model for, 3000 |
| `TG_MEMORY_TIMEOUT` | how long to wait for the model's answer, 90 s |

`TG_MEMORY_BASE_URL` exists so that the correspondence is not obliged to go to
OpenAI: any API-compatible service or a local server is substituted with a single
line. This is the only place in the agent where the content of chats leaves the
machine — see [security.md](security.md).

## Audio transcription

| Variable | What |
|---|---|
| `GROQ_API_KEY` | a key from console.groq.com/keys |
| `TG_WHISPER_ENGINE` | `auto` (default), `telegram`, `groq`, `local` |
| `TG_GROQ_MODEL` | `whisper-large-v3-turbo` by default |
| `TG_WHISPER_MODEL` | the local model, `mlx-community/whisper-large-v3-turbo` by default (on non-Apple hardware the tail of that name is used — `large-v3-turbo` for faster-whisper) |
| `TG_TRANSCRIBE_MAX_MB` | cap on file size, 24 MB |

The watcher uses this by itself: with `transcribe_voice: true` (the default) an
incoming voice message or video note is transcribed by Telegram's built-in engine
before the alert is even sent, and what arrives on your phone is text rather than
"[attachment]". The transcript also lands in `events.jsonl`, so `tg_events` will
show it later without asking again.

The order inside `auto` was not chosen by accident: voice messages and video
notes have Telegram's built-in transcription — it is instant, free and does not
require downloading the file at all, so it goes first. Telegram does not
transcribe music and ordinary video, so there Groq starts right away, and if
there is no key or no network — the local model.

The local engine is installed separately, because it drags megabytes along with
it:

```bash
uv sync --extra local-whisper
```

What exactly gets installed is decided not by a human but by a marker in
`pyproject.toml`: `mlx-whisper` goes only on Apple Silicon (`sys_platform ==
'darwin'` and `platform_machine == 'arm64'`), and `faster-whisper` goes
everywhere. On Linux and on an Intel Mac the same command simply does not pull
mlx in, instead of failing on it.

The core tries the engines in the same order: if `mlx_whisper` imported, it
transcribes with that, otherwise it takes `faster-whisper`. Their model names
differ, so for faster-whisper the tail `large-v3-turbo` is taken out of
`mlx-community/whisper-large-v3-turbo`; there is no need to change
`TG_WHISPER_MODEL` for the sake of Linux.

The first run downloads the weights (turbo is ~1.6 GB) into the Hugging Face
cache. After that everything is computed on the machine, nothing goes out. For
weak hardware pick a smaller model:
`TG_WHISPER_MODEL=mlx-community/whisper-small`.

## Multiple accounts

The daemon holds all signed-in accounts at once — one Telethon client per
account, all in a single process, because the session file must still have
exactly one owner.

```bash
uv run tg login --account work         # add a second one
uv run tg accounts                     # which ones exist and which is the default
uv run tg accounts --default work      # change the default for good
uv run tg call dialogs '{"limit":5}' --account work
uv run tg logout --account work        # remove
```

The label determines the session file name: `main` → `data/session.session`,
`work` → `data/session-work.session`. The `api_id`/`api_hash` keys are shared, a
separate `.env` is not needed: they identify the application, not the person.

### What belongs to an account and what is shared

| Per account | Shared across the installation |
|---|---|
| the session file `data/session*.session` | the `api_id`/`api_hash` keys in `.env` |
| the message index `data/index*.db` | the notification bot and the alert chat |
| the chat dossiers `data/memory*/` | the alert rules and filters in `data/rules.json` |
| Premium and the caps that depend on it | the write permission `TG_ALLOW_WRITE`, the `confirm_writes` mode |
| the `account` label in `events.jsonl` and `actions.jsonl` | the write limits (60 messages an hour — in total) |

Everything that describes the **correspondence** is split across files: mixing
two accounts into one index means losing the ability to wipe one without touching
the other, and the same person in a personal and a work account is two different
conversations with different histories. What stayed shared is what describes the
**owner and the installation**: they have one phone, one notification bot and one
set of quiet hours, no matter how many accounts they set up.

The write limits are shared on purpose: 60 messages an hour is the total, not 60
per account, otherwise the guarantee "the agent will not spam your contacts"
could be worked around by signing in a second session.

### Where calls go

There are two different "defaults", and they must not be confused:

- **the default on disk** — `data/settings.json`, key `default_account`. It is
  set by `uv run tg accounts --default work` or `tg_account_use(persist=true)`.
  It survives a daemon restart and closing Claude, and applies to all clients at
  once;
- **a one-off switch** — `tg_account_use("work")` without `persist`. It lives in
  the MCP server process, that is, until the end of this Claude session; other
  clients and the background watcher do not see it.

There used to be only the second kind of choice, and that was precisely the
mistake: a closed Claude silently returned the agent to the main account, and the
very first message after a restart went to the wrong place. Now the default
choice lies on disk, and `tg_accounts` shows both values as separate fields —
`using` (where this client is writing right now) and `default` (what is on disk).

A separate file, not `rules.json`, because the meaning is different: the rules
describe when to wake the owner, while `settings.json` describes where the agent
writes. Mixing an installation setting with alert conditions means one day
changing one while aiming at the other.

An account chosen as the default and signed out afterwards is **not silently
replaced** by the main one: every call without a label honestly refuses and names
the sign-in command. A message that went to the wrong account cannot be recalled,
but an error can be corrected. `uv run tg logout` itself returns the default to
`main` if it is removing exactly that one.

The response of every writing call contains an `account` field — not for
decoration: the default could have been changed in another Claude session, and
the agent must see where it wrote, not only where it intended to.

A one-off question to a neighbouring account does not require switching:
`tg_capabilities` accepts `account`, and in the CLI `--account` plays the same
role.

### Access level

Premium is bought per account, so the access level differs between accounts too:
`tg_capabilities(all_accounts=true)` shows them side by side — what differs (the
subscription, the tools that depend on it, the server's caps) per account, and
the shared half of the setup (the bot, the keys, the write permission) once for
the whole installation. `tg_accounts` shows the same thing more briefly:
`premium` and how many tools are available and blocked.

### Rules and filters: one file for all accounts

`data/rules.json` stays shared, and that is a decision, not an unfinished job.
The rules answer the question "when to wake the owner", and there is only one
owner: quiet hours, the digest schedule, throttling, the write confirmation mode
and the bot channel are shared across all their accounts. Split the file per
account and you get two digest schedules and two sets of quiet hours where the
owner asked for one.

The only account-dependent things in the rules are the chat lists, and they are
bound by label:

```json
{
  "watch_chats": ["Mom", "work:Client"],
  "mute_chats":  ["main:News"],
  "memory_chats": ["work:Client"],
  "confirm_whitelist": ["main:me"],
  "auto": [
    {"name": "work channels to archive", "account": "work", "type": "channel",
     "action": ["read", "archive"]}
  ]
}
```

- a pattern **without a label** applies in all accounts — the same as before, so
  an old `rules.json` keeps working after an update exactly as it did;
- `work:Client` applies only in the `work` account. That is what the binding is
  for: two different "Mom"s in a personal and a work account are two different
  people, and `watch` for one must not catch the other;
- **only an existing account label** counts as a prefix. A title like
  `Lunch: who is coming` stays a title: a rule that silently stopped firing is
  worse than a missing one;
- for a filter rule (`auto`) the label is set by the `account` field — one or a
  list. It is a narrowing, not a condition: the rule is still obliged to have at
  least one condition out of `chat`, `from`, `keyword`, `type`, otherwise it
  would fire on every incoming message in the account.

The bot commands `/watch` and `/mute` do not set a label — a chat added by them
is watched in all accounts. If you need the binding, write it out:
`/watch work:Client`.

### Watcher and logs

The watcher listens to **all** accounts at once, regardless of which one is
selected as the default: a missed message cannot be fetched after the fact, and
the choice of account is about writing, not about watching.

An `account` field is written into `events.jsonl` and into `actions.jsonl`, a
label is added to the alert if the message arrived in a non-main account, and in
the digest the chat is signed with a label (`Client · work`). Alert throttling is
counted per (account, chat) pair, so that the same chat in two accounts does not
mute itself.

### Signing in is the owner's job

An account cannot be signed in from MCP, and this is not an implementation limit:
the agent does not see the SMS code and the 2FA cloud password, and must not see
them. That is why every refusal of the kind "the account is not signed in"
contains the exact command with the label:

```
Account 'work' is not signed in (available: main). The owner signs in themselves,
the agent does not see the code: cd /path/to/tg-mcp && uv run tg login --account work
```

The directory substituted is the real one — the one the agent was started from —
so that the command can be run word for word, without working out where the
project lies.

The same command is printed by `tg_accounts` (the `add` field), `uv run tg
accounts` and the daemon's message when it starts without a single session.

## Autostart

The daemon can be hung on the system's login service, so that alerts, the digest
and reminders work without Claude running. The wizard offers this too (`uv run tg
init`, the last step): it works out the system itself, substitutes the real paths
to `uv` and to the project directory into the template, and enables the service.

In both templates the paths are placeholders with `YOUR_USER`, and substituting
them is mandatory: the project works from any directory, and the service is
started without your shell and expands no `~` at all.

Installed as a package the wizard writes a different command into the same
templates: `uv run --directory` has no project to enter there, so the service
starts the interpreter of the environment the package lives in — the same daemon,
one process shorter. That is worth knowing if you compare your unit with the one
below and find no `uv` in it.

### macOS (launchd)

The template is `com.tgagent.daemon.plist` in the project root.

```bash
sed -e "s|/Users/YOUR_USER/.local/bin/uv|$(command -v uv)|g" \
    -e "s|/Users/YOUR_USER/tg-agent|$PWD|g" \
    com.tgagent.daemon.plist > ~/Library/LaunchAgents/com.tgagent.daemon.plist
launchctl load -w ~/Library/LaunchAgents/com.tgagent.daemon.plist
```

To unload it again — `launchctl unload ~/Library/LaunchAgents/com.tgagent.daemon.plist`.

`sed` here writes into a new file instead of editing the template in place: `sed
-i` has a different syntax in BSD (macOS) and GNU (Linux), and a recipe with `-i`
breaks exactly when it is carried over to another system.

### Linux (systemd)

The template is `tgagent.service` in the project root. The unit is a **user**
one, root is not needed.

```bash
mkdir -p ~/.config/systemd/user
sed -e "s|/home/YOUR_USER/.local/bin/uv|$(command -v uv)|g" \
    -e "s|/home/YOUR_USER/tg-agent|$PWD|g" \
    tgagent.service > ~/.config/systemd/user/tgagent.service
systemctl --user daemon-reload
systemctl --user enable --now tgagent
```

To check and to stop: `systemctl --user status tgagent`,
`systemctl --user disable --now tgagent`. The unit writes its log into the same
`data/daemon.log` as `tg daemon start`, so `uv run tg daemon logs` works the same
way in both cases; in parallel everything is visible in
`journalctl --user -u tgagent`.

One caveat that macOS does not have: user units are stopped on session exit by
default and do not start until the first login. For the daemon to live with the
monitor off and to come up after a reboot without a login, you need
`loginctl enable-linger $USER` — once.

### Docker

Neither of these is needed: the role of autostart is played by
`restart: unless-stopped` in `docker-compose.yml`.

## Alert rules

`data/rules.json`. Edited through `tg_rules`, the bot commands `/watch` and
`/mute`, or by hand. The default values:

```json
{
  "enabled": true,
  "alert_on_private": true,
  "alert_on_mention": true,
  "keywords": [],
  "watch_chats": [],
  "mute_chats": [],
  "ignore_bots": true,
  "transcribe_voice": true,
  "alert_on_reaction": false,
  "min_interval_sec": 3,
  "quiet_hours": null,
  "digest_at": [],
  "auto": []
}
```

| Key | Meaning |
|---|---|
| `enabled` | the main switch for alerts |
| `alert_on_private` | any private message from a human |
| `alert_on_mention` | `@you` or a reply to your message in a group |
| `keywords` | substrings that fire anywhere (case does not matter) |
| `watch_chats` | an id or a piece of the title: alert on every message |
| `mute_chats` | an id or a piece of the title: never alert |
| `ignore_bots` | do not wake you because of bots |
| `transcribe_voice` | transcribe voice messages and video notes right into the alert |
| `alert_on_reaction` | wake you with an alert when somebody reacts to your message. Off by default: reactions are always written into the log, but notifying about every little flame is too much |
| `min_interval_sec` | throttling: no more than one alert per N seconds per chat |
| `quiet_hours` | `[23, 8]` — stay silent from 23:00 to 08:00; `null` — round the clock |
| `digest_at` | digest times, `["09:00", "20:00"]`; an empty list means off. See [digest](#scheduled-digest) |
| `auto` | inbox filters: condition → action. See [filters](#inbox-filters) |
| `memory_auto` | update chat dossiers on their own, as the correspondence goes. Off by default: this is money and sending pieces of the correspondence outside |
| `memory_after` | after how many new messages in a chat to update the dossier, 50 by default |
| `memory_chats` | which chats to keep dossiers for; an empty list means only those where a dossier was already created by hand |
| `memory_max_per_hour` | cap on auto-updates per hour, 10 by default. This is the agent's only way to spend money, and it is capped |

The order of the checks is this: pause and `enabled` → your own outgoing → your
own bot → `ignore_bots` → `mute_chats` → quiet hours → `keywords` →
`watch_chats` → mention → private. The first match decides, and the reason goes
into the alert text.

Note that `mute_chats` hits earlier than `keywords`, that is, a muted chat will
not wake you even by a keyword. This is done on purpose — "muted" must mean
"muted".

Even earlier than this whole chain come the [inbox filters](#inbox-filters): the
`auto` section is worked through before `alert_reason`, and a rule that fired
suppresses the alert for that same message by default. It can be brought back
with the `alert` flag in the rule itself.

The same file holds the `confirm_*` keys — they are not about alerts but about
[write confirmation](#write-confirmation-mode), and they are not edited through
`tg_rules`.

Edits through `tg_rules` and the bot commands take effect immediately. An edit of
`rules.json` by hand — after `tg daemon restart`: the daemon holds the rules in
memory and re-reads only the `confirm_*` keys from disk.

## Scheduled digest

```json
{ "digest_at": ["09:00", "20:00"] }
```

The times are local, the format is `HH:MM`. At the given moment the daemon
collects the digest for the period itself and sends it to the bot: how many chats
wrote and how many messages there were, who wrote the most, what fell under
`keywords` and `watch_chats`, how many reactions there were to your messages, how
many filters fired and how much is left unread.

It is the daemon that counts this, not the agent, and that is the whole point:
the digest arrives in the morning and in the evening even when Claude Code is not
running and nobody asked for anything.

The source is `events.jsonl`, which holds **all** incoming messages, not only
those that caused an alert. That is why the digest also shows what you were
deliberately not woken about. Unread is counted separately, by a live request for
the dialog list at the moment of sending.

What does not go into the count: your own outgoing messages, the messages of your
own alerting bot (otherwise every alert would count as a second message) and, if
`ignore_bots` is on, the messages of bots. Chats muted by `mute_chats` do go into
the overall count, but are not shown in the "By rules" section — "muted" means
"muted".

| Behaviour | How exactly |
|---|---|
| An empty digest | is not sent at all: if there were neither messages nor reactions in the period, the slot is simply marked as done |
| Daemon restart | the last slot worked through lies in `data/digest.json`; there will be no second digest for the same slot |
| The daemon was down | a slot that passed more than two hours ago is marked as missed and is not sent late: a "morning" digest arriving in the evening is not a digest any more |
| `quiet_hours` and `/pause` | the slot is skipped silently, but the period is not lost: its messages will go into the next digest |
| The period | from the last actual send to the current slot; on the first run — from the previous scheduled slot, but no deeper than 26 hours |

The tick is once every half a minute, by the same background task as the
reminders: minute precision is enough for the schedule.

`tg_status` shows the state in the `digest` field: the last slot worked through,
the time of the last send and the start of the period for the next one.

## Inbox filters

Telegram has no mail filters at all. The `auto` section in `rules.json` adds
them: the same condition as an alert has, but instead of "wake the owner" — an
action.

```json
{
  "auto": [
    {
      "name": "shops to archive",
      "type": "channel",
      "keyword": ["discount", "promo code"],
      "action": ["read", "archive"]
    },
    {
      "name": "invoices to Saved Messages",
      "from": ["Accounting"],
      "keyword": "invoice",
      "action": "save",
      "alert": true
    },
    {
      "name": "noisy chat to a folder",
      "chat": -1004444444444,
      "action": "folder",
      "folder": "Noise",
      "enabled": false
    }
  ]
}
```

### Conditions

| Key | What |
|---|---|
| `chat` | an id or a piece of the chat title; one value or a list |
| `from` | an id, an `@username` or a piece of the sender's name |
| `keyword` | a substring in the text, case does not matter; a list means "any of" |
| `type` | `private`, `group`, `channel`, `bot` — by the type of the chat itself, not of the sender |

The listed conditions are joined with AND: a rule with `type` and `keyword` fires
when both hold. Inside a single condition a list means OR. A rule without a
single condition cannot be saved: it would fire on every incoming message.

### Actions

The list is closed and deliberately short — only what is safe and reversible:

| `action` | What it does | Core method |
|---|---|---|
| `read` | mark the chat read | `mark_read` |
| `archive` | move the chat to the archive | `archive` |
| `mute` | mute the chat; `hours` — for how long, otherwise forever | `mute` |
| `folder` | put the chat into the folder from the `folder` field | `folder_edit` |
| `save` | forward the message to Saved Messages | `forward` |

**There are no auto-replies here and there will not be any.** Not a single action
can write to an outside person: for `save` the recipient is wired into the code —
Saved Messages — and the other four send nothing anywhere at all. This is the
main restriction of the whole section: one wrong rule must not lead to a message
to a live human, so the rules simply do not have that ability.

`action` is one value or a list; in a list the actions run in order.

### Order and switching off

The rules are walked top to bottom, in the order they lie in the file. **All**
matching ones fire, not the first: a chat can be both read and moved to the
archive by different rules. A rule with `"stop": true` breaks off the walk — the
ones after it are not considered.

`"enabled": false` switches a rule off without deleting it: a draft rule is
useful to keep next to the working ones. A global `enabled: false` and the
`/pause` command switch the filters off entirely — a pause must stop all the
automation, not half of it.

### How this combines with alerts

The filters run **before** the alert checks. If a rule fired, no alert is sent for
that message: the message has already been handled, and waking the owner about a
chat that has just gone to the archive is the worst possible outcome. The alert
can be brought back with the `"alert": true` flag in the rule: "do it and show it
anyway".

In `events.jsonl` such a message gains an `auto` field with the list of rules that
fired, so that afterwards you can see why there was no alert.

### Audit and guards

Every firing lands in `actions.jsonl` as an ordinary agent action, with an extra
`auto` field — the rule's name. A failed one lands there too, with the error
text. The actions go through the same core methods as the manual tools, and
therefore through `_assert_write` and `RateGuard`: with `TG_ALLOW_WRITE=0` the
filters do not work, and `save` counts against the same send limit as everything
else.

Filter actions do not go through [write confirmation](#write-confirmation-mode)
mode, and that is deliberate: asking permission for every incoming message means
teaching the owner not to read the questions. Permission here is given once, when
the rule is created, and what makes it safe is the closed list of actions.

`archive`, `mute` and `folder` change not the message but the whole chat, so
within a single lifetime of the daemon they run once per chat: a second archiving
changes nothing, and `folder_edit` on an already added chat would return the error
"nothing to change". `read` and `save` work on every message.

Your own alerting bot does not fall under the filters — for the same reason it
does not fall under the alerts.

## Write limits

`tgagent/config.py`, the `LIMITS` dictionary. A sliding window of one hour:

```python
LIMITS = {
    "max_sends_per_hour": 60,
    "max_distinct_chats_per_hour": 15,
    "max_deletes_per_hour": 50,
    "max_text_len": 4096,
}
```

`max_distinct_chats_per_hour` is the anti-spam one: not "how many messages" but
"to how many different people". An agent that has been talked into mass-mailing
something to your contacts runs into it on the sixteenth chat. Verified: the 16th
chat, the 61st message and the 51st deletion return an error.

`max_text_len` is Telegram's own limit, cut off before going to the network.

To change them — edit the file and restart the daemon. The limits are not
configurable through MCP on purpose: the agent must not be able to raise its own
cap.

## Bot commands

Accepted only from the `TG_ALERT_CHAT_ID` chat, the rest are logged and ignored.

```
/start, /help  the list of commands
/status        the agent's state
/can [chat]    what is available and what is missing; with a chat — also the rights in it
/unread        unread
/actions       what the agent did: the last ten writing calls
/rules         the current rules
/watch <chat>  alert on all messages of a chat
/mute <chat>   do not alert about a chat
/pause         switch alerts off
/resume        switch them back on
```

`/pause` lives in the daemon's memory and is reset on restart; `/watch` and
`/mute` are written into `rules.json` and outlive it. `/can` answers with the same
text that `uv run tg capabilities` and the tail of `tg login` print: one
installation — one picture, not three different ones.

## State files

Everything is in `data/`. The detailed table is in
[architecture.md](architecture.md#state-on-disk). In short: `session.session` must
not be copied anywhere, `events.jsonl` is rotated at 20 MB, `actions.jsonl` is the
audit trail and it grows without rotation on purpose, `reminders.json` holds the
reminders that have not fired yet, `digest.json` holds the last digest slot worked
through, so that a daemon restart does not lead to a second digest for the same
slot. `index.db` appears only if the owner created a local index (`tg_index`), and
it is the only file with the text of the correspondence inside — it is wiped by
`tg_index(action="drop")`.
