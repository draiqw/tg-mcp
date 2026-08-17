"""An understandable refusal instead of a raw Telegram error.

What is checked is not text for the sake of text, but two promises. The first: the
restriction is named in words and with a way out — from the class name
`ChatAdminRequiredError` the model will not work out what to do next. The second: the
check before the action refuses only when the answer is known in advance, while an
"I do not know" lets the call through — a refusal by guess is worse than an honest
attempt.

Not a single test goes to the network: the fake client can only answer about the account
itself, and on any other request it fails the test.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import pytest
import telethon.errors as tg_errors
from conftest import FakeConfigClient, FakeMessage, json_tree
from telethon.errors import ChatAdminRequiredError, FloodWaitError, RPCError
from telethon.errors.rpcerrorlist import PremiumAccountRequiredError, SlowModeWaitError
from telethon.tl import types

from tgagent import capabilities as caps
from tgagent import core


class NoNetwork:
    """A client that has not a single allowed action.

    It stands where the check is obliged to refuse by already known properties of the
    account: any call to Telegram here is a failure of the test, not a detail.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the call went out to Telegram: {name}")

    async def __call__(self, request: Any) -> Any:
        raise AssertionError(f"the call went out to Telegram: {type(request).__name__}")


class FakeAccountClient:
    """A client that answers only about the account: the profile and the configuration.

    The check before the action is assembled out of those two, so everything else is a
    sign that the check did not work and the call went to the network.
    """

    def __init__(self, premium: bool, app_config: dict | None = None) -> None:
        self.me = types.User(id=1, first_name="Someone", premium=premium)
        self.app_config = app_config
        self.profile_reads = 0
        self.requests: list[str] = []

    async def get_me(self) -> Any:
        self.profile_reads += 1
        return self.me

    async def __call__(self, request: Any) -> Any:
        name = type(request).__name__
        self.requests.append(name)
        if name == "GetAppConfigRequest" and self.app_config is not None:
            return types.help.AppConfig(hash=1, config=json_tree(self.app_config))
        raise AssertionError(f"a superfluous request to Telegram: {name}")

    async def get_input_entity(self, ent: Any) -> Any:
        raise AssertionError("a superfluous request to Telegram: get_input_entity")


def known(service, premium: bool | None = None, trial: int | None = None) -> None:
    """Set the account properties as if they had already been asked for.

    A fresh cache is the state the service lives in almost all of the time: the daemon
    runs for weeks, and this is re-read once per ACCOUNT_FACTS_TTL.
    """
    if premium is not None:
        service._premium = premium
        service._premium_at = time.monotonic()
    if trial is not None:
        service._app_config_cache = {"transcribe_audio_trial_weekly_number": trial}
        service._app_config_at = time.monotonic()


# ------------------------------------------------------- translation of errors


def test_admin_rights_are_explained_in_words_not_by_a_class_name():
    text = caps.explain_error(ChatAdminRequiredError(request=None))
    assert "administrator rights" in text
    assert "ChatAdminRequired" not in text


def test_the_subscription_is_named_a_subscription_and_comes_with_a_way_out():
    text = caps.explain_error(PremiumAccountRequiredError(request=None))
    assert "Premium" in text and "no local way around it" in text


def test_a_wait_says_how_long_to_wait():
    assert "42 s" in caps.explain_error(FloodWaitError(request=None, capture=42))


def test_the_number_of_seconds_is_taken_from_the_text_of_an_unfamiliar_error():
    """For an error without a class Telethon does not parse the number either: it is only
    in the text.

    A "wait" without the number is half an answer, so FLOOD_WAIT_18 is obliged to explain
    itself the same way as a FloodWaitError parsed into a class.
    """
    exc = RPCError(request=None, message="FLOOD_WAIT_18", code=420)
    assert getattr(exc, "seconds", None) is None
    assert "18 s" in caps.explain_error(exc)


def test_slow_mode_is_recognised_both_by_class_and_by_code():
    """The server sends SLOWMODE_WAIT, Telethon calls the class SlowModeWaitError.

    The codes from the class name and from the text diverge on one and the same
    restriction, and both have to be known — otherwise the explanation goes to only one of
    the two paths.
    """
    by_class = caps.explain_error(SlowModeWaitError(request=None, capture=7))
    by_text = caps.explain_error(RPCError(request=None, message="SLOWMODE_WAIT_7", code=420))
    assert by_class == by_text
    assert "slow mode" in by_class and "7 s" in by_class


