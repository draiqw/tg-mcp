"""The language catalog: which language the owner reads, and what `t()` gives back.

Two things are checked here that nothing else in the suite can catch. First, that
`TG_LANG` is honoured on every call — the daemon runs for weeks and the owner
edits `.env` by hand, so a value cached at import would look like a broken
setting. Second, that the two languages have not drifted apart: a key present in
one half only, or a placeholder renamed on one side, turns a message into
gibberish at the exact moment somebody is being alerted.
"""

from __future__ import annotations

import pytest

from tgagent.i18n import DEFAULT, MESSAGES, SUPPORTED, language, placeholders

# ---------------------------------------------------------------- language()


def test_default_is_the_first_supported_language():
    """The fallback and the head of SUPPORTED must be the same language:
    `t()` falls back to DEFAULT, and a DEFAULT outside SUPPORTED would mean
    falling back to a language nobody promised to translate into."""
    assert DEFAULT in SUPPORTED
    assert SUPPORTED[0] == DEFAULT


@pytest.mark.parametrize("value", ["en", "ru"])
def test_tg_lang_chooses_the_language(monkeypatch, value):
    monkeypatch.setenv("TG_LANG", value)
    assert language() == value


@pytest.mark.parametrize("value", ["", "   ", "de", "zz", "russian", "rus", "-", "0"])
def test_an_unsupported_or_empty_tg_lang_falls_back_to_english(monkeypatch, value):
    """A typo in .env must not leave the owner with keys instead of messages."""
    monkeypatch.setenv("TG_LANG", value)
    assert language() == DEFAULT


def test_an_unset_tg_lang_falls_back_to_english(monkeypatch):
    """A fresh installation has no TG_LANG at all — that is the common case."""
    monkeypatch.delenv("TG_LANG", raising=False)
    assert language() == DEFAULT


@pytest.mark.parametrize(
    "value, expect",
    [
        ("ru_RU.UTF-8", "ru"),          # what `locale` prints on Linux
        ("en-GB", "en"),                # what a browser sends
        ("RU", "ru"),                   # case is not the owner's problem
        ("  ru  ", "ru"),               # a stray space from a hand-edited .env
        ("en_US", "en"),
        ("ru.UTF-8", "ru"),
    ],
)
def test_locale_shaped_values_are_understood(monkeypatch, value, expect):
    """People paste TG_LANG out of their system locale, not out of the docs."""
    monkeypatch.setenv("TG_LANG", value)
    assert language() == expect


def test_the_language_is_read_on_every_call(monkeypatch):
    """The property the daemon depends on: the setting takes effect without a
    restart. Cached at import, a change to .env would only show up hours later,
    and the owner would report the whole setting as broken."""
    from tgagent.i18n import t

    monkeypatch.setenv("TG_LANG", "en")
    assert t("confirm.yes") == "yes"
    monkeypatch.setenv("TG_LANG", "ru")
    assert language() == "ru"
    assert t("confirm.yes") == "да"
    monkeypatch.setenv("TG_LANG", "en")
    assert t("confirm.yes") == "yes"


# ---------------------------------------------------------------- t()


def test_a_known_key_returns_the_text_of_the_current_language(monkeypatch):
    from tgagent.i18n import t

    monkeypatch.setenv("TG_LANG", "ru")
    assert t("daemon.stopped") == MESSAGES["daemon.stopped"]["ru"]


def test_named_substitutions_are_filled_in():
    """Without this the alerts would be delivered with {pid} in them."""
    from tgagent.i18n import t

    filled = t("daemon.started", pid=17, log="/tmp/x.log")
    assert filled == "Daemon started (pid 17). Log: /tmp/x.log"


def test_an_unknown_key_returns_the_key_itself():
    """`t()` is called from inside the alert path: a typo in a key must cost one
    ugly line, not the alert."""
    from tgagent.i18n import t

    assert t("no.such.key") == "no.such.key"
    assert t("no.such.key", pid=1) == "no.such.key"


def test_a_missing_substitution_returns_the_template():
    """A forgotten keyword argument would otherwise raise KeyError while the
    daemon is formatting an alert — and take the alert with it."""
    from tgagent.i18n import t

    assert t("daemon.started", pid=17) == MESSAGES["daemon.started"]["en"]


def test_braces_that_are_not_placeholders_survive():
    """`cli.arg_call_params` shows a JSON example, braces and all. Formatting it
    with anything at all must not turn into a KeyError on `"limit"`."""
    from tgagent.i18n import t

    plain = t("cli.arg_call_params")
    assert '{"limit": 5}' in plain
    assert t("cli.arg_call_params", limit=5) == plain


