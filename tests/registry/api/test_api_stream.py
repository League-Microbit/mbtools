"""Tests for mbtools.registry.api's "stream" op (sprint 008, ticket 004)
-- the local Unix-socket half of the framed binary data+control
sub-protocol (mbtools.registry.stream_frame) that ticket 007 (sprint 003)
originally gave only to the remote TCP port. Ticket 004 relocated
``_op_stream_precheck``/``_handle_stream`` into the shared
``_api_base.BaseAPIServer`` mixin so ``api.RegistryAPIServer`` can
dispatch ``stream`` into the exact same implementation, mechanics
unchanged (see ``tests/registry/remote_api/test_remote_stream.py``'s
own docstring for the remote-port precedent this mirrors).

Every test here drives a real ``RegistryAPIServer`` over a real
``AF_UNIX`` socket in a short ``tmp_path``-style directory (mirrors
``tests/registry/api/test_api.py``'s own precedent), with the local
port backed by ``mbtools.testing.fakes.FakeSerial`` via an injected
``serial_factory`` rather than any real hardware.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time

import pytest

from mbtools.common import CODE_INVALID_REQUEST, CODE_NOT_FOUND, CODE_NOT_LOCKED
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_RELAY, KIND_SERIAL, LockManager
from mbtools.registry.store import Store
from mbtools.registry.stream_frame import (
    FRAME_BREAK,
    FRAME_CLOSE,
    FRAME_DATA,
    FRAME_SET_DTR,
    FRAME_SET_RTS,
    MAX_FRAME_PAYLOAD,
    encode_frame,
    read_frame,
)
from mbtools.testing.fakes import FakeSerial

pytestmark = pytest.mark.requires_af_unix

LOCAL_UID = "cc00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PORT_PATH = "/dev/ttyACM0"
PID_A = 2001
PID_B = 2002


# ---------------------------------------------------------------------------
# test doubles / helpers
# ---------------------------------------------------------------------------


def _sequential_peer_pid_fn(pids):
    """A ``peer_pid_fn`` that hands out ``pids`` in connection-accept
    order -- mirrors ``test_api.py``'s own identical helper. Needed here
    because the real ``SO_PEERCRED``/``LOCAL_PEERPID`` mechanism
    (this server's default) reports the *same* real OS pid for every
    connection this one test process opens, so "two different
    connections" tests need an injected pid sequence to actually get two
    different local :class:`~mbtools.registry.locks.HolderRef`\\ s --
    unlike the remote TCP port, where every connection mints its own
    random session id regardless of which process opened it.
    """
    it = iter(pids)

    def fn(_sock: socket.socket) -> int:
        return next(it)

    return fn


class _StreamClient:
    """A newline-JSON client over a real ``AF_UNIX`` socket that can also
    switch into the binary frame sub-protocol once a "stream" op has been
    acknowledged -- mirrors
    ``tests/registry/remote_api/test_remote_stream.py``'s own
    ``_StreamClient`` (same client-side sequencing rules: wait for each
    response before sending the next thing), just over ``AF_UNIX``
    instead of ``AF_INET``."""

    def __init__(self, socket_path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(str(socket_path))
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj))
        self.wfile.write("\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("mbregistry api: connection closed")
        return json.loads(line)

    def request(self, obj: dict) -> dict:
        self.send(obj)
        return self.recv()

    # -- binary frame sub-protocol (ticket 007/004) -- raw socket, not rfile/wfile --

    def send_frame(self, frame_type: int, payload: bytes = b"") -> None:
        self.sock.sendall(encode_frame(frame_type, payload))

    def send_raw(self, data: bytes) -> None:
        self.sock.sendall(data)

    def recv_frame(self) -> tuple[int, bytes] | None:
        def _read_exact(n: int) -> bytes:
            chunks: list[bytes] = []
            remaining = n
            while remaining > 0:
                chunk = self.sock.recv(remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        return read_frame(_read_exact)

    def close(self) -> None:
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass
        self.sock.close()


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
    s.upsert_attached(LOCAL_UID, PORT_PATH, VID_PID)
    s.apply_probe_result(
        LOCAL_UID,
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="123", raw="DEVICE:alpha"
        ),
    )
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def socket_dir():
    """A short-path temp dir for the AF_UNIX socket file -- see
    ``test_api.py``'s own identical fixture docstring for why this can't
    just be pytest's own ``tmp_path``."""
    d = tempfile.mkdtemp(prefix="mbregistry-api-stream-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def fake_serials():
    """Every FakeSerial the server's injected serial_factory has
    constructed, in open() order."""
    return []


@pytest.fixture
def make_server(socket_dir, store, locks, fake_serials):
    servers: list[RegistryAPIServer] = []

    def _factory(**serial_kwargs):
        fs = FakeSerial(**serial_kwargs)
        fake_serials.append(fs)
        return fs

    def _make(*, serial_factory=None, break_duration_s=0.01, peer_pid_fn=None):
        flash_op = FlashOp(locks=locks, store=store, runner=lambda cmd, log: 0)
        srv = RegistryAPIServer(
            socket_path=f"{socket_dir}/api.sock",
            store=store,
            locks=locks,
            flash_op=flash_op,
            peer_pid_fn=peer_pid_fn,
            sweep_interval_s=100.0,
            serial_factory=serial_factory if serial_factory is not None else _factory,
            stream_settle_s=0,
            break_duration_s=break_duration_s,
        )
        srv.start()
        servers.append(srv)
        return srv

    yield _make
    for srv in servers:
        srv.stop()


def _lock_serial(client: _StreamClient) -> None:
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_SERIAL})
    assert resp["ok"] is True


