# MCP tools

70 tools. Each one is a thin wrapper over a core method; all the logic is in
`tgagent/core.py`.

## Accounts

### `tg_accounts()`
Which accounts the daemon holds and which one your calls go to. There can be
several accounts: `uv run tg login --account work` sets up a second one.

### `tg_account_use(account)`
Send all further calls to this account (`main` is the primary one). The switch
lives only in the current client session: other clients are untouched, and the
background watcher listens to all accounts at once either way.

## How a chat is specified

Everywhere a parameter is called `chat`, any of these is accepted:

- a numeric id — `222222222`, `-1002474960404`
- `@username`
- a link `https://t.me/username`
- the exact chat title
- `me`, `saved`, `favorites` — Saved Messages

If the title fits several chats, the tool **does not guess**: it returns an error
with the list of candidates and their ids:

```
3 chats match 'Mark': Marketing (id -1001111111111),
Mark Petrov (id 222222222), Market — classifieds (id -1003333333333).
Pass the exact id.
```

## Account structure

### `tg_structure(sample=0)`
A map of the account in one call: how many chats of each type are in the main
list and in the archive, unread, pinned, every folder with its contents.
`sample` is how many chats to show as examples from each folder. Start here when
the question is about the shape of the account, not about a particular
conversation.

### `tg_folders()`
Telegram folders in detail: what is pinned inside a folder, explicitly included
chats, exclusions, auto-rules (all contacts, all groups, hide read), `total`.

### `tg_dialogs(limit=30, unread_only=False, archived=False, query=None, kind=None)`
A list of chats, freshest first, with unread counts and links.
`archived`: `false` — the main list, `true` — the archive, `null` — both folders.
`kind`: `user` / `bot` / `group` / `channel`. `kind="group"` is the answer to
"which groups am I in". `query` filters by title.

### `tg_status()`
Who is signed in, daemon uptime, how many alerts have been sent, whether writing
is allowed, the current rules, pid.

## Reading

### `tg_unread(limit_chats=20, per_chat=5, archived=None)`
Everything unread, grouped by chat, with the latest incoming messages. Every
chat is marked as archived or not. By default it looks at **both** folders.

### `tg_history(chat, limit=40, before_id=None, from_user=None, search=None, topic=None)`
One chat's conversation, oldest to newest. `before_id` pages deeper, `topic`
reads a single forum thread (the id comes from `tg_topics`).

### `tg_history_batch(chats, limit=20, search=None)`
Up to 25 chats in one call. An error in one chat does not bring down the rest —
it comes back in that chat's row. This is the right way to read several chats; a
loop over `tg_history` makes 25 trips where one is enough.

### `tg_search(query="", chat=None, limit=30, kind=None, since=None, until=None, tag=None)`
Search across the whole correspondence; with `chat`, inside one chat.

### `tg_saved_tags()`
Saved Messages labels and how many messages sit under each. The name from there
goes into `tg_search(chat="me", tag=...)`. A label with no name is looked up by
its emoji or by the id of the custom emoji — Telegram allows those too.

### `tg_mentions(limit=20, kind="mentions")`
Unread mentions and replies to you in groups and channels.

### `tg_events(limit=50, since=None)`
What the daemon's watcher caught. `since` is the lower time bound, ISO
(`2026-08-14T09:00:00+00:00`). Every incoming message is written down, not only
the ones that raised an alert — so it can be asked about after the fact.

### `tg_message(chat, message_id, context=0, replies=0)`
One message in full: the reactions, the buttons under it, how many people read
it, plus `context` neighbouring messages before and after and `replies` answers
from the thread. Telegram gives the read counter only in small groups and only
for the first few days — if there is none, the field is simply absent.

Reactions arrive in two fields: `reactions` — a summary of the form
`[{"emoji": "🔥", "count": 1, "mine": true}]`, and `reacted_by` — **who exactly**
put it and when. Telegram does not give the by-name list everywhere: in large
channels it is closed and only the counters remain. For your own messages there
is also `read_at` — whether the other person has read it.