def test_an_extra_keyword_argument_is_ignored():
    from tgagent.i18n import t

    assert t("daemon.stopped", pid=17) == MESSAGES["daemon.stopped"]["en"]


def test_a_key_missing_in_the_current_language_falls_back_to_english(monkeypatch):
    """A half-translated entry gives the owner English, not an identifier."""
    from tgagent import i18n

    monkeypatch.setitem(i18n.MESSAGES, "test.only_en", {"en": "only English"})
    monkeypatch.setenv("TG_LANG", "ru")
    assert i18n.t("test.only_en") == "only English"


# ---------------------------------------------------------------- placeholders()


@pytest.mark.parametrize(
    "template, expect",
    [
        ("Daemon started (pid {pid}). Log: {log}", {"pid", "log"}),
        ("no substitutions here", set()),
        ("{name} and {name} again", {"name"}),
        ('JSON, e.g. \'{"limit": 5}\'', set()),   # a literal brace is not a placeholder
    ],
)
def test_placeholders_reads_the_named_substitutions(template, expect):
    assert placeholders(template) == expect


# ---------------------------------------------------------------- the catalog


def test_the_catalog_is_not_empty():
    """The catalog is assembled from many files at once. If the assembly went
    wrong and MESSAGES came out empty, every `t()` would quietly return its own
    key and the whole interface would turn into identifiers — with every other
    test in this file still passing."""
    assert len(MESSAGES) > 100


def test_every_key_has_text_in_every_supported_language():
    """A language that covers half the messages is worse than no language: the
    owner reads a paragraph and then hits an English sentence, or a bare key."""
    missing = [
        f"{key}[{lang}]"
        for key, entry in sorted(MESSAGES.items())
        for lang in SUPPORTED
        if not (entry.get(lang) or "").strip()
    ]
    assert not missing, f"empty or missing translation: {missing}"


def test_no_key_carries_a_language_outside_supported():
    """A stray `de` in one entry is a translation nobody checks and nobody sees."""
    stray = [
        f"{key}[{lang}]"
        for key, entry in sorted(MESSAGES.items())
        for lang in entry
        if lang not in SUPPORTED
    ]
    assert not stray, f"language outside SUPPORTED: {stray}"


def test_placeholders_match_across_languages():
    """This is the check that keeps the two halves from drifting. A placeholder
    renamed on one side only survives review easily and then shows up as a raw
    `{pid}` in an alert — or, worse, silently drops the value."""
    drifted = []
    for key, entry in sorted(MESSAGES.items()):
        expected = placeholders(entry[DEFAULT])
        for lang in SUPPORTED:
            found = placeholders(entry[lang])
            if found != expected:
                drifted.append(f"{key}: {DEFAULT}={sorted(expected)} != {lang}={sorted(found)}")
    assert not drifted, f"placeholders drifted: {drifted}"


def test_keys_are_shaped_as_area_dot_what():
    """The keys are the only index of this file. One that does not follow
    `<area>.<what>` is a key nobody finds when the message has to be changed."""
    bad = [key for key in sorted(MESSAGES) if not key.replace(".", "").replace("_", "").isalnum()]
    assert not bad, f"malformed keys: {bad}"
    no_area = [key for key in sorted(MESSAGES) if "." not in key]
    assert not no_area, f"keys without an area: {no_area}"


# ---------------------------------------------------------------- commands in messages


def test_the_command_is_filled_in_without_being_passed(monkeypatch):
    """`{cmd}` is the catalog's way of not knowing how the reader spells `tg`.

    The call site does not pass it — there are eighty places that would have to,
    and the one that forgot would print a raw `{cmd}` at the worst moment.
    """
    from tgagent import config
    from tgagent.i18n import t

    monkeypatch.setattr(config, "INSTALLED", True)
    assert t("login.next_daemon") == "Next: tg daemon start"

    monkeypatch.setattr(config, "INSTALLED", False)
    assert t("login.next_daemon") == "Next: uv run tg daemon start"


def test_a_passed_value_wins_over_the_filled_in_one(monkeypatch):
    """Injection must not take a name away from the caller."""
    from tgagent import config
    from tgagent.i18n import t

    monkeypatch.setattr(config, "INSTALLED", True)
    assert t("login.next_daemon", cmd="something else") == "Next: something else daemon start"


def test_no_message_spells_the_command_by_hand():
    """The catalog names commands through `{cmd}` or not at all.

    `uv run tg` in a message is right for a clone and wrong for an installed
    package, and the reader of that message is the one person who cannot tell
    which they have.
    """
    guilty = sorted(
        f"{key}:{lang}"
        for key, entry in MESSAGES.items()
        for lang, text in entry.items()
        if "uv run tg" in text or "uv sync --extra" in text
    )
    assert not guilty, f"the command is spelled by hand in: {guilty}"