def test_a_code_without_a_class_is_caught_by_the_text():
    """The server adds codes faster than Telethon starts classes for them."""
    exc = RPCError(request=None, message="DIALOG_FILTERS_TOO_MUCH", code=400)
    assert "folders" in caps.explain_error(exc)


@pytest.mark.parametrize(
    "code, must_contain",
    [
        ("MEDIA_CAPTION_TOO_LONG", "caption"),
        ("FILE_PARTS_INVALID", "Split the file"),
        ("REACTION_INVALID", "does not allow all of them"),
        ("CHAT_WRITE_FORBIDDEN", "writing in this chat is not possible"),
    ],
)
def test_typical_refusals_say_what_to_do(code, must_contain):
    assert must_contain in caps.explain_error(RPCError(request=None, message=code, code=400))


def test_an_unfamiliar_error_does_not_get_an_invented_cause():
    assert caps.explain_error(RuntimeError("something of our own")) is None
    assert caps.explain_error(RPCError(request=None, message="SOMETHING_NEW", code=400)) is None


def test_a_described_error_is_explained_when_it_arrives_as_a_class_too():
    """One and the same refusal arrives by two paths, and both are obliged to explain
    themselves.

    A familiar error is parsed by Telethon into a class, and the code is then assembled
    from that name rather than from the text. A divergence of the name from the server
    code (SLOWMODE_WAIT against SlowModeWaitError) silently switches the explanation off
    exactly where it is needed most — on the parsed error.
    """
    registry = dict(tg_errors.rpc_errors_dict)
    for pattern, cls in tg_errors.rpc_errors_re:
        registry[re.sub(r"_?\(\\d\+\)_?", "", pattern).strip("_")] = cls

    mute = []
    for code, cls in registry.items():
        if code not in caps.ERROR_HINTS and code not in caps.WAIT_HINTS:
            continue
        try:
            exc = cls(request=None, capture=5)
        except TypeError:
            exc = cls(request=None)
        if caps.explain_error(exc) is None:
            mute.append(f"{code} -> {cls.__name__}")
    assert not mute


def test_our_own_error_text_stays_as_it_was():
    """Translation is only for typical Telegram errors; our own explanation is ready."""
    assert core.explain(RuntimeError("Groq 401: key rejected")) == "Groq 401: key rejected"
    assert core.explain(ValueError("")) == "ValueError"


def test_the_caused_by_tail_is_removed_from_the_text():
    exc = FloodWaitError(request=None, capture=5)
    assert "caused by" in str(exc)
    assert "caused by" not in core.explain(exc)


# --------------------------------------------------------- subscription flag


async def test_the_subscription_is_read_once_per_period(service):
    client = FakeAccountClient(premium=False)
    service.client = client

    assert await service.is_premium() is False
    assert await service.is_premium() is False
    assert client.profile_reads == 1

    # The subscription was bought, the daemon was not restarted: until the period ends
    # this is not visible yet.
    client.me = types.User(id=1, first_name="Someone", premium=True)
    assert await service.is_premium() is False

    service._premium_at -= core.ACCOUNT_FACTS_TTL + 1
    assert await service.is_premium() is True
    assert client.profile_reads == 2


async def test_a_profile_read_that_did_not_happen_is_not_a_no(service):
    """"I do not know" and "no subscription" are different answers: on the first one
    refusing is not allowed."""

    class Broken:
        async def get_me(self) -> Any:
            raise ConnectionError("no network")

    service.client = Broken()
    assert await service.is_premium() is None


async def test_a_read_failure_does_not_spoil_what_is_already_known(service):
    class Broken:
        async def get_me(self) -> Any:
            raise ConnectionError("no network")

    known(service, premium=True)
    service._premium_at -= core.ACCOUNT_FACTS_TTL + 1
    service.client = Broken()
    assert await service.is_premium() is True


async def test_the_app_configuration_is_not_asked_for_twice_in_a_row(service):
    client = FakeConfigClient({"transcribe_audio_trial_weekly_number": 2})
    service.client = client
    assert await service._transcribe_trial() == 2
    assert await service._transcribe_trial() == 2
    assert client.calls == 1


# --------------------------------------------- transcription without a subscription


