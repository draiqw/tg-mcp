# MCP tools

79 tools. Each one is a thin wrapper over a core method; all the logic is in
`tgagent/core.py`.

## What the agent can do

The agent does not only read the correspondence, it also **looks** at pictures (`tg_view`
returns the image itself) and **listens** to sound: voice messages, video notes, music and
video are transcribed by Telegram's built-in transcription, through Groq Whisper or by a
local model. Long posts are retold by Telegram itself (`tg_summarize`), stories are read
without leaving a trace, and `tg_wait` and `tg_ask` let the agent wait for the message it
needs or ask the owner for permission right in the bot.

Going through the inbox is not the same as going through the unread: `tg_pending` shows
the conversations that broke off — who was never answered and who never answered, the
read-and-forgotten ones included, which the unread counter no longer knows about.
`tg_person` collects a dossier on a person in one call: profile, flags, shared chats,
place in the top of your correspondents, the history of the DM. `tg_memory` keeps a
standing dossier on a chat, so that an unfamiliar conversation does not have to start with
a thousand messages of history.

The daemon also does what needs no Claude running at all: alerts about important incoming
messages into your own bot, a digest on a schedule (`digest_at`), mail-style inbox filters
(mark read, archive, mute, move to a folder, move to Saved Messages) and reminders that
survive a restart. Auto-replies are deliberately missing from the filter actions: a rule
works unsupervised, and it must not be able to write to an outside person.

For the chats the owner names, a local full-text index is built (`tg_index`, sqlite +
FTS5): `tg_search(engine="local")` then searches instantly and can do what the server-side
search cannot do at all — filter by author, the slice "everything from this person over
this period", ranking by relevance and highlighting of the match.

## Accounts

### `tg_accounts(access=True)`
Which accounts the daemon holds and which one your calls go to. Every row: the
label, who is behind it (name, id, `@username`, the last digits of the phone),
whether there is Premium, whether the account is active for this client right
now, whether it is the one recorded as the default, and the files that belong to
it alone — session, index, dossiers.

Two "defaults" are named by different fields and must not be confused: `using` is
where this client's calls go now, `default` is what is written on disk and
survives a restart. They diverge only after a one-off `tg_account_use`.

`access=True` (the default) adds each account's access level: Premium and the
"available/blocked" counters. That is one request to Telegram per account, and
the answer is cached for ten minutes; `access=false` returns the list alone.

The `add` field holds a ready-made command for a second account — the agent
cannot sign it in, the owner types the code and the password.

### `tg_account_use(account, persist=False)`
Send further calls to this account (`main` is the primary one).

Without `persist` the switch lives only in the current client session: other
clients are untouched, and after Claude restarts everything goes back to the
default. With `persist=true` the choice is written to `data/settings.json` and
becomes the account every client starts from and the one that survives a daemon
restart — that is for "work from the work account from now on", not for a single
errand.

The background watcher listens to all accounts at once either way.

A one-off question to a neighbouring account needs no switch: `tg_capabilities`
has an `account` parameter. Which account you are writing from is visible in
`tg_accounts`, and every writing tool names the account in its own answer.

## How a chat is specified

Everywhere a parameter is called `chat`, any of these is accepted:

- a numeric id — `222222222`, `-1001111111111`
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

Two `kind` values return not ordinary dialogs but separate Telegram slices:

- `kind="inactive"` — groups and channels where nothing has happened for a long
  time; exactly the list Telegram itself offers when cleaning up subscriptions;
- `kind="saved"` — the subfolders of Saved Messages (see
  `tg_history(saved_from=...)` below): the name of the original author, how much
  is saved from them, the last message and its date. The counter has to be
  fetched by a separate request per subfolder — in the dialog list Telegram
  sends only the last message, not the count.

### `tg_status()`
Who is signed in, daemon uptime, how many alerts have been sent, whether writing
is allowed, the current rules, pid. In `digest` — the state of the scheduled
digests: the last deadline handled, the time of the last send and the start of
the period for the next one.

The `confirm_*` keys in the rules are always shown from disk, not from the
daemon's memory: the confirmation mode is edited by file and takes effect
immediately, so a status showing a stale `off` would lie in the least convenient
direction.

### `tg_limits(full=False)`

The access level on Telegram's side: whether the account has Premium and which
caps follow from that. The server keeps almost every limit as a pair, "ordinary
account / Premium", and returns them in `help.getAppConfig`, so the same action
on different accounts runs into different numbers, and they cannot be guessed.
Every limit in the answer carries all three values: `default`, `premium` and
`value` — the one in force here and now.

The limits picked are the ones this project's features run into: folders and
chats per folder, message and caption length, pinned chats (in the list, in the
archive, in Saved Messages), the number of groups and channels, public links,
upload file size (in 512 KB parts), the number of reactions on one message,
favourite stickers and GIFs, the length of the profile bio, the size of the
similar-channels list. Separately there are single values with no pair: how many
free transcriptions a week an account without Premium is entitled to, whether
translation is allowed, from which boost level transcription works in a group,
how many different reactions fit on one message, how many topics can be pinned
in a forum.

`full=True` adds every "ordinary/Premium" pair found in the configuration and
its remaining keys with their values (nested lists and tables are replaced by a
note about the type). This is the search mode: needed when a limit is being
looked for, not when a known one is being checked.

