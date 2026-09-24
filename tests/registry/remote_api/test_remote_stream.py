"""Tests for mbtools.registry.remote_api's "stream" op (ticket 007;
widened to accept a relay-kind lock too in ticket 011) -- the framed
binary data+control sub-protocol (mbtools.registry.stream_frame) a TCP
connection to RemoteAPIServer switches into once it holds a serial- or
relay-kind lock and sends {"op": "stream", "uid": "..."}.

Every test here drives a real RemoteAPIServer over a real AF_INET
loopback socket, mirroring test_remote_api.py's own precedent, with the
local port backed by mbtools.testing.fakes.FakeSerial via an injected
serial_factory (the same fixture serial.connect's own tests use) rather
than any real hardware.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from mbtools.common import CODE_INVALID_REQUEST, CODE_NOT_FOUND, CODE_NOT_LOCKED
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_RELAY, KIND_SERIAL, LockManager
from mbtools.registry.remote_api import RemoteAPIServer
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

LOCAL_UID = "aa00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PORT_PATH = "/dev/ttyACM0"


# ---------------------------------------------------------------------------
# test doubles / helpers
# ---------------------------------------------------------------------------


class _StreamClient:
    """A newline-JSON client (mirrors test_remote_api.py's own _Client)
    that can also switch into the binary frame sub-protocol once a
    "stream" op has been acknowledged -- exactly the sequence a real
    client (ticket 011's registry.remote_client) follows: wait for each
    response before sending the next thing, per remote_api's own
    protocol-synchronization note."""

    def __init__(self, port: int, host: str = "127.0.0.1"):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.rfile = self.sock.makefile("r", encoding="utf-8", newline="\n")
        self.wfile = self.sock.makefile("w", encoding="utf-8", newline="\n")

    def send(self, obj: dict) -> None:
        self.wfile.write(json.dumps(obj))
        self.wfile.write("\n")
        self.wfile.flush()

    def recv(self) -> dict:
        line = self.rfile.readline()
        if not line:
            raise ConnectionError("mbregistry remote_api: connection closed")
        return json.loads(line)

    def request(self, obj: dict) -> dict:
        self.send(obj)
        return self.recv()

    # -- binary frame sub-protocol (ticket 007) -- raw socket, not rfile/wfile --

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
def fake_serials():
    """Every FakeSerial the server's injected serial_factory has
    constructed, in open() order -- lets a test inspect/script the one
    the server actually opened for a given stream session."""
    return []


@pytest.fixture
def remote_server(store, locks, fake_serials):
    servers: list[RemoteAPIServer] = []

    def _factory(**serial_kwargs):
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


def test_stream_without_a_prior_lock_is_not_locked(remote_server):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    client.close()


def test_stream_with_a_different_kind_lock_is_not_locked(remote_server):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_FLASH})
    assert resp["ok"] is True

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    client.close()


def test_stream_with_another_connections_serial_lock_is_not_locked(remote_server):
    srv = remote_server()
    holder = _StreamClient(srv.bound_port)
    _lock_serial(holder)
    contender = _StreamClient(srv.bound_port)

    resp = contender.request({"op": "stream", "uid": LOCAL_UID})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_LOCKED
    holder.close()
    contender.close()


def test_stream_with_a_relay_kind_lock_is_accepted(remote_server):
    """Regression test for ticket 011's real-hardware finding: this
    precheck used to accept only ``KIND_SERIAL``, so
    ``relay.channel.RemoteRelayChannel`` (ticket 004) -- which locks a
    remote relay device ``relay``-kind before calling this exact op --
    got an uncaught ``not_locked`` error the first time ``mbrelay
    connect <robot>@<host>`` was run against a real, different-host
    relay. Every unit test on both sides of this RPC used a fake peer
    (``tests/relay/test_cli.py``'s ``FakeRemoteRegistryClient``), so
    this never surfaced until real hardware. See remote_api.py's
    ``_op_stream_precheck`` docstring and docs/design/registry-api.md's
    "Stream sub-protocol" section for the full story."""
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    resp = client.request({"op": "lock", "uid": LOCAL_UID, "kind": KIND_RELAY})
    assert resp["ok"] is True

    resp = client.request({"op": "stream", "uid": LOCAL_UID})

    assert resp == {"ok": True}
    client.close()


def test_stream_requires_uid(remote_server):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)

    resp = client.request({"op": "stream"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_INVALID_REQUEST
    client.close()


def test_stream_unknown_uid_is_not_found(remote_server):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)

    resp = client.request({"op": "stream", "uid": "no-such-device"})

    assert resp["ok"] is False
    assert resp["code"] == CODE_NOT_FOUND
    client.close()


# ---------------------------------------------------------------------------
# a full session: open, write, read, BREAK, SET_DTR, SET_RTS, close
# ---------------------------------------------------------------------------


def test_full_stream_session(remote_server, locks, fake_serials):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    _lock_serial(client)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}

    # The server opened the local port with DTR/RTS held low (no reboot),
    # via serial.connect.open_no_reboot, reused rather than duplicated.
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


def test_data_from_the_port_is_forwarded_to_the_client(remote_server, locks, fake_serials):
    def _factory(**serial_kwargs):
        fs = FakeSerial(announcement="board says hi", **serial_kwargs)
        fake_serials.append(fs)
        return fs

    srv = remote_server(serial_factory=_factory)
    client = _StreamClient(srv.bound_port)
    _lock_serial(client)

    resp = client.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}

    frame = client.recv_frame()
    assert frame == (FRAME_DATA, b"board says hi\n")

    client.send_frame(FRAME_CLOSE)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    client.close()


def test_plain_connection_drop_closes_the_port_and_releases_the_lock(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
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
# or leak the lock (ticket 007's own acceptance criterion)
# ---------------------------------------------------------------------------


def test_zero_length_data_frame_is_accepted_and_the_session_continues(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    _lock_serial(client)
    client.request({"op": "stream", "uid": LOCAL_UID})
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    client.send_frame(FRAME_DATA, b"")  # zero-length payload
    client.send_frame(FRAME_DATA, b"still alive")
    _wait_until(lambda: ser.written == [b"", b"still alive"])

    client.send_frame(FRAME_CLOSE)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    client.close()


def test_truncated_frame_header_closes_the_session_without_crashing(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
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
    second = _StreamClient(srv.bound_port)
    resp = second.request({"op": "list"})
    assert resp["ok"] is True
    _lock_serial(second)
    resp = second.request({"op": "stream", "uid": LOCAL_UID})
    assert resp == {"ok": True}
    second.close()


def test_truncated_frame_payload_closes_the_session_without_crashing(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    _lock_serial(client)
    client.request({"op": "stream", "uid": LOCAL_UID})
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    header = bytes([FRAME_DATA]) + (100).to_bytes(4, "big")
    client.send_raw(header + b"only ten b")  # declares 100 bytes, sends 10
    client.close()

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)


def test_oversized_declared_length_closes_the_session_without_crashing(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
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
    second = _StreamClient(srv.bound_port)
    resp = second.request({"op": "list"})
    assert resp["ok"] is True
    second.close()


def test_unknown_frame_type_closes_the_session_without_crashing(
    remote_server, locks, fake_serials
):
    srv = remote_server()
    client = _StreamClient(srv.bound_port)
    _lock_serial(client)
    client.request({"op": "stream", "uid": LOCAL_UID})
    _wait_until(lambda: len(fake_serials) == 1)
    ser = fake_serials[0]

    client.send_frame(0xFF)  # not one of FRAME_DATA/BREAK/SET_DTR/SET_RTS/CLOSE

    _wait_until(lambda: ser.close_calls == 1)
    _wait_until(lambda: locks.status(LOCAL_UID) is None)
    client.close()