# ---------------------------------------------------------------------------
# preconditions
# ---------------------------------------------------------------------------


def test_stream_without_a_prior_lock_is_not_locked(make_server):
    srv = make_server()
    client = _StreamClient(srv.socket_path)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    client.close()


def test_stream_with_a_different_kind_lock_is_not_locked(make_server):
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})
    assert resp["ok"] is True

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    client.close()


def test_stream_with_another_connections_serial_lock_is_not_locked(make_server):
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A, PID_B]))
    holder = _StreamClient(srv.socket_path)
    _lock_serial(holder)
    contender = _StreamClient(srv.socket_path)

    resp = contender.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    holder.close()
    contender.close()


def test_stream_with_a_relay_kind_lock_is_accepted(make_server):
    """Same widened precondition ticket 011 gave the remote port --
    inherited here unchanged (see ``_api_base.BaseAPIServer
    ._op_stream_precheck``'s own docstring)."""
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_RELAY})
    assert resp["ok"] is True

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp == {"ok": True}
    client.close()


def test_stream_requires_uid(make_server):
    srv = make_server()
    client = _StreamClient(srv.socket_path)

    resp = client.request({"op": "stream"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_stream_unknown_uid_is_not_found(make_server):
    srv = make_server()
    client = _StreamClient(srv.socket_path)

    resp = client.request({"op": "stream", "uid": "no-such-device"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


# ---------------------------------------------------------------------------
# a full session: open, write, read, BREAK, SET_DTR, SET_RTS, close
# ---------------------------------------------------------------------------


def test_full_stream_session(make_server, locks, fake_serials):
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    _lock_serial(client)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}

    # The server opened the local port with DTR/RTS held low (no reboot),
    # via serial.connect.open_no_reboot, reused rather than duplicated --
    # identical to the remote port's own behavior.
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]
    assert ser.port == PORT_PATH
    assert ser.open_calls == 1
    assert ser.dtr is False
    assert ser.rts is False

    # DATA client -> server -> port.write().
    client.send_frame(FRAME_DATA, b"HELLO\n")
    _wait_until(lambda: ser.written == [b"HELLO\n"])

    # BREAK -> a real serial break on the server's local port.
    client.send_frame(FRAME_BREAK)
    _wait_until(lambda: len(ser.break_calls) == 1)
    assert ser.break_calls[0] == pytest.approx(0.01)

    # SET_DTR / SET_RTS -> the corresponding line, directly.
    client.send_frame(FRAME_SET_DTR, bytes([1]))
    _wait_until(lambda: ser.dtr is True)
    client.send_frame(FRAME_SET_DTR, bytes([0]))
    _wait_until(lambda: ser.dtr is False)

    client.send_frame(FRAME_SET_RTS, bytes([1]))
    _wait_until(lambda: ser.rts is True)
    client.send_frame(FRAME_SET_RTS, bytes([0]))
    _wait_until(lambda: ser.rts is False)

    # CLOSE -> the local port is closed and the serial-kind lock released.
    client.send_frame(FRAME_CLOSE)
    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    client.close()


def test_data_from_the_port_is_forwarded_to_the_client(make_server, locks, fake_serials):
    def _factory(**serial_kwargs):
        fs = FakeSerial(announcement="board says hi", **serial_kwargs)
        fake_serials.append(fs)
        return fs

    srv = make_server(serial_factory=_factory)
    client = _StreamClient(srv.socket_path)
    _lock_serial(client)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}

    frame = client.recv_frame()
    assert frame == (FRAME_DATA, b"board says hi\n")

    client.send_frame(FRAME_CLOSE)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    client.close()


def test_plain_connection_drop_closes_the_port_and_releases_the_lock(
    make_server, locks, fake_serials
):
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    _lock_serial(client)
    resp = client.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    client.close()  # no CLOSE frame -- just drop the connection

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)