The local part of the access level does not belong here — whether writing is
allowed, whether the bot is configured, which transcription keys exist, that is
`tg_status`. The assembled answer to "what is available to me and why not" is
`tg_capabilities`.

### `tg_capabilities(chat=None, account=None, all_accounts=False)`

What is available to the agent, what is not, and what has to be done for it to
become available. It answers two readers at once: the owner after signing in —
"what level do I have and what did I get", and the model before acting — "is
this possible at all". Asking is cheaper than calling a tool and parsing its
error.

The answer starts with the bottom line: how many tools out of the total are
available, how many are blocked and for which reasons. Then the details, laid
out by the four natures of restriction — they must not be mixed, because each is
cured differently:

| Nature | What it is | How it is cured |
|---|---|---|
| subscription | Telegram Premium | bought; there is no local way around it |
| server cap | numbers from `help.getAppConfig` | not curable, but worth knowing in advance |
| local setting | `TG_ALLOW_WRITE`, keys in `.env`, the bot, `confirm_writes`, the `local-whisper` extra | the owner fixes it in a minute |
| rights in the chat | admin status, chat restrictions, slowmode | depends on the chat and does not carry over to another |

Every unavailable tool in the answer carries both a reason and an action:
"Premium required", "add `OPENAI_API_KEY` to `.env`", "turn on
`TG_ALLOW_WRITE=1`", "admin rights in this chat required". Tools where only part
of the work writes (`tg_stories` with `mark_read`, `tg_scheduled` with
`cancel_ids`, `tg_sessions` with `terminate`, `tg_memory` with
`action="update"`) are marked as reduced, not blocked: reading with them still
works.

Limits are taken from Telegram's answer, not out of thin air, and are shown as a
pair of numbers: how many are allowed now and how many there would be at the
other subscription level — "20 folders, without Premium it would be 10". That is
a fact about this particular account, not an advertisement for the subscription,
and the limits Premium does not move are listed separately.

The local part is checked by fact, not by intent: a key in `.env` is either there
or not (key values never go into the answer — only "set"/"not set"), the bot is
either linked or merely created, the `local-whisper` extra is either installed or
not — and that is asked of the interpreter itself, not read from
`pyproject.toml`. The case where writing is allowed but `confirm_writes` is on
without a configured bot is handled separately: there is nobody to ask for
permission, so writing is in fact locked.

`chat` adds the breakdown of one chat: the role (creator, admin, member,
restricted), whether you can write there and why not, which reactions are
allowed, whether slowmode is on, which rights are granted and which are taken
away from everyone. This is the **only** part of the answer that needs a request
about a particular chat, which is why without `chat` no such request is made: the
account level does not depend on the chat, and paying for it with an extra call
to Telegram is pointless.

`account` asks about a neighbouring account without switching into it: a one-off
question should not require `tg_account_use` there and back.

`all_accounts=true` compares every signed-in account at once. This is needed
because Premium is bought per account: the promise "I will forward a
two-gigabyte file" is true for exactly one of them. Per account it shows what
differs between them (the subscription, the tools that depend on it, the server
caps), while the shared half of the setup — the write permission, the
notification bot, the keys in `.env`, the alert rules — appears once in the
`shared` field: it belongs to the installation, not to a Telegram session.
Rights in a chat are not analysed in this mode: they depend on the account, and
one answer for all of them would be untrue.

The same answer in human wording is printed by `uv run tg capabilities`, by the
tail of `tg login`, by `tg setup` and the `tg init` wizard, and by the bot
command `/can` (with an argument — about a particular chat).

### A refusal instead of a raw Telegram error

A restriction discovered in the middle of the work is the same restriction, and
it has to be named the same way: by its reason and its way out, not by the name
of an exception. From `ChatAdminRequiredError` the model will not work out what
to do next; from "admin rights are required, ask the chat owner to grant them" it
will. So the server's typical answers are translated in one place (the tables in
`tgagent/capabilities.py`), and the translation itself happens on the way out of
the daemon: every call passes through it, and a second place for this is not
needed. That is how missing rights are explained, and a required subscription, a
chat's ban on that reaction, a folder or pin limit hit, a caption that is too
long, a file that does not fit into one upload, and waits (`FLOOD_WAIT`,
slowmode) — with the number of seconds, because "wait" without a number is half
an answer. The translation key is the Telegram code (`CHAT_ADMIN_REQUIRED`), not
the Telethon class: the server adds codes faster than the library adds classes
for them, and an unfamiliar error arrives as a string that contains the code
anyway. An error with no explanation goes up as it was, together with the class
name: an invented reason is worse than raw text.

Some refusals are known before going to the network, and then the call does not
go at all. The account properties this follows from — the Premium flag and the
caps from `help.getAppConfig` — the daemon keeps in memory for ten minutes.
Asking for them before every action would mean adding a request to every call;
remembering them until restart is not allowed — the daemon runs for weeks, and a
freshly bought subscription would be waiting for it to restart.

