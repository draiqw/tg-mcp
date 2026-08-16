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
  "quiet_hours": null
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

The order of the checks is this: pause and `enabled` → your own outgoing → your
own bot → `ignore_bots` → `mute_chats` → quiet hours → `keywords` →
`watch_chats` → mention → private. The first match decides, and the reason goes
into the alert text.

Note that `mute_chats` hits earlier than `keywords`, that is, a muted chat will
not wake you even by a keyword. This is done on purpose — "muted" must mean
"muted".

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
audit trail and it grows without rotation on purpose.
