"""Local full-text index of the correspondence: sqlite + FTS5 in data/index.db.

The module deliberately knows nothing about Telethon: it takes ready-made
message rows and hands back search results. History is downloaded by
`TelegramService.index()`; everything else lives here — where it is stored, how
it is searched and how it is wiped.

Why it exists at all: Telegram's server-side search is substring-based and has
no morphology — "dogovorilis" does not find "dogovorimsya", and "where did we
discuss the rent" does not work at all. Here the text goes into the index
twice: as it is (column `text` — exact form, phrases, prefixes) and normalized
(column `stems`). The query passes through the same normalizer, so any form of
a word finds any other.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The tokenizer was picked by checking, not from memory: on this machine's
# sqlite (3.50.4) `pragma compile_options` reports ENABLE_FTS5, and the table is
# created fine with unicode61, trigram, porter and ascii — all four verified.
# Taken: "porter unicode61 remove_diacritics 2":
#   - unicode61 splits text into words by unicode, that is, it understands
#     Cyrillic; remove_diacritics 2 also folds diacritics in Latin script;
#   - porter on top of it stems English words (meetings -> meeting) and leaves
#     Cyrillic alone — verified; Russian morphology is handled by stem_ru;
#   - trigram was rejected on purpose: it looks for an arbitrary substring, so
#     "kot" is found inside "skotcha", there is nothing to rank words by, and
#     the index is three times fatter. Substring search is exactly what the
#     Telegram server already does and exactly what we are moving away from.
TOKENIZER = "porter unicode61 remove_diacritics 2"

SCHEMA_VERSION = 1

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_CYRILLIC = re.compile("[\u0430-\u044f\u0451\u0410-\u042f\u0401]")


# ---------------------------------------------------------------- morphology

# A Russian stemmer following the Snowball algorithm. Written here instead of
# taken as a dependency: the package is not in the environment, and pulling it
# in for a hundred lines of regular logic, into a project with only four
# dependencies, is a bad trade.
#
# The suffix tables below are Cyrillic data, written as \u escapes so the
# source itself stays ASCII. The escapes are resolved by the parser, so the
# strings are exactly the letters they would be if typed directly: \u0430 is
# lowercase a, \u0451 is yo, \u0410 is capital A.

_VOWELS = "\u0430\u0435\u0438\u043e\u0443\u044b\u044d\u044e\u044f"


def _by_length(*groups: Iterable[str]) -> tuple[str, ...]:
    """Endings are tried from long to short: the longest one wins."""
    return tuple(sorted({s for g in groups for s in g}, key=len, reverse=True))


# Gerund: the first group only after "a" or "ya", the second one on its own.
_GERUND_1 = _by_length(("\u0432", "\u0432\u0448\u0438", "\u0432\u0448\u0438\u0441\u044c"))
_GERUND_2 = _by_length((
    "\u0438\u0432", "\u0438\u0432\u0448\u0438", "\u0438\u0432\u0448\u0438\u0441\u044c",
    "\u044b\u0432", "\u044b\u0432\u0448\u0438", "\u044b\u0432\u0448\u0438\u0441\u044c",
))
_REFLEXIVE = _by_length(("\u0441\u044f", "\u0441\u044c"))
_ADJECTIVE = _by_length((
    "\u0435\u0435", "\u0438\u0435", "\u044b\u0435", "\u043e\u0435", "\u0438\u043c\u0438",
    "\u044b\u043c\u0438", "\u0435\u0439", "\u0438\u0439", "\u044b\u0439", "\u043e\u0439",
    "\u0435\u043c", "\u0438\u043c", "\u044b\u043c", "\u043e\u043c", "\u0435\u0433\u043e",
    "\u043e\u0433\u043e", "\u0435\u043c\u0443", "\u043e\u043c\u0443", "\u0438\u0445",
    "\u044b\u0445", "\u0443\u044e", "\u044e\u044e", "\u0430\u044f", "\u044f\u044f", "\u043e\u044e",
    "\u0435\u044e",
))
_PARTICIPLE_1 = _by_length((
    "\u0435\u043c", "\u043d\u043d", "\u0432\u0448", "\u044e\u0449", "\u0449",
))
_PARTICIPLE_2 = _by_length(("\u0438\u0432\u0448", "\u044b\u0432\u0448", "\u0443\u044e\u0449"))
_VERB_1 = _by_length((
    "\u043b\u0430", "\u043d\u0430", "\u0435\u0442\u0435", "\u0439\u0442\u0435", "\u043b\u0438",
    "\u0439", "\u043b", "\u0435\u043c", "\u043d", "\u043b\u043e", "\u043d\u043e", "\u0435\u0442",
    "\u044e\u0442", "\u043d\u044b", "\u0442\u044c", "\u0435\u0448\u044c", "\u043d\u043d\u043e",
))
_VERB_2 = _by_length((
    "\u0438\u043b\u0430", "\u044b\u043b\u0430", "\u0435\u043d\u0430", "\u0435\u0439\u0442\u0435",
    "\u0443\u0439\u0442\u0435", "\u0438\u0442\u0435", "\u0438\u043b\u0438", "\u044b\u043b\u0438",
    "\u0435\u0439", "\u0443\u0439", "\u0438\u043b", "\u044b\u043b", "\u0438\u043c", "\u044b\u043c",
    "\u0435\u043d", "\u0438\u043b\u043e", "\u044b\u043b\u043e", "\u0435\u043d\u043e",
    "\u044f\u0442", "\u0443\u0435\u0442", "\u0443\u044e\u0442", "\u0438\u0442", "\u044b\u0442",
    "\u0435\u043d\u044b", "\u0438\u0442\u044c", "\u044b\u0442\u044c", "\u0438\u0448\u044c",
    "\u0443\u044e", "\u044e",
))
_NOUN = _by_length((
    "\u0430", "\u0435\u0432", "\u043e\u0432", "\u0438\u0435", "\u044c\u0435", "\u0435",
    "\u0438\u044f\u043c\u0438", "\u044f\u043c\u0438", "\u0430\u043c\u0438", "\u0435\u0438",
    "\u0438\u0438", "\u0438", "\u0438\u0435\u0439", "\u0435\u0439", "\u043e\u0439", "\u0438\u0439",
    "\u0439", "\u0438\u044f\u043c", "\u044f\u043c", "\u0438\u0435\u043c", "\u0435\u043c",
    "\u0430\u043c", "\u043e\u043c", "\u043e", "\u0443", "\u0430\u0445", "\u0438\u044f\u0445",
    "\u044f\u0445", "\u044b", "\u044c", "\u0438\u044e", "\u044c\u044e", "\u044e", "\u0438\u044f",
    "\u044c\u044f", "\u044f",
))
_SUPERLATIVE = _by_length(("\u0435\u0439\u0448", "\u0435\u0439\u0448\u0435"))
_DERIVATIONAL = _by_length(("\u043e\u0441\u0442", "\u043e\u0441\u0442\u044c"))


def _region_starts(word: str) -> tuple[int, int]:
    """The RV and R2 boundaries from the Snowball description, as positions in
    the original word.

    Positions, not substrings: afterwards the word is only shortened from the
    end, so boundaries computed once stay correct through all the steps.
    """
    rv = len(word)
    for i, ch in enumerate(word):
        if ch in _VOWELS:
            rv = i + 1
            break
    r1 = len(word)
    for i in range(1, len(word)):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r1 = i + 1
            break
    r2 = len(word)
    for i in range(r1 + 1, len(word)):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r2 = i + 1
            break
    return rv, r2


def _cut(word: str, endings: tuple[str, ...], start: int) -> tuple[str, str | None]:
    for suffix in endings:
        if word.endswith(suffix) and len(word) - len(suffix) >= start:
            return word[: -len(suffix)], suffix
    return word, None


def _cut_after_ay(word: str, endings: tuple[str, ...], start: int) -> tuple[str, str | None]:
    """The same, but the ending must be preceded by "a" or "ya" inside RV."""
    for suffix in endings:
        head = len(word) - len(suffix)
        if word.endswith(suffix) and head - 1 >= start and word[head - 1] in "\u0430\u044f":
            return word[:-len(suffix)], suffix
    return word, None


def stem_ru(word: str) -> str:
    """Russian stem of a word. "dogovorilis" and "dogovorimsya" -> "dogovor"."""
    word = word.replace("\u0451", "\u0435")  # yo -> ye
    rv, r2 = _region_starts(word)

    # Step 1: gerund, otherwise reflexive + adjective/verb/noun.
    stem, cut = _cut_after_ay(word, _GERUND_1, rv)
    if cut is None:
        stem, cut = _cut(word, _GERUND_2, rv)
    if cut is None:
        stem, _ = _cut(word, _REFLEXIVE, rv)
        after_adj, adj = _cut(stem, _ADJECTIVE, rv)
        if adj:
            after_part, part = _cut_after_ay(after_adj, _PARTICIPLE_1, rv)
            if part is None:
                after_part, part = _cut(after_adj, _PARTICIPLE_2, rv)
            stem = after_part
        else:
            after_verb, verb = _cut_after_ay(stem, _VERB_1, rv)
            if verb is None:
                after_verb, verb = _cut(stem, _VERB_2, rv)
            stem = after_verb if verb else _cut(stem, _NOUN, rv)[0]

    # Step 2: a dangling "i".
    if stem.endswith("\u0438") and len(stem) - 1 >= rv:
        stem = stem[:-1]

    # Step 3: the derivational "ost" — only inside R2.
    stem = _cut(stem, _DERIVATIONAL, r2)[0]

    # Step 4: "nn" -> "n", superlative, soft sign.
    if stem.endswith("\u043d\u043d"):
        stem = stem[:-1]
    else:
        after_sup, sup = _cut(stem, _SUPERLATIVE, rv)
        if sup:
            stem = after_sup[:-1] if after_sup.endswith("\u043d\u043d") else after_sup
        elif stem.endswith("\u044c"):
            stem = stem[:-1]
    return stem


def stem_token(token: str) -> str:
    """Cyrillic we stem ourselves, Latin we leave to the tokenizer's porter."""
    token = token.lower().replace("\u0451", "\u0435")  # yo -> ye
    return stem_ru(token) if _CYRILLIC.search(token) else token