### `tg_drafts()`
Every unsent draft on the account, with the chats they are attached to.

### `tg_scheduled(chat, limit=30, cancel_ids=None)`
A chat's scheduled messages. With `cancel_ids` it cancels them instead of showing
them.

### `tg_activity(since="today", until=None, limit_chats=100, kind=None, include_own=True, per_chat=0)`
Where the correspondence actually went on over a period. Unlike `tg_unread`, the
chats that are already read and the ones where only you wrote land here too —
which is why this is the right start for a digest of the day.

Per chat: how many messages in all, how many incoming and outgoing, the time of
the first and of the last, whether it is archived. `since` understands `today`
(midnight in your local time, not in Greenwich), an ISO date and an offset of the
form `-6h`. `per_chat` adds sample messages.

It looks at the archive as well: on a live account that comes to 52 chats and
1672 messages a day out of 320 dialogs examined.

### `tg_export(chat=None, chats=None, limit=1000, format="json", dest=None, since=None, until=None, media=False, media_max_mb=50)`
Export of a correspondence to a file, up to 5000 messages per chat, in
chronological order. `json` is for parsing, `markdown` and `text` for reading.

`chats` exports up to 25 chats at a time, and an error in one does not bring down
the rest. `since="today"` limits the period. `media=true` downloads every
attachment into a folder next to the transcript, and then every message gains:

- `file` — the path on disk, the name, the size, the mime type and the
  attachment type;
- `links` — the links from the text, including the ones hidden behind the
  caption;
- `link` — a link to the message itself. It exists only in channels and
  supergroups: in a private conversation Telegram has no message link at all, and
  substituting `t.me/username/id` there is not allowed — it leads somewhere else.

Files larger than `media_max_mb` are not downloaded but listed in
`skipped_large`.

The pairing with `tg_activity` is what closes "give me all of today's
conversations in full": first the list of chats for the day, then their ids as a
list in `chats`.

## Links

Every message read now carries two extra fields:

```json
"links": ["https://t.me/durov (Durov)"],
"preview": {"url": "...", "site": "GitHub", "title": "...", "description": "..."}
```

`links` collects both bare addresses and the ones hidden behind text. Telegram
counts entity offsets in UTF-16 — in the code they are converted through
surrogates, otherwise any emoji in a message shifts the slicing and the link
arrives truncated.

The chat's tab of all links is `tg_media` with `kind="link"`: the addresses plus
the preview card.

### `tg_resolve(link)`

What is behind a link, without opening it and without joining:

| Link | What it returns |
|---|---|
| `t.me/username`, `@username` | type (user/bot/group/channel), id, title, subscribers, description |
| `t.me/+hash`, `t.me/joinchat/...` | the title of the private chat, the number of members, whether you are already in it, whether a request is required |
| `t.me/channel/123`, `t.me/c/.../123` | the chat plus the message itself |
| `t.me/addstickers/name` | a sticker pack |
| any external address | marked as external — that is a job for a web tool, not for Telegram |

## Stories

### `tg_stories(peer=None, mark_read=False, download=False, limit=20)`

Without `peer` — the general feed: who has a story right now. With `peer` — one
person's stories. Returns the id, the date, the expiry, the caption, the media
type, the view count for your own, the "close friends only" flag and the reaction
you gave.

Viewing the list is invisible to the author. Only `mark_read=True` makes it
visible — which is why it is off by default and is turned on solely at the
owner's direct request. `download=True` puts the media on disk;
`tg_view(chat=person, story_id=id)` shows a photo story to the model itself.

## Summaries

### `tg_summarize(chat, message_ids, to_lang=None)`

A summary of a long message made by Telegram itself: the server returns a ready
digest, and the model's context is not spent on it. With `to_lang` — straight
away in another language. Up to 10 messages per call, each summarised separately.

