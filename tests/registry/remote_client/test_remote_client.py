"""Tests for mbtools.registry.remote_client -- the typed TCP client
library (ticket 011) that mirrors mbtools.registry.client's shape
(same method names, same session model, the exact same exception
classes) for registry.remote_api's wire protocol.

Per ticket 011's own Testing section: every method is exercised against
a real loopback RemoteAPIServer (tickets 006/007/008), reusing that
module's own "drive the real server over a real socket" precedent
(tests/registry/remote_api/test_remote_api.py and friends) -- plus the
exception-mapping behavior registry.client's own suite already proves,
repeated here against RemoteRegistryClient to show behavioral parity.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from mbtools.common import EXIT_ERROR, EXIT_LOCKED, EXIT_NO_DEVICE, EXIT_USAGE
from mbtools.registry import client as client_module
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, LockManager
from mbtools.registry.remote_api import DEFAULT_REMOTE_PORT, RemoteAPIServer
from mbtools.registry.remote_client import (
    DeviceLockedError,
    DeviceNotFoundError,
    FlashResult,
    InvalidRequestError,
    RegistryClientError,
    RegistryUnavailable,
    RemoteRegistryClient,
    RemoteStream,
)
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeSerial

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


UID = _uid("aaaa1111")
UID2 = _uid("bbbb2222")

#: A minimal, complete, valid Intel HEX file -- just the EOF record,
#: same content tests/registry/remote_api/test_remote_flash.py uses.
_VALID_HEX_CONTENT = ":00000001FF\n"


class _FakeProcess:
    """Stand-in for a subprocess.Popen instance -- mirrors
    test_remote_flash.py's own fake: flashlogic._run_streamed only ever
    iterates `.stdout` for lines and calls `.wait()`."""

    def __init__(self, returncode: int, lines: tuple[str, ...] = ()):
        self.returncode = returncode
        self.stdout = iter(f"{line}\n" for line in lines)

    def wait(self) -> int:
        return self.returncode


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    s.apply_probe_result(
        UID,
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="1", raw="x"
        ),
    )
    s.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)
    s.apply_probe_result(
        UID2,
        ProbeResult(
            role="robot", common_name="Robot", device_name="beta", serial="2", raw="y"
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

    def _serial_factory(**serial_kwargs):
        fs = FakeSerial(**serial_kwargs)
        fake_serials.append(fs)
        return fs

    def _make(*, auth_token=None, serial_factory=None, break_duration_s=0.01):
        srv = RemoteAPIServer(
            host="127.0.0.1",
            port=0,
            store=store,
            locks=locks,
            auth_token=auth_token,
            sweep_interval_s=100.0,
            serial_factory=serial_factory if serial_factory is not None else _serial_factory,
            stream_settle_s=0,
            break_duration_s=break_duration_s,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


def _wait_until(predicate, timeout=2.0, interval=0.01) -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


# ---------------------------------------------------------------------------
# reused exceptions -- imported from registry.client, never redefined
# ---------------------------------------------------------------------------


def test_exceptions_are_the_exact_same_classes_as_registry_client():
    assert RegistryUnavailable is client_module.RegistryUnavailable
    assert RegistryClientError is client_module.RegistryClientError
    assert InvalidRequestError is client_module.InvalidRequestError
    assert DeviceNotFoundError is client_module.DeviceNotFoundError
    assert DeviceLockedError is client_module.DeviceLockedError


def test_default_remote_port_matches_the_server_default():
    assert DEFAULT_REMOTE_PORT == 7440


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


def test_find_returns_the_matching_device(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        device = client.find("alpha")

    assert device["uid"] == UID
    assert device["device_name"] == "alpha"


def test_find_unknown_uid_raises_device_not_found(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(DeviceNotFoundError) as excinfo:
            client.find("does-not-exist")

    assert excinfo.value.exit_code == EXIT_NO_DEVICE
    assert excinfo.value.code == "not_found"


# ---------------------------------------------------------------------------
# lock / unlock
# ---------------------------------------------------------------------------


def test_lock_then_unlock_round_trip(remote_server, locks):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        client.lock(UID, KIND_SERIAL)
        status = locks.status(UID)
        assert status.kind == KIND_SERIAL
        assert status.holder.origin == "remote"
        assert status.holder.host == "127.0.0.1"

        released = client.unlock(UID)

    assert released is True
    assert locks.status(UID) is None


def test_lock_unknown_kind_raises_invalid_request(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(InvalidRequestError) as excinfo:
            client.lock(UID, "nonsense")

    assert excinfo.value.exit_code == EXIT_USAGE
    assert excinfo.value.code == "invalid_request"


def test_lock_already_held_raises_device_locked_with_remote_holder_shape(remote_server):
    srv = remote_server()
    holder = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    holder.connect()
    holder.lock(UID, KIND_FLASH)

    contender = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    contender.connect()
    try:
        with pytest.raises(DeviceLockedError) as excinfo:
            contender.lock(UID, KIND_SERIAL)
    finally:
        contender.close()
        holder.unlock(UID)
        holder.close()

    assert excinfo.value.exit_code == EXIT_LOCKED
    assert excinfo.value.code == "locked"
    assert excinfo.value.holder["kind"] == KIND_FLASH
    assert excinfo.value.holder["pid"] is None
    assert excinfo.value.holder["origin"] == "remote"
    assert excinfo.value.holder["host"] == "127.0.0.1"


def test_unlock_when_not_held_by_this_connection_returns_false(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        released = client.unlock(UID)

    assert released is False


def test_unlock_unknown_uid_raises_device_not_found(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(DeviceNotFoundError):
            client.unlock("does-not-exist")


# ---------------------------------------------------------------------------
# session model
# ---------------------------------------------------------------------------


def test_closing_the_client_releases_locks_it_held(remote_server, locks):
    srv = remote_server()
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    client.lock(UID, KIND_SERIAL)
    assert locks.status(UID) is not None

    client.close()  # no explicit unlock

    _wait_until(lambda: locks.status(UID) is None)


def test_one_client_reuses_the_same_connection_across_calls(remote_server):
    srv = remote_server()
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    sock = client._sock  # noqa: SLF001 -- whitebox check that no reconnect happened

    client.find(UID)
    client.lock(UID, KIND_SERIAL)
    client.unlock(UID)

    assert client._sock is sock  # noqa: SLF001
    client.close()


def test_close_is_idempotent_and_safe_before_connect():
    client = RemoteRegistryClient("127.0.0.1", 1)
    client.close()
    client.close()


def test_connect_to_unreachable_port_raises_registry_unavailable():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    client = RemoteRegistryClient("127.0.0.1", port)
    with pytest.raises(RegistryUnavailable):
        client.connect()


# ---------------------------------------------------------------------------
# EXIT_* mapping -- table-driven, mirroring registry.client's own suite
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls, code, exit_code",
    [
        (DeviceNotFoundError, "not_found", EXIT_NO_DEVICE),
        (InvalidRequestError, "invalid_request", EXIT_USAGE),
        (DeviceLockedError, "locked", EXIT_LOCKED),
        (RegistryClientError, "internal_error", EXIT_ERROR),
    ],
)
def test_exception_code_maps_to_stable_exit_code(exc_cls, code, exit_code):
    if exc_cls is DeviceLockedError:
        exc = exc_cls(code, "message", {"kind": "flash", "pid": None, "origin": "remote"})
    else:
        exc = exc_cls(code, "message")
    assert exc.code == code
    assert exc.exit_code == exit_code


# ---------------------------------------------------------------------------
# auth token
# ---------------------------------------------------------------------------


def test_connect_with_correct_token_succeeds_and_dispatches_ops(remote_server):
    srv = remote_server(auth_token="s3cret")
    with RemoteRegistryClient("127.0.0.1", srv.bound_port, auth_token="s3cret") as client:
        device = client.find(UID)

    assert device["uid"] == UID


def test_connect_with_wrong_token_raises_registry_client_error(remote_server):
    srv = remote_server(auth_token="s3cret")
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port, auth_token="wrong")

    with pytest.raises(RegistryClientError) as excinfo:
        client.connect()

    assert excinfo.value.code == "unauthorized"


def test_connect_with_no_token_against_an_unauthenticated_server_works(remote_server):
    srv = remote_server(auth_token=None)
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        device = client.find(UID)

    assert device["uid"] == UID


# ---------------------------------------------------------------------------
# flash: send_hex + flash, streamed
# ---------------------------------------------------------------------------


def test_flash_success_streams_logs_and_returns_flash_result(
    monkeypatch, remote_server, store, locks, tmp_path
):
    def fake_popen(cmd, **kw):
        if "flash" in cmd:
            return _FakeProcess(0, ("erasing...", "programming..."))
        return _FakeProcess(0, ("resetting...",))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    srv = remote_server()
    logs: list[str] = []
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        client.lock(UID, KIND_FLASH)
        result = client.flash(UID, hex_path, log_callback=logs.append)

    assert result == FlashResult(success=True, exit_code=0, error=None)
    assert "erasing..." in logs
    assert "programming..." in logs
    assert store.find(UID).flash_count == 1
    assert locks.status(UID) is None


def test_flash_pyocd_failure_is_a_result_not_an_exception(
    monkeypatch, remote_server, store, locks, tmp_path
):
    def fake_popen(cmd, **kw):
        return _FakeProcess(1, ("some pyocd error",))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        client.lock(UID, KIND_FLASH)
        result = client.flash(UID, hex_path)

    assert result.success is False
    assert result.exit_code == 1
    assert store.find(UID).flash_count == 0


def test_flash_without_a_flash_lock_raises_not_locked(remote_server, tmp_path):
    hex_path = tmp_path / "firmware.hex"
    hex_path.write_text(_VALID_HEX_CONTENT)

    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(RegistryClientError) as excinfo:
            client.flash(UID, hex_path)

    assert excinfo.value.code == "not_locked"


# ---------------------------------------------------------------------------
# open_stream: binary data+control sub-protocol
# ---------------------------------------------------------------------------


def test_open_stream_without_a_prior_lock_raises_not_locked_and_client_stays_usable(
    remote_server,
):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(RegistryClientError) as excinfo:
            client.open_stream(UID)
        assert excinfo.value.code == "not_locked"

        # A failed "stream" precondition never left JSON framing -- this
        # connection is still usable for further ops.
        device = client.find(UID)

    assert device["uid"] == UID


def test_open_stream_unknown_uid_raises_device_not_found(remote_server):
    srv = remote_server()
    with RemoteRegistryClient("127.0.0.1", srv.bound_port) as client:
        with pytest.raises(DeviceNotFoundError):
            client.open_stream("no-such-device")


def test_open_stream_full_session(remote_server, locks, fake_serials):
    srv = remote_server()
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    client.lock(UID, KIND_SERIAL)

    stream = client.open_stream(UID)
    assert isinstance(stream, RemoteStream)

    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]
    assert ser.port == "/dev/ttyACM0"
    assert ser.dtr is False
    assert ser.rts is False

    stream.write(b"HELLO\n")
    _wait_until(lambda: ser.written == [b"HELLO\n"])

    stream.send_break()
    _wait_until(lambda: len(ser.break_calls) == 1)
    assert ser.break_calls[0] == pytest.approx(0.01)

    stream.set_dtr(True)
    _wait_until(lambda: ser.dtr is True)
    stream.set_dtr(False)
    _wait_until(lambda: ser.dtr is False)

    stream.set_rts(True)
    _wait_until(lambda: ser.rts is True)
    stream.set_rts(False)
    _wait_until(lambda: ser.rts is False)

    stream.close()
    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(UID) is None)


def test_open_stream_reads_data_the_port_produces(remote_server, locks, fake_serials):
    def _factory(**serial_kwargs):
        fs = FakeSerial(announcement="board says hi", **serial_kwargs)
        fake_serials.append(fs)
        return fs

    srv = remote_server(serial_factory=_factory)
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    client.lock(UID, KIND_SERIAL)

    stream = client.open_stream(UID)

    data = stream.read()
    assert data == b"board says hi\n"

    stream.close()
    _wait_until(lambda: locks.status(UID) is None)


def test_open_stream_transfers_the_connection_so_the_client_has_no_socket_left(
    remote_server,
):
    srv = remote_server()
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    client.lock(UID, KIND_SERIAL)

    stream = client.open_stream(UID)

    assert client._sock is None  # noqa: SLF001 -- whitebox: ownership moved
    client.close()  # inert -- no connection left to close
    stream.close()


def test_plain_disconnect_of_the_stream_closes_the_port_and_releases_the_lock(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = RemoteRegistryClient("127.0.0.1", srv.bound_port)
    client.connect()
    client.lock(UID, KIND_SERIAL)
    stream = client.open_stream(UID)
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    stream._sock.close()  # noqa: SLF001 -- simulate a plain connection drop, no CLOSE frame

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(UID) is None)
