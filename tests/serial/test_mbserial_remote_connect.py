"""Tests for ``mbtools.serial.remote_connect`` (ticket 013) -- the
remote-transport counterpart to ``serial.connect``.

Mirrors ``tests/registry/remote_client/test_remote_client.py``'s own
``open_stream`` tests: every test here drives a real ``RemoteAPIServer``
over a real ``AF_INET`` loopback socket, with the local port backed by
``mbtools.testing.fakes.FakeSerial`` via an injected ``serial_factory``,
proving ``connect()``/:class:`RemoteSession` against the real wire
protocol rather than a mock of it.

``interact()``/``send_command()`` are exercised directly against a
:class:`~mbtools.serial.remote_connect.RemoteSession`, proving the
"duck-typed against anything with .read/.write" claim
``serial.connect``'s own module docstring makes, rather than assuming it
holds for this new transport too.
"""

from __future__ import annotations

import io
import sys
import time

import pytest

from mbtools.registry.client import DeviceLockedError, DeviceNotFoundError
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
from mbtools.registry.remote_client import RemoteRegistryClient
from mbtools.registry.store import Store
from mbtools.serial import connect as connect_mod
from mbtools.serial import remote_connect
from mbtools.testing.fakes import FakeSerial

VID_PID = "0d28:0204"
UID = "aa00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
PORT_PATH = "/dev/ttyACM0"


def _wait_until(predicate, timeout=2.0, interval=0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID, PORT_PATH, VID_PID)
    s.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="tovez", serial="1", raw="raw"
        ),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def fake_serials():
    return []


@pytest.fixture
def remote_server(store, locks, fake_serials):
    servers: list[RemoteAPIServer] = []

    def _default_factory(**serial_kwargs):
        fs = FakeSerial(**serial_kwargs)
        fake_serials.append(fs)
        return fs

    def _make(*, serial_factory=None, break_duration_s=0.01):
        srv = RemoteAPIServer(
            host="127.0.0.1",
            port=0,
            store=store,
            locks=locks,
            sweep_interval_s=100.0,
            serial_factory=serial_factory if serial_factory is not None else _default_factory,
            stream_settle_s=0,
            break_duration_s=break_duration_s,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


@pytest.fixture
def client(remote_server):
    srv = remote_server()
    c = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    yield c, srv
    c.close()


# ---------------------------------------------------------------------------
# connect(): resolve, lock, open the stream
# ---------------------------------------------------------------------------


def test_connect_locks_and_opens_the_stream(client, locks, fake_serials):
    c, srv = client
    session = remote_connect.connect(c, "tovez")
    try:
        assert session.uid == UID
        assert session.name == "tovez"
        status = locks.status(UID)
        assert status is not None
        assert status.kind == KIND_SERIAL

        _wait_until(lambda: len(fake_serials) == 1)
        ser = fake_serials[0]
        assert ser.dtr is False
        assert ser.rts is False
        assert not ser.break_calls  # no reset by default
    finally:
        session.close()


def test_close_releases_the_lock_and_is_idempotent(client, locks):
    c, srv = client
    session = remote_connect.connect(c, "tovez")
    assert locks.status(UID) is not None

    session.close()
    _wait_until(lambda: locks.status(UID) is None)
    session.close()  # must not raise, must not double-release


def test_already_locked_fails_fast_and_never_opens_the_stream(client, locks, fake_serials):
    c, srv = client
    holder_client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    holder_client.connect()
    holder_client.lock(UID, KIND_FLASH)
    try:
        start = time.monotonic()
        with pytest.raises(DeviceLockedError) as excinfo:
            remote_connect.connect(c, "tovez")
        elapsed = time.monotonic() - start
    finally:
        holder_client.unlock(UID)
        holder_client.close()

    assert excinfo.value.holder.get("kind") == KIND_FLASH
    assert fake_serials == []  # the stream was never opened
    assert elapsed < 2.0  # no retry, no blocking wait


def test_unknown_target_raises_device_not_found_and_unlocks_nothing(client, locks):
    c, srv = client
    with pytest.raises(DeviceNotFoundError):
        remote_connect.connect(c, "no-such-device")
    assert locks.status(UID) is None


# ---------------------------------------------------------------------------
# --reset: sends a BREAK frame over the stream, matching local Linux
# ---------------------------------------------------------------------------


def test_reset_sends_a_break_frame(client, fake_serials):
    c, srv = client
    session = remote_connect.connect(c, "tovez", reset=True, reset_settle_s=0)
    try:
        _wait_until(lambda: len(fake_serials) == 1)
        ser = fake_serials[0]
        _wait_until(lambda: len(ser.break_calls) == 1)
        assert ser.break_calls[0] == pytest.approx(0.01)
    finally:
        session.close()


def test_no_reset_by_default_sends_no_break(client, fake_serials):
    c, srv = client
    session = remote_connect.connect(c, "tovez")
    try:
        _wait_until(lambda: len(fake_serials) == 1)
        ser = fake_serials[0]
    finally:
        session.close()
    assert ser.break_calls == []


# ---------------------------------------------------------------------------
# RemoteSession duck-typing: interact()/send_command() work unchanged
# ---------------------------------------------------------------------------


def test_send_command_reads_the_boards_reply(remote_server):
    # `announcement_after_writes=1` -- the board answers only once it has
    # seen a write, not unprompted the instant the stream opens. An
    # unprompted announcement is exactly what `send_command`'s own
    # `reset_input_buffer()` call (before it writes anything) is *meant*
    # to discard as stale -- true for a real port's OS-level receive
    # buffer as much as for `RemoteSession`'s own buffer -- so scripting
    # one here would make this test race its own precondition rather than
    # exercise the reply path it means to cover.
    def _factory(**kw):
        return FakeSerial(announcement="OK 42", announcement_after_writes=1, **kw)

    srv = remote_server(serial_factory=_factory)
    c = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    session = remote_connect.connect(c, "tovez")
    try:
        lines = session.send_command("PING", timeout=1.0, idle_gap=0.05)
        assert lines == ["OK 42"]
    finally:
        session.close()


def test_send_command_no_reply_returns_empty_after_timeout(remote_server):
    srv = remote_server()
    c = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    session = remote_connect.connect(c, "tovez")
    try:
        lines = session.send_command("PING", timeout=0.2, idle_gap=0.05)
        assert lines == []
    finally:
        session.close()


def test_interact_relays_stdin_and_prints_board_output(
    remote_server, monkeypatch, capsys
):
    def _factory(**kw):
        return FakeSerial(announcement="hi", **kw)

    srv = remote_server(serial_factory=_factory)
    c = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    session = remote_connect.connect(c, "tovez")

    monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.1)
    monkeypatch.setattr(sys, "stdin", io.StringIO("PING\n"))

    try:
        assert session.interact() == 0
    finally:
        session.close()

    assert "hi" in capsys.readouterr().out


def test_write_reaches_the_owning_hosts_port(remote_server, fake_serials):
    srv = remote_server()
    c = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    session = remote_connect.connect(c, "tovez")
    try:
        session.write(b"HELLO\n")
        _wait_until(lambda: len(fake_serials) == 1)
        _wait_until(lambda: fake_serials[0].written == [b"HELLO\n"])
    finally:
        session.close()