The advance check refuses only where the answer is known in full, and that
matters more than the check itself. Built-in transcription is rejected if there
is no subscription **and** the account is not entitled to free transcriptions
(`tg_limits`, the key `transcribe_audio_trial_weekly_number` equals zero): with a
non-zero counter it works without Premium too, and forbidding it would be a lie.
For `tg_react` without a subscription, a second reaction on a message and a
custom emoji are rejected in advance — those are properties of the account, not
of the chat, and knowing them does not require asking about the chat. If a
property could not be established (no network, the configuration did not
arrive), the call goes as before and gets Telegram's answer: "we are not sure, so
we will not try" is the worst refusal of all.

## Reading

### `brief`

Six tools take it: `tg_unread`, `tg_history`, `tg_history_batch`, `tg_search`,
`tg_mentions`, `tg_person`. It drops what surrounds a message — reactions, the
link preview card, the exact minute of an edit — and keeps what a message is:
who, when, what was said, what was attached, what it replies to.

It exists because the agent pays by the token for every answer, and the same
call means two different things. Scanning forty chats for what matters, the
trimmings are pure cost; reading one conversation closely, they are the point.
Measured on a real account: about a fifth off `tg_search`, a quarter off
`tg_unread`, and under a tenth off a long `tg_history` — which is text, and text
is what was asked for.