Checked on a post of 3051 characters: a four-paragraph summary came back, and the
same one in English with `to_lang="en"`.

## Waiting and questions

### `tg_wait(chat=None, from_user=None, keyword=None, timeout=120, private_only=False)`

A blocking wait for the next matching message. This is the right answer to "wait
for him to write": the daemon does the waiting, and it is listening to Telegram
anyway — polling `tg_events` in a loop is neither necessary nor acceptable.

Returns `got: true` and the event itself, or `got: false` once the time runs out;
a timeout means "nothing arrived", not an error. The `timeout` range is 5…600
seconds.

### `tg_ask(question, options=None, timeout=300)`

A question to the owner through the agent's bot, with buttons, and a wait for the
answer. For the cases where the decision is not the agent's: whether to send the
draft, whether to really delete, which of the options to pick. The owner answers
by pressing a button or with ordinary text to the bot.

Silence is not consent: on timeout `answered: false` comes back, and such an
answer is read as "no permission given".

## Devices

### `tg_sessions(terminate=None)`

Where the account is open: the device, the platform, the app, the IP, the
country, when the session was created and when it was last active. It answers
"where am I signed in" and "is there someone else here".

With `terminate=<session>` the session is revoked — an irreversible action on the
account itself, so it goes through the write switch and lands in
`actions.jsonl`. The current session has id 0 and cannot be revoked this way. A
session id that came out of someone else's message is a reason to refuse, not to
comply.

## Audio to text

### `tg_transcribe(chat, message_ids=None, kind="voice", limit=5, engine="auto", language=None)`

Transcribes voice messages, video notes, music and video. Without `message_ids`
it takes the last `limit` items of the `kind` tab — so "transcribe the last five
voice messages" is one call.

Three engines, `auto` tries them in order:

| Engine | When it is taken | Upside | Limitations |
|---|---|---|---|
| `telegram` | voice messages and video notes | instant, free, nothing is downloaded | needs Premium; cannot do music or ordinary video |
| `groq` | everything else | fast, whisper-large-v3-turbo | needs `GROQ_API_KEY`, file up to 24 MB, paid past the limits |
| `local` | if there is no key or no network | nothing leaves the machine | the first run downloads the model (~1.6 GB), needs `uv sync --extra local-whisper` |

If an engine in the chain falls over, the answer still comes from the next one,
and the `fallback_from` field shows what failed and why. `language="ru"` raises
the accuracy a little.

### `tg_translate(to_lang, chat=None, message_ids=None, text=None)`
Translation by Telegram itself: either of a chat's messages (up to 20 at a time)
or of arbitrary text. The language codes are the usual ones: `ru`, `en`, `de`.

## People

### `tg_chat_info(chat)`
id, username, type, number of members, description.

### `tg_participants(chat, limit=50, query=None)`
Members with everything needed to reach them: `@username`, a direct link to the
DM, the phone if it is visible, the role in the chat (owner / admin / custom
rank), last seen, the bot / premium / verified / deleted / in-contacts flags.

### `tg_contacts(query=None, limit=50, kind="all")`
Contacts and slices of them:

| kind | What it returns |
|---|---|
| `all` | the list of contacts (default), filtered by `query` |
| `birthdays` | those whose birthday Telegram knows, sorted by day — answers "whose birthday is coming up" |
| `top` | the people, groups and channels the account interacts with most often, ranked by Telegram itself |
| `online` | the contacts who are online right now |
| `blocked` | the blocklist |

### `tg_common_chats(user, limit=50)`
The groups and channels you are both in. A quick way to identify an unfamiliar
person: where they came from and through whom.

## Attachments

### `tg_media(chat, kind="media", limit=30, before_id=None)`
The attachment tabs, as in Telegram itself:

```
photo  video  media (photo+video)  file  music  voice
round  gif  link  pinned  geo  contact
```

