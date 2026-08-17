# Security

There are two different documents here, do not mix them up:

- **this file** — what to do if you found a vulnerability, and what a user
  should worry about before running the agent;
- **[docs/security.md](docs/security.md)** — the full threat model: what is
  protected in the code and by what exactly, where the boundaries run, why a
  particular trade-off was accepted. Below is only a summary with links there.

## How to report a vulnerability

Do not open a public issue: a description of a hole in the tracker gets read
faster than a fix ships.

1. **Private vulnerability reporting** on GitHub —
   [Security → Report a vulnerability](https://github.com/draiqw/tg-mcp/security/advisories/new)
   in the `draiqw/tg-mcp` repository. This is the main channel.
2. If private reporting is unavailable — open an empty issue titled "security
   contact" with no details, and you will be told where to write.

What is useful to attach: the version (commit), how you run it (locally or
docker), what exactly you manage to do and what should not be possible. There is
no need to test anything on someone else's account — reproducing it on your own
is enough.

A reply within a week. The project is personal, there is no security on-call; no
SLA is promised, but a report will not quietly disappear.

## Supported versions

`main` is supported. There are no separate fix branches for older versions:
updating is `git pull`.

## What a user should know before starting

This is a wrapper around a **personal** account over MTProto, not a bot. Hence
four things you do not meet in ordinary Bot API projects:

**1. `data/session.session` is a sign-in to the account without a password and
without 2FA.** A copied file equals a stolen account: the cloud password will
not stop it, no SMS code will be asked for. The file is covered by `.gitignore`
and `.dockerignore`, but backups, syncing the directory to the cloud and copying
it to another machine are your responsibility alone. If it leaked —
`uv run tg logout` (revokes the session on the Telegram side) or Settings →
Privacy and Security → Devices, the session is named `claude-tg-agent`.

**2. `.env` holds the keys.** `TG_API_ID`/`TG_API_HASH` from the application,
the notification bot token, and — when the dossier and Groq are in use — keys
for external models. Permissions are 600, `tg setup` sets them itself. A leak of
`.env` does not let anyone sign in to the account, but it does let them take
over the notification bot completely. The token is revoked at @BotFather
(`/revoke`), `api_hash` is regenerated at my.telegram.org.

**3. The local index and the dossier put your correspondence on disk.**
`tg_index` writes message text into `data/index.db` (sqlite + FTS5), `tg_memory`
into `data/memory/*.md`. Everything else in the agent is ephemeral: messages
arrive in response to a call and settle nowhere. This does not. The index file,
which is enough to simply copy, is the whole indexed correspondence in full. No
chat is indexed on its own: both the index and the dossier are created only for
explicitly named chats, and every such call lands in `data/actions.jsonl`. To
wipe it: `uv run tg call index '{"action":"drop"}'`.

**4. The dossier sends correspondence to an external model.** `tg_memory` is the
only place where chat content leaves the machine: a dossier update sends the
previous dossier plus new messages to OpenAI (`OPENAI_API_KEY`).
`TG_MEMORY_BASE_URL` switches it to any API-compatible service, including a
local one — then the correspondence does not leave the machine. The second such
case is the `groq` engine in `tg_transcribe`, it too is enabled only
explicitly. Important: there is a second person in the chat, and they did not
sign up for this being sent anywhere.

Additionally, in brief:

- **The `data/daemon.sock` socket** has no authentication: any process of your
  user that opens it gets the full set of operations. The security boundary here
  is the OS user, and that is a deliberate choice.
- **Prompt injections.** The text of other people's messages is untrusted data.
  It is never interpreted by code; at the model level the defence is a section in
  the subagent prompts. That is not considered a guarantee; which is exactly why
  limits, audit and a trimmed-down tool set for the watcher sit underneath it.
- **Write guards**: `TG_ALLOW_WRITE=0` turns writing off entirely,
  `confirm_writes` asks the owner in the bot before every writing call, the caps
  (60 messages and 15 distinct chats per hour, 50 deletions) sit in the core.
  The agent cannot lift them through MCP — they are only edited by file.
- **Audit**: every writing action is written to `data/actions.jsonl` with the
  time, the method, the parameters and the result. After any incident, start
  there.

Details and rationale — in [docs/security.md](docs/security.md).

## What is not considered a vulnerability

- Access to the socket or to `data/` from processes of the same OS user — that
  is the declared boundary, not a hole.
- The fact that the agent can send a message when `TG_ALLOW_WRITE=1`. That is
  its purpose; what constrains it is the caps, the confirmation mode and the
  audit.
- The possibility of the model making a mistake after being tricked by chat
  content, with writing enabled. As a problem it is acknowledged as open in
  `docs/security.md`; what is worth reporting is a concrete bypass of a concrete
  guard — for example, a way to perform a writing call around `confirm_writes`
  or around `RateGuard`.