async def test_without_a_subscription_and_without_free_ones_transcription_refuses_in_words(
    service,
):
    known(service, premium=False, trial=0)
    service.client = NoNetwork()
    with pytest.raises(ValueError, match="Premium") as exc:
        await service._transcribe_telegram("Pete", FakeMessage(id=1))
    # The refusal is obliged to name the way out, not only the prohibition.
    assert "engine=\"groq\"" in str(exc.value)


async def test_with_a_subscription_transcription_goes_as_before(service):
    """The check must not become a new way to fail — nor a superfluous request."""
    client = FakeAccountClient(premium=True)
    service.client = client
    await service._assert_transcribe_allowed()
    assert client.requests == []          # there is no point asking about the caps


async def test_free_transcripts_do_not_let_us_refuse_in_advance(service):
    """Without Premium a transcript happens by the counter — a refusal would take it away."""
    known(service, premium=False, trial=2)
    service.client = NoNetwork()
    await service._assert_transcribe_allowed()


async def test_an_unknown_subscription_lets_the_call_through_to_telegram(service):
    class Broken:
        async def get_me(self) -> Any:
            raise ConnectionError("no network")

    service.client = Broken()
    await service._assert_transcribe_allowed()


async def test_an_unknown_counter_also_lets_the_call_through(service):
    """The configuration did not arrive — that is about us, not about the account: let the
    server decide."""

    class NoConfig(FakeAccountClient):
        async def __call__(self, request: Any) -> Any:
            raise ConnectionError("no network")

    known(service, premium=False)
    service.client = NoConfig(premium=False)
    await service._assert_transcribe_allowed()


# ---------------------------------------------------------------- reactions


async def test_several_reactions_without_a_subscription_refuse_before_going_to_the_chat(
    service,
):
    known(service, premium=False)
    service.client = NoNetwork()
    with pytest.raises(ValueError, match="Premium"):
        await service.react("Pete", 10, emoji=["👍", "🔥"])


async def test_a_custom_emoji_without_a_subscription_refuses_before_going_to_the_chat(service):
    known(service, premium=False)
    service.client = NoNetwork()
    with pytest.raises(ValueError, match="Premium"):
        await service.react("Pete", 10, emoji="5312526098750252863")


async def test_one_ordinary_reaction_without_a_subscription_passes_the_check(service):
    """It is allowed to everyone: refusing here would mean inventing a restriction."""
    known(service, premium=False)
    service.client = NoNetwork()
    with pytest.raises(AssertionError, match="went out to Telegram"):
        await service.react("Pete", 10, emoji="👍")


async def test_an_unknown_subscription_does_not_forbid_a_reaction(service):
    class Broken(NoNetwork):
        async def get_me(self) -> Any:
            raise ConnectionError("no network")

    service.client = Broken()
    with pytest.raises(AssertionError, match="went out to Telegram"):
        await service.react("Pete", 10, emoji=["👍", "🔥"])


# ------------------------------------------------------------ the daemon's answer


class FakeRequest:
    """An aiohttp request in the volume in which handle_call reads it."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


async def call_daemon(daemon, monkeypatch, exc: Exception) -> dict:
    """A writing call that failed with the given Telegram error."""

    async def boom(**params: Any) -> None:
        raise exc

    class Svc:
        account = "main"

    monkeypatch.setattr(daemon, "service", lambda account=None: Svc())
    monkeypatch.setattr(daemon, "dispatch_table", lambda svc: {"react": boom})
    resp = await daemon.handle_call(
        FakeRequest({"method": "react", "params": {"chat": "Work", "message_id": 5}})
    )
    return json.loads(resp.text)


async def test_a_telegram_error_does_not_reach_the_agent_raw(daemon, monkeypatch):
    out = await call_daemon(daemon, monkeypatch, ChatAdminRequiredError(request=None))
    assert out["ok"] is False
    assert "administrator rights" in out["error"]
    assert "ChatAdminRequired" not in out["error"]


async def test_an_unfamiliar_error_arrives_in_full(daemon, monkeypatch):
    """An invented cause is worse than raw text: here we show it as it is."""
    out = await call_daemon(daemon, monkeypatch, RuntimeError("something new"))
    assert out["error"] == "RuntimeError: something new"


async def test_the_action_log_holds_the_same_cause_as_the_agent_got(daemon, monkeypatch):
    """A divergence between the log and the answer would read as two different refusals."""
    from tgagent import config

    await call_daemon(daemon, monkeypatch, ChatAdminRequiredError(request=None))
    assert "administrator rights" in config.ACTIONS_LOG.read_text()