Returns message ids, file names, sizes, mime types, duration, the artist for
music. The order of work is: first the list and an estimate of the volume, then
the download — do not pull a chat blindly.

There are twelve tabs, and all of them are checked on a live account: `photo`,
`video`, `media`, `file`, `music`, `voice`, `round`, `gif`, `link`, `pinned`,
`geo`, `contact`. A video note is identified as `round`, not as ordinary video —
Telethon returns it in both, so the order of the checks in the code matters.

### `tg_view(chat, message_id=None, size="preview", story_id=None)`
**Show a picture to the model.** It returns neither a path nor a description but
the image itself — the agent sees it. That is how "what is in this photo", "what
is on the screenshot", "read the text from the picture" are answered.

`preview` takes Telegram's ready preview, no wider than 1280 px (cheap in
context), `full` takes the original. For video, GIFs and documents a preview
frame is returned if there is one; if there is no preview, the tool honestly says
to download the file.

With `story_id` it is not a message that is looked at but a story: `chat` is
whose, `story_id` is which one (the list comes from `tg_stories`). Viewing
through this tool is invisible to the author.

### `tg_download(chat, message_id, dest=None)`
One attachment. Into `data/downloads` by default.

### `tg_download_many(chat, message_ids, dest=None)`
Up to 50 files at a time, the ids come from `tg_media`.

## Writing

Everything listed below goes through `_assert_write()` (the `TG_ALLOW_WRITE`
switch) and `RateGuard`, and every call is written to `data/actions.jsonl`.

| Tool | What it does |
|---|---|
| `tg_send(chat, text, reply_to=None, silent=False)` | a message in your name, up to 4096 characters |
| `tg_send_file(chat, path, caption="", voice=False, silent=False)` | a file; a list of paths in `path` goes as one album, `voice=true` — as a voice message |
| `tg_send_location(chat, latitude, longitude)` | a point on the map |
| `tg_schedule(chat, text, when, reply_to=None)` | send later: `+30m`, `+2h`, `2026-08-17T09:00` |
| `tg_draft(chat, text=None, reply_to=None, clear=False)` | a draft instead of a send |
| `tg_poll(chat, question, options, multiple=False, quiz_answer=None, anonymous=True)` | a poll or a quiz, 2–10 options |
| `tg_react(chat, message_id, emoji=None, big=False)` | a reaction; without `emoji` — remove your own. `emoji` accepts a character, the id of a custom emoji (Premium) or a list of up to three. If the chat does not allow every reaction, the error lists the allowed ones |
| `tg_click(chat, message_id, button=None)` | a bot's buttons: without `button` show them, with it press one |
| `tg_edit(chat, message_id, text)` | edit something of yours already sent |
| `tg_delete(chat, message_ids, revoke=True)` | deletion; `revoke` erases it for everyone, irreversibly |
| `tg_forward(from_chat, message_ids, to_chat)` | forwarding |
| `tg_mark_read(chat, clear_mentions=True, unread=False)` | drop the unread badge, or `unread=true` — put it back as a reminder |
| `tg_mute(chat, hours=None, unmute=False)` | mute for N hours or forever |
| `tg_archive(chat, undo=False)` | into the archive and back |
| `tg_pin(chat, unpin=False)` | pin a chat at the top of the list |
| `tg_pin_message(chat, message_id, unpin=False, notify=False)` | pin a message inside a chat |
| `tg_folder_edit(folder, add=None, remove=None)` | put chats into a folder or take them out |
| `tg_block(user, unblock=False)` | block a person |
| `tg_contact_edit(phone, name, last_name="", user=None, delete=False, note=None)` | add a contact by number, delete one, or leave a private note about a person (`note`) that only the owner sees |

### A draft instead of a send

`tg_draft` is the right answer to "write it, but I will look at it first". The
draft is saved on Telegram's servers and is visible in all your clients; you send
it yourself with one tap. Nothing goes outside, and the sending quota is not
spent.