def stems_of(text: str) -> str:
    """A normalized copy of the text — what goes into the stems column."""
    return " ".join(stem_token(w) for w in _WORD_RE.findall(text or ""))


# ---------------------------------------------------------------- query

def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def build_match(query: str) -> tuple[str, set[str], set[str]]:
    """User query -> an FTS5 expression, a set of stems and a set of prefixes.

    Words are joined with AND over the stems column (that is what gives
    morphology), and the whole phrase is additionally glued on with OR over the
    text column: the exact form gets an extra hit and therefore floats to the
    top in bm25. A word with a trailing asterisk is searched as a prefix over
    the original text — that is the only way to ask "starts with" when the stem
    does not help.
    """
    raw_tokens = [t for t in re.split(r"\s+", (query or "").strip()) if t]
    clauses: list[str] = []
    stems: set[str] = set()
    prefixes: set[str] = set()
    for raw in raw_tokens:
        if raw.endswith("*") and len(raw) > 1:
            base = "".join(_WORD_RE.findall(raw[:-1])).lower()
            if base:
                clauses.append("{text} : " + _quote(base) + "*")
                prefixes.add(base)
            continue
        words = _WORD_RE.findall(raw)
        for word in words:
            stem = stem_token(word)
            if stem:
                clauses.append("{stems} : " + _quote(stem))
                stems.add(stem)
    if not clauses:
        return "", stems, prefixes
    expr = " AND ".join(clauses)
    phrase = " ".join(_WORD_RE.findall(query))
    if phrase and stems:
        expr = f"({expr}) OR ({{text}} : {_quote(phrase)})"
    return expr, stems, prefixes


