# Configuration

## Environment variables

They live in `.env` next to the code (or wherever `TG_ENV_FILE` points). The
template is [`.env.example`](../.env.example). The file is read both when the
daemon starts and by the CLI; permissions must be 600, and `tg setup` sets them
itself.

| Variable | Required | What it is |
|---|---|---|
| `TG_API_ID` | yes | app id from my.telegram.org |
| `TG_API_HASH` | yes | the hash from the same place |
| `TG_BOT_TOKEN` | for alerts | bot token from @BotFather |
| `TG_ALERT_CHAT_ID` | for alerts | where to send; filled in by `tg link-bot` |
| `TG_ALLOW_WRITE` | no (1 by default) | `0` — read-only mode |
| `TG_DATA_DIR` | no | where the state lives; `./data` by default, `/data` in docker |
| `TG_ENV_FILE` | no | a non-standard location of `.env` itself |

`TG_API_ID` and `TG_API_HASH` are the MTProto keys. Without them only the Bot API
is available, that is, you see exactly what was written to the bot and nothing
from your own chats.

Get a **separate** bot for the agent. The daemon hard-ignores messages from the
bot whose token is in its config — otherwise an alert flies back in as an
incoming message and causes the next alert, up to FloodWait.

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
question would loop. And `tg_stories`, `tg_scheduled` and `tg_sessions` count as
writing only because of a single argument: without `mark_read`, `cancel_ids` and
`terminate` such a call is an ordinary read, and no question is asked.

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

## Audio transcription

| Variable | What |
|---|---|
| `GROQ_API_KEY` | a key from console.groq.com/keys |
| `TG_WHISPER_ENGINE` | `auto` (default), `telegram`, `groq`, `local` |
| `TG_GROQ_MODEL` | `whisper-large-v3-turbo` by default |
| `TG_WHISPER_MODEL` | the local model, `mlx-community/whisper-large-v3-turbo` by default |
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
uv sync --extra local-whisper      # mlx-whisper on Apple Silicon + faster-whisper
```

The first run downloads the weights (turbo is ~1.6 GB) into the Hugging Face
cache. After that everything is computed on the machine, nothing goes out. For
weak hardware pick a smaller model:
`TG_WHISPER_MODEL=mlx-community/whisper-small`.

## Multiple accounts

The daemon holds all signed-in accounts at once — one Telethon client per
account, all in a single process, because the session must still have one owner.

```bash
uv run tg login --account work      # add a second one
uv run tg accounts                  # which ones exist
uv run tg call dialogs '{"limit":5}' --account work
uv run tg logout --account work     # remove
```

The label determines the session file name: `main` → `data/session.session`,
`work` → `data/session-work.session`. The `api_id`/`api_hash` keys are shared, a
separate `.env` is not needed.

In MCP: `tg_accounts` shows the list, `tg_account_use` switches the current one.
The switch only takes effect in this client session — another Claude session will
go on working with its own account.

The watcher listens to **all** accounts at once; an `account` field is written
into `events.jsonl` and into `actions.jsonl`, and a label is added to the alert if
the message arrived in a non-main account. Alert throttling is counted per
(account, chat) pair, so that the same chat in two accounts does not mute itself.

Write limits, meanwhile, are shared across the process: 60 messages an hour is the
total, not 60 per account. That is by design, otherwise the "will not spam"
guarantee breaks by adding a second session.


## Autostart (macOS)

The daemon can be hung on launchd, so that it comes up together with the system:

```bash
sed -i '' "s|/Users/YOUR_USER|$HOME|g" com.tgagent.daemon.plist
cp com.tgagent.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tgagent.daemon.plist
```

To unload it again — `launchctl unload ~/Library/LaunchAgents/com.tgagent.daemon.plist`.
In docker this is not needed: there the role of autostart is played by `restart: unless-stopped`.

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
      "chat": -1001162943519,
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
/status        the agent's state
/unread        unread
/actions       what the agent did: the last ten writing calls
/rules         the current rules
/watch <chat>  alert on all messages of a chat
/mute <chat>   do not alert about a chat
/pause         switch alerts off
/resume        switch them back on
```

`/pause` lives in the daemon's memory and is reset on restart; `/watch` and
`/mute` are written into `rules.json` and outlive it.

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