### Groups and channels

| Tool | What it does |
|---|---|
| `tg_create_group(title, users=None, kind="group", about="")` | create a supergroup or a channel and invite people right away |
| `tg_invite(chat, users=None, link=False, revoke=False)` | add members and/or get an invite link |
| `tg_moderate(chat, user, action)` | `kick`, `ban`, `unban`, `promote`, `demote`, and also `approve` / `decline` — the answer to a join request |
| `tg_chat_edit(chat, title=None, about=None, photo=None)` | title, description, avatar |
| `tg_leave(chat, delete=False)` | leave a group or a channel |

`tg_leave` for a private chat requires an explicit `delete=true` and erases the
correspondence on your side irrevocably — by default the tool refuses and
explains why.

Besides the title, the description and the avatar, `tg_chat_edit` can do:

```
slowmode: 30        pause between a member's messages, 0 turns it off
forum: true         turn on topics (a forum) in a supergroup
permissions: {...}  default rights of the members
```

The rights keys: `send_messages`, `send_media`, `send_stickers`, `send_gifs`,
`send_polls`, `embed_links`, `change_info`, `invite_users`, `pin_messages`,
`manage_topics`. The value `true` means "allowed" — inside it is inverted into
Telegram's prohibitions, on the outside the logic is direct.

### Forums

| Tool | What it does |
|---|---|
| `tg_topics(chat, limit=50, query=None)` | the threads: id, title, unread, closed, pinned |
| `tg_topic_create(chat, title, icon_emoji_id=None)` | a new thread |
| `tg_topic_edit(chat, topic_id, title, closed, hidden, pinned)` | rename, close, hide, pin |

Telegram does not let you rename a topic and close it in one request, so the tool
splits the changes into separate calls by itself.

### Stickers and GIFs

`tg_stickers(scope, set=None, limit=60)` — `sets` (the installed packs), `set`
(the contents of one pack by `short_name`), `faved`, `recent`, `gifs`.

`tg_send_sticker(chat, scope, set, index, emoji, reply_to)` — send one by its
number in the list or by emoji (`emoji="🎉"` picks a matching one from the
favourites).

### Administration and bots

| Tool | What it does |
|---|---|
| `tg_admin_log(chat, limit=50, query="", admins=None)` | the log: who banned, deleted, renamed, and when |
| `tg_bot_info(bot, lang_code="")` | the name, the "about", the description and the commands of your own bot |
| `tg_bot_edit(bot, name, about, description, commands, lang_code)` | change them |
| `tg_cache_clear(downloads=False)` | drop the cache of chat titles, and optionally clear the downloads |

The list of commands (`commands`) Telegram allows to be changed only with the
bot's own token, so it works for this agent's bot; for the rest there is
@BotFather.

## Reactions

They are visible from three sides:

- **in any message read** — the `reactions` field with the summary by emoji;
- **by name** — `tg_message` returns `reacted_by`: who put what and when. In
  large channels Telegram closes the list, and only the counters remain there;
- **unread ones** — `tg_mentions(kind="reactions")`: which of your messages were
  reacted to while you have not seen it yet.

**At the moment it is put**, a reaction is caught by the daemon's watcher. A
reaction is not a new message and does not arrive as an ordinary event, so the
daemon listens to a separate `UpdateMessageReactions` update and takes only the
reactions to **your** messages: for other people's, Telegram sends them in
batches across the whole chat. An event of the form
`{"kind": "reaction", "from": "Lena", "emoji": "🔥", ...}` lands in
`events.jsonl` (that is, it is available through `tg_events`), wakes `tg_wait`
and, if the `alert_on_reaction` rule is turned on, arrives as an alert in the
bot. By default the rule is off: there are usually plenty of reactions, and
waking the phone with every little flame is not what anyone wants.

