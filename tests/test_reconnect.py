"""Coming back after Telethon has given the connection up for dead.

The failure this guards against is silent, which is the whole problem with it.
Telethon retries a broken connection a fixed number of times and then calls
`_disconnect`: the process stays up, the socket keeps answering, incoming
messages stop arriving and every call raises ConnectionError without touching
the network. Nothing ever tries again. It was found by hand, twice, and both
times the cure was `tg daemon restart` — which is not something an agent that is
supposed to be watching a chat should need.

Measured against the real account before the fix: after forcing that state, four
calls spread over thirty-five seconds all failed instantly, and a plain
`client.connect()` fixed it in a tenth of a second.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tgagent import config
from tgagent import daemon as daemon_mod
from tgagent.core import TelegramService


class FakeRequest:
    """An aiohttp request in the volume in which handle_call reads it."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def json(self) -> dict:
        return self._payload


class FakeClient:
    """Connected or not, and it counts how often it was asked to connect."""

    def __init__(self, *, connected: bool = True, authorized: bool = True,
                 connect_delay: float = 0.0, fails: Exception | None = None) -> None:
        self._connected = connected
        self._authorized = authorized
        self._delay = connect_delay
        self._fails = fails
        self.connects = 0

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.connects += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fails:
            raise self._fails
        self._connected = True

    async def is_user_authorized(self) -> bool:
        return self._authorized


def make_service(client: FakeClient, account: str = "main") -> TelegramService:
    svc = TelegramService.__new__(TelegramService)
    svc.account = account
    svc.client = client
    svc._reconnect_lock = asyncio.Lock()
    return svc


async def test_a_healthy_connection_is_left_alone():
    """This sits on the path of every call, so the common case must cost nothing."""
    client = FakeClient(connected=True)
    svc = make_service(client)
    assert await svc.ensure_connected() is False
    assert client.connects == 0


async def test_a_dead_connection_is_brought_back():
    client = FakeClient(connected=False)
    svc = make_service(client)
    assert await svc.ensure_connected() is True
    assert client.connects == 1
    assert client.is_connected()
    # And the next caller finds nothing left to do.
    assert await svc.ensure_connected() is False
    assert client.connects == 1


async def test_a_burst_of_calls_opens_one_connection_not_a_burst():
    """Ten concurrent calls arriving during an outage are ten chances to stampede.

    Without the lock each of them sees a disconnected client and starts its own
    reconnection, which is how a client that was merely offline earns a flood
    error and stays offline.
    """
    client = FakeClient(connected=False, connect_delay=0.05)
    svc = make_service(client)
    healed = await asyncio.gather(*(svc.ensure_connected() for _ in range(10)))
    assert client.connects == 1
    assert healed.count(True) == 1          # one of them did the work
    assert healed.count(False) == 9         # the rest waited and found it done


async def test_a_session_revoked_while_offline_is_not_papered_over():
    """Reconnecting forever would not help, and silence would be worse.

    A session revoked from another device comes back as a connection that works
    and an account that is gone. That is the owner's business, not something to
    retry.
    """
    client = FakeClient(connected=False, authorized=False)
    svc = make_service(client)
    with pytest.raises(config.SetupError):
        await svc.ensure_connected()


async def test_a_failed_reconnect_reaches_the_caller():
    client = FakeClient(connected=False, fails=OSError("network is unreachable"))
    svc = make_service(client)
    with pytest.raises(OSError):
        await svc.ensure_connected()


# ---------------------------------------------------------------- in the daemon


async def test_a_call_that_arrives_during_an_outage_waits_out_the_reconnect(
        daemon, monkeypatch):
    """The agent should get its answer, not a ConnectionError it cannot act on."""
    seen: dict[str, Any] = {}

    async def spy(**params: Any) -> dict:
        seen.update(params)
        return {"ok": 1}

    svc = daemon_mod.TelegramService.__new__(daemon_mod.TelegramService)
    svc.account = "main"
    svc.client = FakeClient(connected=False)
    svc._reconnect_lock = asyncio.Lock()

    monkeypatch.setattr(daemon, "service", lambda account=None: svc)
    monkeypatch.setattr(daemon, "dispatch_table", lambda s: {"history": spy})
    resp = await daemon.handle_call(
        FakeRequest({"method": "history", "params": {"chat": "Work"}})
    )
    assert json.loads(resp.text) == {"ok": True, "result": {"ok": 1}}
    assert seen == {"chat": "Work"}
    assert svc.client.connects == 1


async def test_the_health_loop_speaks_once_per_outage_not_once_per_tick(
        daemon, monkeypatch):
    """A connection that flaps must not turn the alert chat into the log."""
    said: list[str] = []

    async def notify(text: str) -> None:
        said.append(text)

    monkeypatch.setattr(daemon, "notify_owner", notify)
    monkeypatch.setattr(daemon_mod, "HEALTH_TICK_SEC", 0)

    client = FakeClient(connected=False, fails=OSError("still down"))
    svc = make_service(client)
    daemon.services = {"main": svc}

    task = asyncio.create_task(daemon.health_loop())
    await asyncio.sleep(0.05)
    assert len(said) == 1, said              # many ticks, one complaint
    assert "main" in said[0]

    client._fails = None                     # the network comes back
    await asyncio.sleep(0.05)
    task.cancel()
    assert len(said) == 2
    assert said[1] != said[0]                # and the recovery is announced


# ---------------------------------------------------------------- the log itself


def test_the_log_is_trimmed_in_place_and_keeps_the_tail(tmp_path, monkeypatch):
    """It had reached 82 MB before anybody looked, which is a log nobody opens.

    Trimming in place rather than renaming is not a detail: the daemon's stdout
    is this file, opened by whoever started it. A rename would leave the process
    writing into the renamed inode and the visible file empty for the rest of the
    run — rotated to look at, dead to read.
    """
    path = tmp_path / "daemon.log"
    monkeypatch.setattr(daemon_mod.config, "DAEMON_LOG", path)
    monkeypatch.setattr(daemon_mod, "MAX_DAEMON_LOG_BYTES", 4096)
    monkeypatch.setattr(daemon_mod, "DAEMON_LOG_KEEP_BYTES", 1024)

    path.write_bytes(b"old\n" * 100 + b"x" * 8000 + b"the end\n")
    daemon_mod.trim_daemon_log()

    kept = path.read_bytes()
    assert len(kept) <= 1024 + 200          # the tail, plus the line it writes
    assert kept.rstrip().endswith(b"4 MB") or b"the end" in kept

    # And the file is still a file that can be appended to.
    with path.open("a") as fh:
        fh.write("after\n")
    assert path.read_text().endswith("after\n")


def test_a_log_under_the_cap_is_left_alone(tmp_path, monkeypatch):
    path = tmp_path / "daemon.log"
    monkeypatch.setattr(daemon_mod.config, "DAEMON_LOG", path)
    monkeypatch.setattr(daemon_mod, "MAX_DAEMON_LOG_BYTES", 4096)
    path.write_text("small\n")
    daemon_mod.trim_daemon_log()
    assert path.read_text() == "small\n"


def test_a_log_that_cannot_be_trimmed_does_not_stop_the_daemon(tmp_path, monkeypatch):
    """Nothing about a log is worth killing the process over."""
    monkeypatch.setattr(daemon_mod.config, "DAEMON_LOG", tmp_path / "gone.log")
    daemon_mod.trim_daemon_log()            # missing file: no exception
