"""Local index: morphology, query assembly, search and teardown."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tgagent.index import (
    MessageIndex,
    build_match,
    highlight,
    stem_token,
    stems_of,
)

# The words below are Cyrillic on purpose: the whole point of the module is the
# Russian stemmer, so the test data has to be Russian to exercise it at all.

# ---------------------------------------------------------------- morphology


@pytest.mark.parametrize(
    "a, b",
    [
        ("договорились", "договоримся"),
        ("встреча", "встречу"),
        ("аренда", "аренду"),
        ("квартиры", "квартире"),
        ("отправил", "отправила"),
        ("документы", "документ"),
        ("важный", "важная"),
        ("новости", "новость"),
        ("работает", "работать"),
        ("счёт", "счета"),          # yo is folded to ye
    ],
)
def test_different_forms_give_one_stem(a, b):
    """This is why the index exists at all: server-side search is substring
    based and knows nothing about morphology."""
    assert stem_token(a) == stem_token(b) != ""


@pytest.mark.parametrize(
    "a, b",
    [
        ("аренда", "работа"),
        ("встреча", "врач"),
        # a substring is not a match, unlike with trigram
        ("кот", "скотч"),
    ],
)
def test_different_words_do_not_glue_together(a, b):
    assert stem_token(a) != stem_token(b)


def test_latin_script_is_left_to_the_tokenizer():
    """English is handled by porter inside FTS5, our own stemmer does not touch
    it — otherwise the word would be stemmed twice, and differently in the text
    and in the query."""
    assert stem_token("Meetings") == "meetings"
    assert stem_token("Deadline") == "deadline"


def test_normalized_copy_of_the_text():
    assert stems_of("Договорились о встрече в 15:00 🙂") == "договор о встреч в 15 00"
    assert stems_of("") == ""
    assert stems_of(None) == ""


# ---------------------------------------------------------------- query


def test_words_are_joined_by_stems_and_the_phrase_lifts_the_exact_form():
    expr, stems, prefixes = build_match("аренда квартиры")
    assert stems == {"аренд", "квартир"}
    assert prefixes == set()
    assert '{stems} : "аренд"' in expr and '{stems} : "квартир"' in expr
    assert " AND " in expr
    assert '{text} : "аренда квартиры"' in expr and " OR " in expr


def test_asterisk_searches_a_prefix_over_the_original_text():
    expr, stems, prefixes = build_match("Дог*")
    assert prefixes == {"дог"} and stems == set()
    assert expr == '{text} : "дог"*'


def test_an_empty_query_gives_no_expression():
    assert build_match("") == ("", set(), set())
    assert build_match("   ")[0] == ""
    assert build_match("!!! ???")[0] == ""


def test_highlighting_finds_another_form_of_the_word():
    stems = {"договор"}
    out = highlight("Мы договорились о встрече завтра", stems, set())
    assert out == "Мы **договорились** о встрече завтра"
    assert highlight("ничего похожего", stems, set()) is None
    assert highlight("", stems, set()) is None
    assert highlight("любой текст", set(), set()) is None


def test_highlighting_cuts_a_window_around_the_match():
    text = "а" * 400 + " договор " + "б" * 400
    out = highlight(text, {"договор"}, set(), window=60)
    assert out.startswith("…") and out.endswith("…") and len(out) < 100


# ---------------------------------------------------------------- database


# The sender names stay Cyrillic: the author filter lowercases the needle, and
# that has to work for Cyrillic too, not only for ASCII.
def _msg(msg_id: int, text: str, *, who: str = "Петя", who_id: int = 555,
         day: int = 17, out: bool = False, media: str | None = None) -> dict:
    dt = datetime(2026, 8, day, 12, 0, tzinfo=UTC)
    return {
        "msg_id": msg_id,
        "ts": int(dt.timestamp()),
        "date": dt.isoformat(),
        "from_id": who_id,
        "from_name": who,
        "out": out,
        "media": media,
        "text": text,
    }


@pytest.fixture
def index(tmp_path):
    idx = MessageIndex(tmp_path / "index.db")
    idx.add(-100123, "Team", "group", [
        _msg(1, "Договорились о встрече во вторник"),
        _msg(2, "Аренду квартиры продлили до августа", who="Маша", who_id=777, day=10),
        _msg(3, "Отправил документы", out=True, who="you", who_id=1, day=12),
        _msg(4, "", media="photo"),
    ])
    idx.add(555, "Петя", "user", [_msg(9, "договоримся о встрече позже", day=16)])
    return idx


def test_search_finds_another_form_of_the_word(index):
    res = index.search("договоримся")
    assert res["indexed"] is True
    ids = {(m["chat_id"], m["id"]) for m in res["messages"]}
    # "договорились" found by the query "договоримся"
    assert (-100123, 1) in ids
    assert res["stems"] == ["договор"]


def test_the_exact_form_ranks_above_the_same_root(index):
    res = index.search("договорились")
    assert [m["id"] for m in res["messages"]][0] == 1
    assert res["messages"][0]["score"] >= res["messages"][1]["score"]


def test_the_match_is_highlighted(index):
    res = index.search("аренда")
    assert res["messages"][0]["match"] == "**Аренду** квартиры продлили до августа"


def test_query_words_are_joined_with_and(index):
    assert index.search("аренда квартира")["total"] == 1
    assert index.search("аренда документы")["total"] == 0


def test_filter_by_author(index):
    res = index.search("", author="маша")
    assert [m["id"] for m in res["messages"]] == [2]
    # including the message with a photo
    assert index.search("", author="петя")["total"] == 3
    assert index.search("", author="nobody")["total"] == 0


def test_filter_by_my_own_messages(index):
    assert [m["id"] for m in index.search("", mine=True)["messages"]] == [3]
    assert 3 not in [m["id"] for m in index.search("", mine=False)["messages"]]


def test_filter_by_period(index):
    since = int(datetime(2026, 8, 15, tzinfo=UTC).timestamp())
    until = int(datetime(2026, 8, 16, 23, 59, tzinfo=UTC).timestamp())
    res = index.search("", since_ts=since, until_ts=until)
    assert [m["id"] for m in res["messages"]] == [9]
    # the period narrows full-text search too: "встреча" is in both chats
    assert index.search("встреча")["total"] == 2
    assert [m["id"] for m in index.search("встреча", until_ts=until)["messages"]] == [9]


def test_filter_by_chat(index):
    res = index.search("встреча", chat_ids=[555])
    assert [m["chat_id"] for m in res["messages"]] == [555]


def test_filter_by_attachment_kind(index):
    assert [m["id"] for m in index.search("", kind="photo")["messages"]] == [4]
    assert index.search("", kind="any")["total"] == 1
    assert index.search("", kind="voice")["total"] == 0
    with pytest.raises(ValueError, match="kind"):
        index.search("", kind="telepathy")


def test_an_empty_query_is_a_slice_by_filters(index):
    res = index.search("", limit=2)
    assert res["total"] == 5 and len(res["messages"]) == 2
    assert "stems" in res and res["stems"] is None
    # freshest on top: 1 and 4 are the latest in time, 9 is older and missed the slice
    assert {m["id"] for m in res["messages"]} == {1, 4}


def test_reloading_does_not_breed_duplicates_and_updates_the_text(index):
    added = index.add(-100123, "Team", "group", [_msg(1, "Договорились на среду")])
    assert added == 0
    assert index.search("")["total"] == 5
    # the old text is gone from FTS as well
    assert index.search("вторник")["total"] == 0
    assert index.search("среда")["total"] == 1


def test_chat_state_and_boundaries(index):
    state = index.chat_state(-100123)
    assert state["name"] == "Team" and state["min_id"] == 1 and state["max_id"] == 4
    status = index.status()
    assert status["messages"] == 5 and status["exists"] is True
    assert status["mode"] == "0o600"                 # the correspondence on disk is private


def test_search_over_a_nonexistent_index(tmp_path):
    idx = MessageIndex(tmp_path / "missing.db")
    assert idx.exists() is False
    assert idx.search("anything at all") == {"indexed": False, "total": 0, "messages": []}
    assert idx.chat_state(1) is None
    assert idx.status()["exists"] is False


def test_dropping_a_chat_leaves_the_others(index):
    res = index.drop([-100123])
    assert res["messages_removed"] == 4 and res["messages_left"] == 1
    assert index.search("аренда")["total"] == 0          # out of FTS too
    assert index.search("")["total"] == 1
    assert index.chat_state(-100123) is None
    assert index.search("встреча")["total"] == 1         # chat 555 is untouched


def test_dropping_everything_deletes_the_file(index):
    res = index.drop()
    assert res["dropped"] == "all" and res["existed"] is True
    assert not index.path.exists()
    assert index.drop() == {"dropped": "nothing", "existed": False}


def test_a_quote_in_the_query_does_not_break_search(index):
    """An unclosed quote is FTS5 syntax; without escaping, the owner would get
    an sqlite exception instead of results."""
    assert index.search('quote"inside')["total"] == 0
    assert index.search('"аренда')["total"] == 1


def test_attachment_text_is_found_by_word(index):
    """An empty message with a picture would otherwise be found by nothing."""
    index.add(-100123, "Team", "group", [_msg(5, "[voice]", media="voice")])
    assert [m["id"] for m in index.search("voice")["messages"]] == [5]