Removing a reaction is not written to the log. Your own reaction will not get
there either: for the actions of this same session Telegram sends no update — just
as it sends none for your own sent messages.

Checked on a live reaction: a person put a ❤ on the owner's message in a DM, and
an event with the author, the emoji, the message text and the counters landed in
`events.jsonl`.

## Notifications and folders

### `tg_notify(chat=None, scope=None, mute=None, hours=None, sound=None, previews=None, stories=None, exceptions=False)`

Without arguments — what the defaults are for private chats, groups and
channels. With `chat` — the settings of one chat, and with changes it changes
them. `scope` (`users` / `groups` / `channels`) changes the default of a whole
category: that is exactly "turn off notifications for all channels".

`sound=false` — notify silently, `previews=false` — do not show the text in the
notification, `stories=false` — do not notify about stories, `hours` — mute for a
while. `exceptions=true` lists the chats whose settings differ from the default
(on a live account there turned out to be 108 of them).

`tg_mute` remains the short path for "mute the chat"; `tg_notify` is for
everything else.

### `tg_folder_edit(folder=None, add=None, remove=None, create=None, delete=False, rename=None, emoji=None, rules=None, exclude=None)`

Folders as a whole, not only what fills them:

| What | How |
|---|---|
| create | `create="Work"`, plus `emoji`, `rules`, `add` |
| delete | `folder=<id or title>`, `delete=true` — the chats stay, only the folder disappears |
| rename | `rename="New one"` (up to 12 characters), `emoji` changes the icon |
| fill | `add` / `remove` — chats, `exclude` — exclusions |
| auto-rules | `rules={"groups": true, "exclude_muted": true}` |

The auto-rules are the same checkboxes as in the app: `contacts`,
`non_contacts`, `groups`, `broadcasts`, `bots`, `exclude_muted`, `exclude_read`,
`exclude_archived`. Anything not passed is left alone.

## Alerts

### `tg_alert(text)`
A message to the owner through the agent's bot. This is the right way to
"warn" — not writing to Saved Messages and not sending yourself a DM.

### `tg_rules(patch)`
Changes the daemon's auto-alert rules, merged over the current ones. The keys and
the values are in [configuration.md](configuration.md#alert-rules).

## What is deliberately missing

The MTProto map (layer 227) is wider than these 70 tools, and part of it is
deliberately not taken — that is a decision, not a gap:

| What | Why not |
|---|---|
| Stars, gifts, payments, withdrawals (`payments.*`) | these are operations with money; the agent does not do them even when asked |
| Publishing stories, changing the name, the bio, the avatar, the emoji status | the owner's public face is changed by the owner, not by the agent on its own initiative |
| Telegram Business settings (opening hours, away messages, chatbots) | tools for a business account, not for a personal assistant |
| Fact-checking, sponsored messages, channel statistics | they need special rights and are not needed for personal tasks |
| Secret chats | Telethon has no implementation of the crypto layer, only raw requests; half-finished E2E support is worse than none |
| Calls and group calls | a WebRTC stack is needed and there is none here; signalling without media is useless |
| An arbitrary call of any MTProto method | such a "universal" tool cancels out the whole point of the limits and the audit: anything at all passes through it, including what the other tools forbid |
| Changing privacy, 2FA, deleting the account | irreversible operations on the account itself; their place is in the owner's hands |

The single exception among "operations on the account" is `tg_sessions`: seeing
where the account is open is useful precisely to an assistant, while revoking a
session is left as an explicit action under the write switch and with a record in
the audit.

## Outside MCP

The same set is available from the terminal — handy for debugging and for
scripts:

```bash
uv run tg call structure '{"sample": 3}'
uv run tg call history '{"chat": "me", "limit": 5}'
uv run tg call media '{"chat": "Photos", "kind": "photo", "limit": 10}'
```

The method names match the tool names without the `tg_` prefix, except
`tg_unread` → `unread` and `tg_pin` → `pin`.