# ---------------------------------------------------------------------------
# fuzz / boundary: malformed frames must never crash the handler thread
# or leak the lock -- same guarantee the remote port's own ticket 007
# acceptance criterion required, now proven for this transport too.
# ---------------------------------------------------------------------------


def test_truncated_frame_header_closes_the_session_without_crashing(
    make_server, locks, fake_serials
):
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    _lock_serial(client)
    client.request({"op": "stream", "uid": LOCAL_UID})
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    client.send_raw(bytes([FRAME_DATA]) + b"\x00\x00")  # 3 of 5 header bytes
    client.close()  # then vanish -- the frame is truncated, not just slow

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)

    # The handler thread survived (didn't crash) and the server is still
    # serving other connections/other sessions on this same uid.
    second = _StreamClient(srv.socket_path)
    resp = second.request({"op": "list"})
    assert resp["ok"] is True
    _lock_serial(second)
    resp = second.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}
    second.close()


def test_oversized_declared_length_closes_the_session_without_crashing(
    make_server, locks, fake_serials
):
    srv = make_server()
    client = _StreamClient(srv.socket_path)
    _lock_serial(client)
    client.request({"op": "stream", "uid": LOCAL_UID})
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    header = bytes([FRAME_DATA]) + (MAX_FRAME_PAYLOAD + 1).to_bytes(4, "big")
    client.send_raw(header)  # never sends the (absurd) declared payload
    client.close()

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)

    # server survived -- another connection can still be served
    second = _StreamClient(srv.socket_path)
    resp = second.request({"op": "list"})
    assert resp["ok"] is True
    second.close()


# ---------------------------------------------------------------------------
# handoff from ticket 003: unlock --force against a live local-socket
# stream session
# ---------------------------------------------------------------------------


def test_force_unlock_closes_a_live_stream_session_and_the_holder_sees_eof(
    make_server, locks, fake_serials
):
    """sprint.md Step 3's predicted interaction, verified rather than
    assumed (ticket 004's own acceptance criteria): ``api.py``'s per-uid
    ``{uid: connection}`` map (ticket 003) is populated at ``lock`` time
    and covers the same connection object whether or not it is later
    switched into ``stream`` mode -- so ``force_unlock`` against a live
    streaming holder should already work with no further change. This
    proves it: the lock is released, the local port is closed, the
    holder's blocked frame reader observes EOF (not a hang or a
    traceback), and the pump thread stops cleanly.
    """
    srv = make_server(peer_pid_fn=_sequential_peer_pid_fn([PID_A, PID_B, PID_B]))
    holder = _StreamClient(srv.socket_path)
    _lock_serial(holder)
    resp = holder.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    # A live session: the holder can still exchange DATA frames right up
    # until it gets force-unlocked.
    holder.send_frame(FRAME_DATA, b"still streaming\n")
    _wait_until(lambda: ser.written == [b"still streaming\n"])

    admin = _StreamClient(srv.socket_path)
    force_resp = admin.request({"op": "force_unlock", "uid": LOCAL_UID})
    assert force_resp["ok"] is True
    assert force_resp["released"] is True
    assert force_resp["kind"] == KIND_SERIAL

    # The holder's own blocked frame read observes EOF -- read_frame
    # returns None on a clean EOF, exactly like a plain connection drop.
    frame = holder.recv_frame()
    assert frame is None

    # The lock is released, the port is closed, and the pump thread
    # stopped -- no crash, no traceback, no hang.
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    _wait_until(lambda: ser.close_calls == 1)

    admin.close()
    holder.close()

    # The server is still alive and can serve a fresh session on the
    # same uid.
    second = _StreamClient(srv.socket_path)
    resp = second.request({"op": "list"})
    assert resp["ok"] is True
    second.close()