Off by default deliberately. A tool that quietly returns less than it promises
is worse than an expensive one, and only the caller knows which of the two jobs
this is. See [what the answer costs](architecture.md#what-the-answer-costs).

### `tg_unread(limit_chats=20, per_chat=5, archived=None, brief=False)`
Everything unread, grouped by chat, with the latest incoming messages. Every
chat is marked as archived or not. By default it looks at **both** folders.

### `tg_pending(limit=30, direction="theirs", min_age_hours=0, kind=None, archived=None, include_bots=False)`
Conversations left hanging: whom you have not answered and who has not answered
you. It looks not at the unread counter but at whose message is last in the chat
(`out`) — which is why, unlike `tg_unread`, the read-and-forgotten lands here.
Open a chat and it disappears from `tg_unread` forever, although you still have
not replied; `tg_pending` holds on to it.

| direction | What it shows |
|---|---|
| `theirs` | the last message is incoming — the debt is yours (default) |
| `mine` | the last one is yours and no answer came — the debt is theirs |
| `both` | both slices in one list, each row with its own `direction` |

Sorted by age, oldest first: the top row is the most neglected conversation. A
row carries the age in hours, `unread`/`read`, the author of the last message
(`last_from`, `"you"` for your own), a fragment of the text, a link and whether
the chat is archived.

`min_age_hours` cuts off the fresh ones: `24` or `48` leave only what has had
time to go stale. `kind` narrows down to a single type and at the same time
cancels the default filtering.

Channels are always thrown out, except with an explicit `kind="channel"`: in a
broadcast the last message is incoming by definition, and the whole list would
degenerate into a feed of subscriptions. Bots are thrown out by default for the
same reason (a bot's last message is almost always an unanswered notification),
but come back with `include_bots=True`. Saved Messages is thrown out too: notes
to yourself are not a debt.

On a live account: 147 chats with the debt on you, 115 with the debt on the
other side, the oldest one hanging for 39,455 hours (since February 2022). Out
of 323 dialogs, 48 channels, 12 bots and Saved Messages were filtered out.

### `tg_history(chat, limit=40, before_id=None, from_user=None, search=None, topic=None, saved_from=None, brief=False)`
One chat's conversation, oldest to newest. `before_id` pages deeper, `topic`
reads a single forum thread (the id comes from `tg_topics`).

`saved_from` works only with `chat="me"` and reads one subfolder of Saved
Messages. What is forwarded to yourself Telegram stores not as a flat feed but
laid out by original author: everything forwarded from the "News" channel lies
in the "News" subfolder, what you wrote to yourself lies in a subfolder with your
own name. The list of subfolders is `tg_dialogs(kind="saved")`; the name or id
from there is what goes into `saved_from`. In other chats this cut does not
exist, so with any `chat` other than `"me"` the parameter returns an error rather
than emptiness. The answer additionally carries `total` — how much is saved from
this author in total, not only on this page. The message identifiers in the
answer are ordinary Saved Messages ids; they can be passed to `tg_message`,
`tg_view` and `tg_forward`.

### `tg_history_batch(chats, limit=20, search=None, brief=False)`
Up to 25 chats in one call. An error in one chat does not bring down the rest —
it comes back in that chat's row. This is the right way to read several chats; a
loop over `tg_history` makes 25 trips where one is enough.

### `tg_search(query="", chat=None, limit=30, kind=None, since=None, until=None, tag=None, engine="server", author=None, brief=False)`
Search across the whole correspondence; with `chat`, inside one chat.

`engine` chooses where to search. `server` (the default) is the ordinary
Telegram search: it sees every chat and is always current, but it has exactly as
many filters as the client screen does. `local` searches the local index built by
`tg_index` (below): instant, ranked by relevance, with the match highlighted and
with a filter by author, but only over the chats the owner has indexed by hand.

There is deliberately no separate tool for local search. Two similar tools with
different behaviour would be confused by the model more often than chosen
correctly; one tool with an explicit switch forces a conscious choice.

What only `local` can do:

- `author` — whose messages these are, by a substring of the name; `author="me"`
  is your own;
- a slice with no query at all: `query=""` plus `author`, `since`, `until`,
  `kind` — that is "everything from Sophia in March", not a search for a word;
- stemming both ways: `sozvonilis` ("we had a call") finds both `sozvon` and
  `sozvonitsya`;
- `arend*` — search by the beginning of a word, for when the stem does not help;
- `score` in the answer (bm25, higher is more relevant) and `match` — the piece
  of text with the match in `**asterisks**`;
- `total` — how many matched in all, not only how many fitted into `limit`.

The `local` answer is an object, not a list: besides `messages` it carries the
state of the index. An empty answer always explains itself — "the index is
empty", "this chat is not in the index" or "no matches, the index holds so many
chats". Silent emptiness is the most dangerous thing here: it makes "there was no
such conversation" indistinguishable from "this chat is not indexed".

`tag` works only with `engine="server"`: Saved Messages labels live on the
Telegram server and never reach the index. `author` is the opposite — `local`
only.

The server, contrary to expectation, does have morphology: it finds
`dogovorimsya` by `dogovorilis`, and `napisat` by `napishu`, which Snowball
cannot do. Checked live, which is why `engine="server"` stayed the default. The
local index wins on other forms (`sozvonilis` → `sozvon`) and on everything that
is out of the server's reach in principle: filters, ranking, counters and working
without a network.

### `tg_index(action="sync", chats=None, since=None, limit=None)`
The local full-text index of the correspondence — what
`tg_search(engine="local")` searches. It lives in `data/index.db` (sqlite +
FTS5, mode 600).

- `action="sync"` — download and index. Incrementally: for every chat the
  boundaries of what is already downloaded are remembered, so a repeat call costs
  exactly the messages that appeared since last time (three chats, nothing new —
  one second).
- `action="status"` — what is inside: the chats, how many messages in each, over
  which period, when they were synced, the file size and its permissions.
- `action="drop"` — tear it down. Without `chats` — the whole file, with
  `chats` — only the named chats (after which a `VACUUM` runs so that the text
  leaves the disk instead of lying around in the freed pages).

Nothing gets indexed by itself. The first `sync` for a chat has to name it
explicitly; a `sync` without `chats` only refreshes what the owner has already
set up. That is not an inconvenience but a boundary: the index puts the
correspondence on disk in a parsable form, and a human makes that decision, not
the agent in the middle of a task.

`limit` is how many messages to pull per chat in one call (2000 by default,
20000 maximum). It also means "dig deeper": without it and without `since` only
fresh messages are fetched, otherwise every call would go back through the
history to the very beginning of the correspondence. `since` (`today`, ISO,
`-30d`) limits how far back to dig.

Syncing is interruptible. The call stops itself after about a hundred seconds
(the MCP client waits no longer than 120 for the daemon) and says
`stopped: "budget"`, every 300 messages are committed to the database, and a
`FloodWait` does not put the call to sleep for half an hour but ends it with the
mark `flood_wait:Ns`. In all three cases a repeat `sync` continues from the same
boundary — the only thing that can be lost is the last messages not yet fetched,
never what has already landed.

What goes into the index is the text, the author, the date, the chat and message
ids and the attachment type. The files themselves are not stored: a message with
an attachment and no caption gets `[voice]`, `[photo]`,
`[document:contract.pdf]` as its text — that way attachments are found with
ordinary words. The details of how this differs from reading through the API, and
how to tear it down, are in [security.md](security.md#local-message-index).

### `tg_saved_tags()`
Saved Messages labels and how many messages sit under each. The name from there
goes into `tg_search(chat="me", tag=...)`. A label with no name is looked up by
its emoji or by the id of the custom emoji — Telegram allows those too.

### `tg_mentions(limit=20, kind="mentions", brief=False)`
Unread mentions and replies to you in groups and channels.

### `tg_events(limit=50, since=None)`
What the daemon's watcher caught. `since` is the lower time bound, ISO
(`2026-08-14T09:00:00+00:00`). Every incoming message is written down, not only
the ones that raised an alert — so it can be asked about after the fact.

### `tg_actions(limit=50, since=None, method=None, chat=None)`
The log of what the agent itself did: every writing call lies in
`data/actions.jsonl` with the time, the account, the method, the parameters, the
first 400 characters of the text, an `ok` flag and the error text if it did not
go through. This is the other side of `tg_events`: there, what happened in
Telegram; here, what was done in it on your behalf.

It is needed for two things. The first is a report to the owner: "what did you
send" is answered from the log, not from the agent's memory. The second is
working out what happened after a failure: failed calls are recorded too, with
their error text, so it is visible what did not go through and why.

`since` understands `today`, ISO (`2026-08-17T09:00`) and an offset backwards
(`-6h`, `-3d`) — as in `tg_activity`. `method` filters by the name of the action
(`send`, `delete`), `chat` by where the action was aimed, as a substring of the
value passed. Records go in ascending order of time, new ones at the end.

Reading calls do not reach the log: the point of the audit is what changed, not
that someone looked at the history. There is one exception — `tg_index`: it
changes nothing in the account, but it puts the correspondence on disk, and the
owner has to see that.

Besides the agent's calls, [inbox filter](configuration.md#inbox-filters) hits
land here too — they get an `auto` field with the name of the rule. So the log
shows both what the agent did and what the automation did while nobody was
around.

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

If the message is a poll, a `votes` field is added: the question, the flags
(`anonymous`, `quiz`, `multiple`, `closed`), `total_voters`, `options` with
counters and `my_vote` — what you voted for yourself. In an open poll
(`anonymous: false`) every option gains a `by` — **who exactly** chose it and
when; with multiple choice one person lands in several options at once.

Boundaries, checked live:

- in an **anonymous** poll the by-name list does not exist for anyone, the author
  included. Telegram rejects a request for the votes as `MESSAGE_ID_INVALID`, so
  the core does not go into such a poll at all and writes straight into `note`
  that only the counters exist;
- the server returns no more than fifty voters at a time. If more people voted,
  `voters_truncated: true` is set, and `voters_listed` says how many rows
  actually arrived;
- a poll that hides its results until you vote has no counters at all until you
  vote yourself — that is not an error but a Telegram rule, and it too is
  explained in `note`.

### `tg_drafts()`
Every unsent draft on the account, with the chats they are attached to.

### `tg_scheduled(chat, limit=30, cancel_ids=None)`
A chat's scheduled messages. With `cancel_ids` it cancels them instead of showing
them.

### `tg_activity(since=None, until=None, limit_chats=100, kind=None, include_own=True, per_chat=0, chat=None, limit_days=120)`
Where the correspondence actually went on over a period. Unlike `tg_unread`, the
chats that are already read and the ones where only you wrote land here too —
which is why this is the right start for a digest of the day.

Per chat: how many messages in all, how many incoming and outgoing, the time of
the first and of the last, whether it is archived. `since` with no value means
`today`, that is midnight in your local time, not in Greenwich; it also
understands an ISO date and an offset of the form `-6h`, `-30d`. `per_chat` adds
sample messages.

It looks at the archive as well: on a live account that comes to 52 chats and
1672 messages a day out of 320 dialogs examined.

**With `chat` the axis changes.** The question is the same — "when did the
correspondence happen" — but not by chat over a period, rather by day inside one
chat over its whole history. This is the answer to "when did we actually talk" and
"in which period was this discussed", and the history is not downloaded: the
server computes the per-day breakdown. Every day carries `min_id` and `max_id` —
with them `tg_history(before_id=...)` jumps straight into that day.

`since` in this mode with no value means the whole history, not today. The
boundaries are cut by UTC days (that is how the server groups them), and a day
touched by a boundary even partly stays whole. `limit_days` caps the number of
days in the answer, days go from fresh to old.

About accuracy, this is worth knowing. The real Telegram calendar
(`messages.getSearchResultsCalendar`) counts days only for a single attachment
type: to "all messages" (`InputMessagesFilterEmpty`) the server answers
`FILTER_NOT_SUPPORTED` — checked on Saved Messages, a one-to-one conversation, an
ordinary group, a supergroup and a channel. Hence two modes:

| Call | How it is counted | Accuracy |
|---|---|---|
| `tg_activity(chat=X)` | sparse message positions (`messages.getSearchResultsPositions`), up to 2000 points per history | exact if the chat holds no more than ~2000 messages; a sample otherwise |
| `tg_activity(chat=X, kind="photo")` | that same calendar, over the attachments tab | always exact |

In the exact case the answer carries `exact: true`. When there are more messages
than points, `sampled_every` and `note` appear: the count was taken roughly every
Nth message, so a day may be overstated by up to N messages, and a day with fewer
messages than the step may not make the list at all. On a live chat of 11,599
messages the step came out at 6: a day with 38 messages was shown as 41, and days
with a single message were dropped. For "exactly how many" there is `kind`: the
photo calendar in the same chat gave 103 days and 4678 messages — exactly as many
as `counters.photo` in `tg_chat_info`.

`kind` in this mode is not the type of dialog (there is nothing to filter, there
is one chat) but the type of attachment: `photo`, `video`, `media`, `file`,
`music`, `voice`, `round`, `gif`, `link`, `geo`, `pinned`, `contact`.

### `tg_export(chat=None, limit=1000, format="json", dest=None, chats=None, since=None, until=None, media=False, media_max_mb=50)`
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

What is behind a link (or behind a phone number), without opening it and without
joining:

| Link | What it returns |
|---|---|
| `t.me/username`, `@username` | type (user/bot/group/channel), id, title, subscribers, description |
| `t.me/+hash`, `t.me/joinchat/...` | the title of the private chat, the number of members, whether you are already in it, whether a request is required |
| `t.me/channel/123`, `t.me/c/.../123` | the chat plus the message itself |
| `t.me/addstickers/name` | a sticker pack |
| `+79991234567` | the person behind the number: id, name, @username, a link to the DM, last seen |
| any external address | marked as external — that is a job for a web tool, not for Telegram |

A number is recognised only with an explicit plus (`+7...`; spaces, brackets and
hyphens are allowed): bare digits are a chat id, not a phone. It goes through
`contacts.resolvePhone`, that is, **no contact is created** — unlike the old way
through `ImportContacts`, which for the same answer created an entry in the
address book and showed your number to the person you were checking. The answer
has `contact` — whether the person is already saved by you.

If no account is visible, the tool returns an explanation rather than an error
code. What matters is that there are two cases behind that error and Telegram
deliberately does not distinguish them: the number may have no Telegram at all,
or it may have "who can find me by number" turned off. There is nothing to tell
one from the other, and promising otherwise is wrong. Search by number is also
rate-limited — under flood control a request to wait comes back.

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

## Chat memory

### `tg_memory(chat=None, action="show", limit=None, model=None)`

A dossier on a chat: who these people are, what the conversation is about, what
has already been agreed. One markdown file per chat in
`data/memory/<chat id>.md`, written by a language model.

The file name is the chat id, not the title: a chat gets renamed, and the dossier
must neither get lost nor split in two. The title lies inside, in the
frontmatter, together with the `covered_to` boundary — the id of the last message
taken into account.

| action | What it does |
|---|---|
| `show` | read the dossier; without `chat` — a list of all that exist |
| `update` | update it, and create it if it does not exist yet |
| `list` | every dossier with its metadata |
| `drop` | delete this chat's dossier |

**An update builds on top, it does not retell from scratch.** What goes to the
model is the previous dossier plus only the messages that are not in it yet. So
the first pass costs the history and every one after that costs pennies, and the
dossier remembers what went over the horizon long ago. Measured on a live chat:
the first pass 6142 input tokens, the next one 747.

The sections are fixed: "What this chat is", "Who takes part", "What it is
usually about", "Agreements and facts", "Open questions". The dossier is read not
by a human but by the agent, right before it answers, and it needs a predictable
shape.

Two warnings, both material:

- **An update sends pieces of the correspondence outside** — to OpenAI by default
  (`OPENAI_API_KEY`, `TG_MEMORY_MODEL`, `gpt-4o-mini` by default). This is the
  only place in the whole agent where personal correspondence leaves the machine.
  By itself a dossier is not created for any chat.
- **The dossier is written from untrusted text.** Anything at all can be inside a
  correspondence, instructions to a model included. The prompt says outright that
  the content of the chat is data and not instructions, but the finished dossier
  deserves the same attitude: it is a retelling of someone else's words, not a
  command to the agent.

Auto-update is configured by the rules `memory_auto`, `memory_after`,
`memory_chats`, `memory_max_per_hour` — see
[configuration.md](configuration.md#alert-rules). The daemon does the counting:
it sees the stream of messages anyway, so it knows which chat has accumulated
enough new material. The update runs on a separate tick rather than in the
incoming-message handler — otherwise the watcher would start falling behind the
stream for the length of the network call to the model.

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

### `tg_remind(text=None, when=None, chat=None, unless_reply=False, list=False, cancel=None)`

A reminder to the owner in the bot after a given time. It answers "remind me in
two hours if Lena has not replied" and "at 18:00 remind me about the invoice" —
that is, everything `tg_wait` is not fit for: that one holds the call and lives
600 seconds at most, while here nobody holds anything.

`when` is `+2h`, `+30m`, `+3d` or an absolute `2026-08-18T09:00` (a naive time is
taken as local), the same parsing as in `tg_schedule`. The reminder lies in
`data/reminders.json` and survives a daemon restart; the tick runs every half
minute, so do not expect it to the second. If the daemon was down at the
deadline, the reminder arrives at startup — late, but not silently.

`unless_reply=true` together with `chat` cancels the reminder by itself if an
incoming message arrived from that chat (or from that person) before the
deadline. The point is not to wake the owner where the question has already
resolved itself. A reaction does not count as a reply: the question was asked in
text, so text is what is waited for. Without `chat` this flag is meaningless and
is rejected.

`list=true` shows the active reminders with their ids and how long is left,
`cancel=<id>` removes one. Creating and cancelling land in `actions.jsonl`
(`tg_actions`): a reminder survives a restart and will wake the owner on its own,
and that must not happen unnoticed. Showing the list is reading and does not go
into the audit.

It goes to the owner alone and only through the agent's bot; nobody else is sent
anything when it fires.

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
| `telegram` | voice messages and video notes | instant, free, nothing is downloaded | needs Premium (without it — only within the free transcription counter); cannot do music or ordinary video; **clears the "not listened" flag** |
| `groq` | everything else | fast, whisper-large-v3-turbo | needs `GROQ_API_KEY`, file up to 24 MB, paid past the limits |
| `local` | if there is no key or no network | nothing leaves the machine | the first run downloads the model (~1.6 GB), needs `uv sync --extra local-whisper` |

If an engine in the chain falls over, the answer still comes from the next one,
and the `fallback_from` field shows what failed and why — by reason, not by the
name of an exception: engine errors are explained in words the same way the
errors of the calls themselves are (see "A refusal instead of a raw Telegram
error"). The `telegram` engine on an account without a subscription and without
free transcriptions is cut off before the request — with `engine="auto"` the
chain simply starts from the next one. `language="ru"` raises the accuracy a
little.

**Transcription by the `telegram` engine is visible to the sender.**
`messages.TranscribeAudio` clears the `media_unread` flag on the server, that is,
for the other person the voice message is marked as listened to although nobody
listened to it. Checked by experiment: a voice message arrived with
`unlistened: true`, and after transcription by the built-in engine it became
`false`; downloading the same file does not touch the flag, which is why `groq`
and `local` are invisible and `telegram` is not.

This concerns the watcher too: with `transcribe_voice: true` it transcribes every
incoming voice message that raised an alert, that is, private correspondence
first of all. The behaviour was deliberately left on by default — instant text in
the notification matters more than the flag — but it is a conscious choice, not
an accident. It is turned off by the rule `transcribe_voice: false`.

Your own state is visible in the `unlistened` field of any message read: it is
there as long as an incoming voice message, video note or video has not been
consumed.

### `tg_translate(to_lang, chat=None, message_ids=None, text=None)`
Translation by Telegram itself: either of a chat's messages (up to 20 at a time)
or of arbitrary text. The language codes are the usual ones: `ru`, `en`, `de`.

## People

### `tg_chat_info(chat, counters=True, similar=False)`
id, username, type, number of members, description.

`counters` — how much of what lies in the chat: `photo`, `video`, `file`,
`music`, `voice`, `round`, `gif`, `link`, `geo`, `pinned`. The server does the
counting (`messages.getSearchCounters`), all the filters go in one request and
the history is not downloaded — on a live channel that is +0.05–0.1 s to the
answer, which is why it is on by default instead of hidden behind a flag. Zeros
are not shown: a missing key is the zero. This is the answer to "how much is
there to download at all" before `tg_download_many`.

Calls are deliberately absent from the list: for a particular interlocutor the
server rejects `InputMessagesFilterPhoneCalls` (`PEER_ID_NOT_SUPPORTED`) — calls
in Telegram live in a list of their own, not inside a chat.

`similar` — the channels similar to this one ("what else to subscribe to on the
topic"), `channels.GetChannelRecommendations`. It works for channels only: a
group, a supergroup and a person return an `error`. A subscription to the channel
is not needed, and someone else's public one can be asked about as well. The list
is sometimes empty — Telegram simply does not have something to offer for every
channel.

About truncation. It was checked on a Premium account, and there the full list
arrived: 86–95 channels, with no truncation mark. It is known that without
Premium Telegram returns only the beginning of the list, sending it as a slice
with the full number inside; in that case `total` and `truncated` appear in the
answer. That branch itself has not been checked on a non-Premium account — there
was nothing here to check it on.

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

### `tg_person(user, messages=20, chats=10, brief=False)`
A dossier on a person in one call — what `tg_chat_info`, `tg_contacts`,
`tg_common_chats`, `tg_history` and `tg_resolve` used to be pulled one after
another for. The point is context: before writing to someone, the agent should
know everything about them that the account knows, and not spend five answers on
it.

What is inside:

| Field | What is there |
|---|---|
| `profile` | id, name, `@username`, a direct link to the DM, the phone if it is visible, the bio, online or when last seen |
| `profile` (flags) | `bot`, `premium`, `verified`, `scam`, `fake`, `deleted`, `contact`, `mutual_contact`, `blocked`, `me` — the false ones are not returned at all |
| `profile.birthday` | if Telegram gives it: `05.02.2003`, or `01.12` when the year is hidden |
| `profile.note` | the private note about a contact — only the account owner sees it, the other person knows nothing about it |
| `common_chats_count`, `common_chats` | how many chats you share and which exactly (`chats` of them) |
| `top_rating` | the place in the top of interlocutors: Telegram's own rating, the same one that lifts people in search. No row means not in the top |
| `conversation` | the last `messages` messages of the private correspondence, `total` — how many there are in all, `since` and `first_text` — what it started with |

`messages=0` removes the texts but keeps the counters; `chats=0` skips the
request for common chats.

`since` is the date of the oldest **remaining** message: a cleared history shifts
it, so it is the start of the correspondence, not of the acquaintance.

**A boundary.** There is no global search of messages by author in MTProto —
Telegram does not expose such a method. So "what he wrote" here means the private
correspondence only. What a person wrote in a shared group is read separately and
one chat at a time: `tg_history(chat=<group>, from_user=<him>)`.

Not a person, not a dossier: for a channel or a group the tool answers with an
error and sends you to `tg_chat_info`.

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

If [write confirmation mode](configuration.md#write-confirmation-mode) is on, one
more question to the owner in the bot stands between the call and Telegram, and
without an "allow" answer the call returns the error "the owner did not
confirm". The check sits in the daemon, so it works the same for every writing
tool — including the ones described in other sections of this page: `tg_notify`,
`tg_stories` (with `mark_read`), `tg_scheduled` (with `cancel_ids`),
`tg_sessions` (with `terminate`), `tg_remind`, `tg_rules`. Only `tg_alert` and
`tg_ask` are excluded: they are the question channel itself, and confirming it
with a question would be a loop.

| Tool | What it does |
|---|---|
| `tg_send(chat, text, reply_to=None, silent=False)` | a message in your name, up to 4096 characters |
| `tg_send_file(chat, path, caption="", voice=False, silent=False)` | a file; a list of paths in `path` goes as one album, `voice=true` — as a voice message |
| `tg_send_location(chat, latitude, longitude)` | a point on the map |
| `tg_schedule(chat, text, when, reply_to=None)` | send later: `+30m`, `+2h`, `2026-08-17T09:00` |
| `tg_draft(chat, text=None, reply_to=None, clear=False)` | a draft instead of a send |
| `tg_poll(chat, question, options, multiple=False, quiz_answer=None, anonymous=True)` | a poll or a quiz, 2–10 options |
| `tg_react(chat, message_id, emoji=None, big=False)` | a reaction; without `emoji` — remove your own. `emoji` accepts a character, the id of a custom emoji (Premium) or a list of up to three (also Premium: without a subscription Telegram accepts one reaction, and both of these attempts are rejected before the chat is contacted). If the chat does not allow every reaction, the error lists the allowed ones |
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
| `tg_invites(chat, link=None, limit=50, revoked=False)` | the chat's invite links and who came in through them |
| `tg_bot_info(bot, lang_code="")` | the name, the "about", the description and the commands of your own bot |
| `tg_bot_edit(bot, name, about, description, commands, lang_code)` | change them |
| `tg_cache_clear(downloads=False)` | drop the cache of chat titles, and optionally clear the downloads |

The list of commands (`commands`) Telegram allows to be changed only with the
bot's own token, so it works for this agent's bot; for the rest there is
@BotFather.

#### `tg_invites(chat, link=None, limit=50, revoked=False)`

The tool is a reading one, even though it stands among the administration ones:
it creates nothing and revokes nothing — a new link is issued by
`tg_invite(chat, link=true)`.

Without `link` — which links the chat has: the address, the title, whether it is
permanent, the usage limit and how much of it is used, the expiry, whether a join
request is required. The links shown are the ones created by you (Telegram
requires naming the administrator whose links you are asking about); who else in
this chat hands them out and how many they have is in the `admins` field.
`revoked=true` — the same about the revoked ones.

With `link` — by name, **who joined through exactly that one** and when: the
name, the @username, a link to the DM, the date, and for links with join requests
also who approved the request. Both the full address and the bare hash are
accepted — `+hash` is completed to `t.me/+hash` automatically. The link has to
belong to this chat: someone else's Telegram does not recognise, and the answer
that comes back is "there is no such link in this chat, or it has already been
revoked".

Admin rights are required — Telegram gives these lists only to those who can
manage invitations. Without the rights an explanation comes back rather than a
traceback, and text just as clear comes back for an attempt to ask about the
invitations of a private conversation, where there are none.

The practical point is to tie "a person arrived" to "through which link". A
link's usage counter counts only those who went through it themselves; the ones
added by hand through `tg_invite(users=...)` will not be in this list — they did
not join through a link.

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

The text that reaches the owner — alerts, digests, reminders and the questions
`tg_ask` puts into the bot — is written in the language of `TG_LANG` (`en` or
`ru`, `en` by default), the same setting that decides what `tg` prints in the
terminal. Values like `ru_RU.UTF-8` or `en-GB` are understood, and an unsupported
language falls back to `en`. The value is read on every call, so an edit to
`.env` takes effect without restarting the daemon. What the tools return to
Claude — fields, notes, explanations of errors — is English regardless: the
catalogue in `tgagent/i18n.py` holds both languages side by side and covers only
what the owner reads, and `scripts/selfcheck.py` checks its two halves for gaps.

### `tg_alert(text)`
A message to the owner through the agent's bot. This is the right way to
"warn" — not writing to Saved Messages and not sending yourself a DM.

### `tg_rules(patch)`
Changes the daemon's auto-alert rules, merged over the current ones. The keys and
the values are in [configuration.md](configuration.md#alert-rules).

The same tool configures the automation the daemon runs on its own, without a
running Claude: `digest_at` — the times of the digests in the bot
([digest](configuration.md#scheduled-digest)), `auto` — the inbox filters
([filters](configuration.md#inbox-filters)). They deliberately have no tools of
their own: this is a setting of the daemon, not an action of the agent. The
schedule and the filters are validated on save — a rule with a typo in the action
will not be saved silently.

The filter actions are a closed list: mark as read, archive, mute, put into a
folder, forward to Saved Messages. A rule cannot write to an outside person —
there is simply no such action.

The `confirm_*` keys — [write confirmation
mode](configuration.md#write-confirmation-mode) — do not go through this tool: an
attempt to change them returns an error. That is the owner's restriction on the
agent, not a setting of the agent, so it is edited by file only — as are the
limits.

## What is deliberately missing

The MTProto map (layer 227) is wider than these 79 tools, and part of it is
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
| Auto-replies in [inbox filters](configuration.md#inbox-filters) | a rule fires with no human present, and one typo in the condition would mean a message to an outsider. The filter actions are a closed list with no sending to live people in it at all: mark as read, archive, mute, into a folder, forward **to Saved Messages** |
| A separate tool for local search, for filters and for the digest | local search is `engine="local"` in `tg_search`, while filters and the digest are a setting of the daemon through `tg_rules`. Two similar tools with different behaviour would be confused by the model more often than chosen correctly, and automation that works without a running Claude needs no tool by definition |
| Changing `confirm_*` and `LIMITS` from MCP | these are the owner's restrictions on the agent; they are edited by file only, otherwise the agent can lift them off itself and they protect against an honest agent alone |

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
uv run tg call pending '{"direction": "theirs", "min_age_hours": 48}'
uv run tg call index '{"action": "status"}'
uv run tg call actions '{"since": "-6h"}'
```

The method names match the tool names without the `tg_` prefix. There is one
exception: `tg_resolve` calls the method `resolve_link`. `tg_account_use` does
have a method (`account_use`), but only a persistent choice reaches the daemon: a
one-off switch lives in the MCP server itself, because the daemon serves every
client at once and one client's choice must not change the behaviour of the rest.

The account for a one-off call is chosen with a flag:
`uv run tg call dialogs '{"limit":5}' --account work`. A permanent one —
`uv run tg accounts --default work`.