def highlight(text: str, stems: set[str], prefixes: set[str], window: int = 260) -> str | None:
    """A window around the first match, matched words wrapped in `**asterisks**`.

    Our own highlighting, not `highlight()` from FTS5: a match here is most
    often morphological, that is, the original text carries a different form of
    the word, and the built-in function simply would not have found it.
    """
    if not text or (not stems and not prefixes):
        return None
    hits = [
        m.span()
        for m in _WORD_RE.finditer(text)
        if stem_token(m.group(0)) in stems
        or any(m.group(0).lower().startswith(p) for p in prefixes)
    ]
    if not hits:
        return None
    start = max(0, hits[0][0] - window // 4)
    end = min(len(text), start + window)
    out: list[str] = []
    pos = start
    for a, b in hits:
        if b <= start or a >= end:
            continue
        out.append(text[pos:a])
        out.append("**" + text[a:b] + "**")
        pos = b
    out.append(text[pos:end])
    body = "".join(out)
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


# ---------------------------------------------------------------- attachments

# The type names are the same as `kind` in tg_media and in server-side search,
# but mapped onto the `_media_kind()` values that actually sit in the database.
MEDIA_KINDS: dict[str, tuple[str, ...]] = {
    "photo": ("photo",),
    "video": ("video",),
    "media": ("photo", "video"),
    "file": ("document",),          # document:filename lands here too, by prefix
    "music": ("audio",),
    "voice": ("voice",),
    "round": ("round",),
    "gif": ("gif",),
    "sticker": ("sticker",),
    "poll": ("poll",),
    "geo": ("location",),
    "contact": ("contact",),
    "link": ("link_preview",),      # plus text with a link, see _media_where
    "any": (),                      # any attachment
}


def _media_where(kind: str) -> tuple[str, list[Any]]:
    if kind not in MEDIA_KINDS:
        raise ValueError(
            f"kind must be one of: {', '.join(sorted(MEDIA_KINDS))}"
        )
    if kind == "any":
        return "m.media IS NOT NULL", []
    if kind == "link":
        # On the server "link" means a link in the text; the index only has the
        # attachment type, so a link in text is searched for as it is.
        return "(m.media = 'link_preview' OR m.text LIKE '%http%')", []
    parts, args = [], []
    for value in MEDIA_KINDS[kind]:
        parts.append("m.media = ?")
        args.append(value)
        if value == "document":
            parts.append("m.media LIKE 'document:%'")
    return "(" + " OR ".join(parts) + ")", args


# ---------------------------------------------------------------- storage

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS chats(
    chat_id   INTEGER PRIMARY KEY,
    name      TEXT,
    kind      TEXT,
    min_id    INTEGER,
    max_id    INTEGER,
    synced_at TEXT
);

CREATE TABLE IF NOT EXISTS messages(
    rowid     INTEGER PRIMARY KEY,
    chat_id   INTEGER NOT NULL,
    msg_id    INTEGER NOT NULL,
    ts        INTEGER,
    date      TEXT,
    from_id   INTEGER,
    from_name TEXT,
    from_low  TEXT,
    out       INTEGER,
    media     TEXT,
    text      TEXT,
    UNIQUE(chat_id, msg_id)
);

CREATE INDEX IF NOT EXISTS messages_chat_ts ON messages(chat_id, ts);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, stems,
    content='', contentless_delete=1,
    tokenize="{TOKENIZER}"
);
"""


class MessageIndex:
    """The index file and every operation over it. One file per account."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    # ---------- connection ----------

    def _connect(self) -> sqlite3.Connection:
        fresh = not self.path.exists()
        if fresh:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # The file is created with mode 600 right away instead of being
            # chmod'ed afterwards: otherwise there is a window between creation
            # and chmod in which the correspondence lies world-readable.
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        # The default journal (DELETE), not WAL: there is exactly one writer
        # here — the daemon — and WAL would leave an index.db-wal next to it
        # with the same text inside and with umask permissions.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        os.chmod(self.path, 0o600)
        return conn

    def exists(self) -> bool:
        return self.path.exists()

    # ---------- writing ----------

    def chat_state(self, chat_id: int) -> dict | None:
        if not self.exists():
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT chat_id, name, kind, min_id, max_id, synced_at FROM chats WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return dict(row) if row else None

    def add(self, chat_id: int, name: str, kind: str, rows: list[dict]) -> int:
        """Put a batch of messages in and move the chat boundaries.

        In batches, not one by one: synchronization can break off at any
        moment, and everything that made it to the previous commit must stay in
        the index.
        """
        if not rows:
            self._touch_chat(chat_id, name, kind, None, None)
            return 0
        added = 0
        conn = self._connect()
        try:
            with conn:
                for row in rows:
                    text = row.get("text") or ""
                    existing = conn.execute(
                        "SELECT rowid FROM messages WHERE chat_id=? AND msg_id=?",
                        (chat_id, row["msg_id"]),
                    ).fetchone()
                    values = (
                        chat_id, row["msg_id"], row.get("ts"), row.get("date"),
                        row.get("from_id"), row.get("from_name"),
                        (row.get("from_name") or "").lower() or None,
                        1 if row.get("out") else 0, row.get("media"), text,
                    )
                    if existing:
                        # The message could have been edited — we update both the
                        # text and the index: a contentless table would otherwise
                        # keep the old words.
                        rid = existing["rowid"]
                        conn.execute(
                            "UPDATE messages SET ts=?, date=?, from_id=?, from_name=?,"
                            " from_low=?, out=?, media=?, text=? WHERE rowid=?",
                            values[2:] + (rid,),
                        )
                        conn.execute("DELETE FROM messages_fts WHERE rowid=?", (rid,))
                    else:
                        cur = conn.execute(
                            "INSERT INTO messages(chat_id, msg_id, ts, date, from_id,"
                            " from_name, from_low, out, media, text)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?)",
                            values,
                        )
                        rid = cur.lastrowid
                        added += 1
                    conn.execute(
                        "INSERT INTO messages_fts(rowid, text, stems) VALUES (?,?,?)",
                        (rid, text, stems_of(text)),
                    )
                ids = [r["msg_id"] for r in rows]
                self._touch_chat(chat_id, name, kind, min(ids), max(ids), conn=conn)
        finally:
            conn.close()
        return added

    def _touch_chat(
        self, chat_id: int, name: str, kind: str,
        min_id: int | None, max_id: int | None,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        own = conn is None
        conn = conn or self._connect()
        try:
            now = datetime.now(UTC).isoformat()
            row = conn.execute(
                "SELECT min_id, max_id FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO chats(chat_id, name, kind, min_id, max_id, synced_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (chat_id, name, kind, min_id, max_id, now),
                )
            else:
                lo = min([v for v in (row["min_id"], min_id) if v] or [None])
                hi = max([v for v in (row["max_id"], max_id) if v] or [None])
                conn.execute(
                    "UPDATE chats SET name=?, kind=?, min_id=?, max_id=?, synced_at=?"
                    " WHERE chat_id=?",
                    (name, kind, lo, hi, now, chat_id),
                )
            if own:
                conn.commit()
        finally:
            if own:
                conn.close()

    # ---------- state ----------

    def status(self) -> dict:
        if not self.exists():
            return {"path": str(self.path), "exists": False, "chats": [], "messages": 0}
        conn = self._connect()
        try:
            chats = []
            for row in conn.execute(
                "SELECT c.chat_id, c.name, c.kind, c.min_id, c.max_id, c.synced_at,"
                "       COUNT(m.rowid) AS messages,"
                "       MIN(m.date) AS first_date, MAX(m.date) AS last_date"
                " FROM chats c LEFT JOIN messages m ON m.chat_id = c.chat_id"
                " GROUP BY c.chat_id ORDER BY messages DESC"
            ):
                chats.append({k: row[k] for k in row.keys()})
            total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        return {
            "path": str(self.path),
            "exists": True,
            "bytes": self.path.stat().st_size,
            "mode": oct(self.path.stat().st_mode & 0o777),
            "chats": chats,
            "messages": total,
        }

    def drop(self, chat_ids: list[int] | None = None) -> dict:
        """Wipe the index entirely or per chat.

        Entirely — by deleting the file, not with `DELETE FROM`: only that way
        both the text and the service pages leave the disk. Per chat, the
        delete is followed by VACUUM for the same reason — otherwise the erased
        messages stay lying in the freed pages of the file and can be read out
        by any hex editor.
        """
        if not self.exists():
            return {"dropped": "nothing", "existed": False}
        if not chat_ids:
            size = self.path.stat().st_size
            self.path.unlink()
            for tail in ("-journal", "-wal", "-shm"):
                side = Path(str(self.path) + tail)
                if side.exists():
                    side.unlink()
            return {"dropped": "all", "existed": True, "freed_bytes": size}
        conn = self._connect()
        try:
            removed = 0
            with conn:
                for chat_id in chat_ids:
                    rows = conn.execute(
                        "SELECT rowid FROM messages WHERE chat_id=?", (chat_id,)
                    ).fetchall()
                    for row in rows:
                        conn.execute("DELETE FROM messages_fts WHERE rowid=?", (row["rowid"],))
                    conn.execute("DELETE FROM messages WHERE chat_id=?", (chat_id,))
                    conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
                    removed += len(rows)
            conn.execute("VACUUM")
            left = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()
        return {
            "dropped": "chats", "chats": len(chat_ids),
            "messages_removed": removed, "messages_left": left,
            "bytes": self.path.stat().st_size,
        }

    # ---------- search ----------

    def search(
        self,
        query: str = "",
        chat_ids: list[int] | None = None,
        author: str | None = None,
        mine: bool | None = None,
        since_ts: int | None = None,
        until_ts: int | None = None,
        kind: str | None = None,
        limit: int = 30,
    ) -> dict:
        if not self.exists():
            return {"indexed": False, "total": 0, "messages": []}
        expr, stems, prefixes = build_match(query)
        where: list[str] = []
        args: list[Any] = []
        if chat_ids:
            where.append("m.chat_id IN (" + ",".join("?" * len(chat_ids)) + ")")
            args += list(chat_ids)
        if author:
            where.append("m.from_low LIKE ?")
            args.append(f"%{author.lower()}%")
        if mine is not None:
            where.append("m.out = ?")
            args.append(1 if mine else 0)
        if since_ts is not None:
            where.append("m.ts >= ?")
            args.append(since_ts)
        if until_ts is not None:
            where.append("m.ts <= ?")
            args.append(until_ts)
        if kind:
            clause, extra = _media_where(kind)
            where.append(clause)
            args += extra
        tail = (" WHERE " + " AND ".join(where)) if where else ""

        conn = self._connect()
        try:
            if expr:
                # No alias for the FTS table: MATCH and bm25() accept only the
                # real table name, to an alias sqlite answers "no such column".
                base = (
                    " FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
                    " WHERE messages_fts MATCH ?"
                    + ((" AND " + " AND ".join(where)) if where else "")
                )
                params = [expr] + args
                total = conn.execute("SELECT COUNT(*)" + base, params).fetchone()[0]
                # The text column weighs more than stems: if a message carries
                # exactly the form that was asked for, it must stand above the
                # messages with the same root in another form.
                rows = conn.execute(
                    "SELECT m.*, bm25(messages_fts, 3.0, 1.0) AS rank" + base
                    + " ORDER BY rank LIMIT ?",
                    params + [limit],
                ).fetchall()
            else:
                # An empty query is a slice by filters: "everything from Petya
                # for March".
                total = conn.execute(
                    "SELECT COUNT(*) FROM messages m" + tail, args
                ).fetchone()[0]
                rows = conn.execute(
                    "SELECT m.*, NULL AS rank FROM messages m" + tail
                    + " ORDER BY m.ts DESC LIMIT ?",
                    args + [limit],
                ).fetchall()
            names = {
                r["chat_id"]: r["name"]
                for r in conn.execute("SELECT chat_id, name FROM chats")
            }
            indexed_chats = len(names)
            indexed_messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        finally:
            conn.close()

        out = []
        for row in rows:
            item = {
                "chat_id": row["chat_id"],
                "chat": names.get(row["chat_id"]),
                "id": row["msg_id"],
                "date": row["date"],
                "from": row["from_name"],
                "from_id": row["from_id"],
                "out": bool(row["out"]),
                "media": row["media"],
                "text": row["text"],
            }
            mark = highlight(row["text"] or "", stems, prefixes)
            if mark:
                item["match"] = mark
            item = {k: v for k, v in item.items() if v not in (None, "", False)}
            item["id"] = row["msg_id"]
            if row["rank"] is not None:
                # Set after the empty fields are filtered out: bm25 is sometimes
                # exactly zero, and a zero would be dropped along with False, and
                # the relevance would disappear.
                item["score"] = round(-float(row["rank"]), 3)
            out.append(item)
        return {
            "indexed": True,
            "indexed_chats": indexed_chats,
            "indexed_messages": indexed_messages,
            "total": total,
            "messages": out,
            "stems": sorted(stems) or None,
        }
