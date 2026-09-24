"""Tests for ``mbtools.serial.connect`` (ticket 009).

Per sprint.md's Testing note ("FakeSerial ... standing in for the port --
asserts DTR/RTS held low by default; asserts --reset drives the
platform-specific reset path ... already-locked fail-fast; lock released
on normal exit, simulated Ctrl-C, and explicit library-caller close"),
these tests drive :func:`mbtools.serial.connect.connect` against a real,
in-process :class:`~mbtools.registry.api.RegistryAPIServer` (no Daemon
poll loop -- mirrors ``tests/deploy/test_deploy_cli.py``'s own
lightweight ``server`` fixture) with ``FakeSerial`` injected as the port,
so both halves -- the registry round-trip and the serial-port mechanics
-- are exercised together, not mocked apart.

:func:`send_command`/:func:`interact` are also covered directly against
a bare ``FakeSerial`` -- they are ported unchanged from mbdeploy's
``console.py``, so these are the same cases that module's own test suite
covers, applied here to prove the port carried the logic over intact.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile

import pytest

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.client import (
    DeviceLockedError,
    RegistryClient,
)
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, HolderRef, LockManager
from mbtools.registry.store import Store
from mbtools.serial import connect as connect_mod
from mbtools.testing.fakes import FakeSerial

VID, PID = DAPLINK_VID_PID
VID_PID = "0d28:0204"
UID = "99000000111122223333444455556666e0528ab"


def _local_holder(pid: int) -> HolderRef:
    """Mirrors api.py's own ``_local_holder`` construction (ticket 002)
    for tests that acquire directly against ``LockManager``, bypassing
    the wire protocol."""
    return HolderRef(origin="local", ref=str(pid), pid=pid)
PORT = "/dev/ttyACM7"


# ---------------------------------------------------------------------------
# fixtures -- lightweight RegistryAPIServer, no Daemon poll loop
# (mirrors tests/deploy/test_deploy_cli.py's own `server` fixture)
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbserial-connect-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def server(socket_dir, store, locks):
    flash_op = FlashOp(locks=locks, store=store)
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        sweep_interval_s=100.0,
    )
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    c = RegistryClient(server.socket_path)
    yield c
    c.close()


def _seed_device(store: Store, uid: str = UID, *, port: str = PORT, device_name: str = "tovez") -> None:
    store.upsert_attached(uid, port, VID_PID)
    store.apply_probe_result(
        uid,
        ProbeResult(
            role="NEZHA2",
            common_name="robot",
            device_name=device_name,
            serial="1",
            raw="raw",
        ),
    )


def _fake_factory(**kwargs):
    return FakeSerial(**kwargs)


class _RaisingStdin:
    """Stands in for ``sys.stdin`` to simulate Ctrl-C landing mid-read
    (``interact()``'s own ``sys.stdin.readline()`` call) -- monkeypatching
    a method directly onto ``sys.stdin`` is unsafe under pytest's own
    stdin capture object, so a whole replacement object is used instead,
    mirroring ``io.StringIO`` swap-ins used elsewhere in this suite.
    """

    def readline(self) -> str:
        raise KeyboardInterrupt


# ---------------------------------------------------------------------------
# default open: DTR/RTS held low, no reboot
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_connect_holds_dtr_rts_low_by_default(server, store, client):
    _seed_device(store)

    session = connect_mod.connect(
        client, "tovez", serial_factory=_fake_factory, settle_s=0
    )
    try:
        assert session.ser.dtr is False
        assert session.ser.rts is False
        assert session.ser.open_calls == 1
        assert not session.ser.break_calls  # no reset performed
    finally:
        session.close()


@pytest.mark.requires_af_unix
def test_connect_locks_the_device_for_the_session(server, store, locks, client):
    _seed_device(store)

    session = connect_mod.connect(
        client, "tovez", serial_factory=_fake_factory, settle_s=0
    )
    try:
        status = locks.status(UID)
        assert status is not None
        assert status.kind == "serial"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# --reset: platform-specific path, injected via `platform_name=`
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_reset_on_linux_sends_a_break_not_a_reopen(server, store, client):
    _seed_device(store)

    session = connect_mod.connect(
        client,
        "tovez",
        reset=True,
        serial_factory=_fake_factory,
        settle_s=0,
        reset_settle_s=0,
        platform_name="Linux",
    )
    try:
        assert session.ser.break_calls == [connect_mod.BREAK_DURATION]
        assert session.ser.open_calls == 1  # never closed/reopened
        assert session.ser.close_calls == 0
    finally:
        session.close()


@pytest.mark.requires_af_unix
def test_reset_on_macos_closes_and_reopens_not_a_break(server, store, client):
    _seed_device(store)

    opened: list[FakeSerial] = []

    def _tracking_factory(**kwargs):
        fake = FakeSerial(**kwargs)
        opened.append(fake)
        return fake

    session = connect_mod.connect(
        client,
        "tovez",
        reset=True,
        serial_factory=_tracking_factory,
        settle_s=0,
        reset_settle_s=0,
        platform_name="Darwin",
    )
    try:
        assert len(opened) == 2  # the original open, plus the reset reopen
        first, second = opened
        assert first.close_calls == 1
        assert first.break_calls == []  # no BREAK on this platform
        assert second.open_calls == 1
        assert session.ser is second
        # back to the held-low idle default after the reset reopen
        assert session.ser.dtr is False
        assert session.ser.rts is False
    finally:
        session.close()


# ---------------------------------------------------------------------------
# already-locked: fails fast, no retry, no blocking wait, port never opened
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_already_locked_fails_fast_and_never_opens_the_port(server, store, locks, client):
    _seed_device(store)
    locks.acquire(UID, KIND_FLASH, _local_holder(4242))

    opened = []

    def _spy_factory(**kwargs):
        opened.append(1)
        return FakeSerial(**kwargs)

    with pytest.raises(DeviceLockedError) as excinfo:
        connect_mod.connect(client, "tovez", serial_factory=_spy_factory, settle_s=0)

    assert excinfo.value.holder == {"kind": "flash", "pid": 4242}
    assert opened == []  # the port was never touched


# ---------------------------------------------------------------------------
# lock is released on every exit path: normal close, simulated Ctrl-C
# (inside interact()), and explicit library-caller close
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_lock_released_on_explicit_close(server, store, locks, client):
    _seed_device(store)

    session = connect_mod.connect(
        client, "tovez", serial_factory=_fake_factory, settle_s=0
    )
    assert locks.status(UID) is not None

    session.close()
    assert locks.status(UID) is None


@pytest.mark.requires_af_unix
def test_close_is_idempotent(server, store, locks, client):
    _seed_device(store)

    session = connect_mod.connect(
        client, "tovez", serial_factory=_fake_factory, settle_s=0
    )
    session.close()
    session.close()  # must not raise, must not double-release
    assert locks.status(UID) is None


@pytest.mark.requires_af_unix
def test_lock_released_after_simulated_ctrl_c_in_interact(
    server, store, locks, client, monkeypatch
):
    _seed_device(store)

    session = connect_mod.connect(
        client, "tovez", serial_factory=_fake_factory, settle_s=0
    )

    monkeypatch.setattr(sys, "stdin", _RaisingStdin())
    monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.0)

    # interact() itself catches Ctrl-C and returns normally -- the lock
    # is released by the caller's own close(), same as a normal Ctrl-D
    # exit.
    assert session.interact() == 0
    assert locks.status(UID) is not None  # not yet -- close() hasn't run

    session.close()
    assert locks.status(UID) is None


@pytest.mark.requires_af_unix
def test_lock_released_even_if_open_fails(server, store, locks, client):
    _seed_device(store)

    def _busy_factory(**kwargs):
        return FakeSerial(busy=True, **kwargs)

    with pytest.raises(connect_mod.ConnectError):
        connect_mod.connect(client, "tovez", serial_factory=_busy_factory, settle_s=0)

    assert locks.status(UID) is None  # never leaked


# ---------------------------------------------------------------------------
# send_command / interact -- ported-unchanged logic, exercised directly
# ---------------------------------------------------------------------------


class TestSendCommand:
    def test_message_is_sent_and_reply_lines_returned(self):
        ser = FakeSerial(announcement="OK")
        lines = connect_mod.send_command(ser, "PING", timeout=0.3, idle_gap=0.05)
        assert ser.written == [b"PING\n"]
        assert lines == ["OK"]

    def test_no_reply_returns_empty_after_timeout(self):
        ser = FakeSerial()
        lines = connect_mod.send_command(ser, "PING", timeout=0.1)
        assert lines == []

    def test_resets_input_buffer_before_writing(self):
        ser = FakeSerial()
        connect_mod.send_command(ser, "PING", timeout=0.05)
        assert ser.reset_input_buffer_calls == 1


class TestInteract:
    def test_stdin_is_relayed_and_board_output_printed(self, monkeypatch, capsys):
        ser = FakeSerial(announcement="hi")
        monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.05)
        monkeypatch.setattr(sys, "stdin", io.StringIO("PING\n"))

        assert connect_mod.interact(ser) == 0
        assert ser.written == [b"PING\n"]
        assert "hi" in capsys.readouterr().out

    def test_empty_stdin_exits_cleanly(self, monkeypatch):
        ser = FakeSerial()
        monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.05)
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))

        assert connect_mod.interact(ser) == 0
        assert ser.written == []

    def test_ctrl_c_returns_zero_without_raising(self, monkeypatch):
        ser = FakeSerial()

        monkeypatch.setattr(sys, "stdin", _RaisingStdin())
        monkeypatch.setattr(connect_mod, "EOF_DRAIN", 0.0)

        assert connect_mod.interact(ser) == 0
