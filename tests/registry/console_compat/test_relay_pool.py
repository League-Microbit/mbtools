"""Tests for mbtools.registry.console_compat.relay_pool (sprint 004,
ticket 006) -- the ``_mbrelay._tcp`` pool-port TCP listener.

Mirrors ``tests/registry/remote_api/test_remote_stream.py``'s own
precedent ("network services with an existing precedent" -- sprint.md's
Test Strategy): the pool is driven over a real AF_INET loopback socket
(``port=0``, an OS-chosen ephemeral port via ``bound_port``), with the
local relay's serial port backed by a scripted fake
(:class:`ScriptedSerial`, adapted from ``tests/relay/test_channel.py``'s
own fake of the same name and self-contained here for the same reason
that file gives -- a test file duplicates small fixture data rather than
importing across test modules, and test module basenames must stay
globally unique across ``tests/``).

mDNS is exercised against a fake ``zeroconf``-module-like namespace
(mirroring ``tests/registry/peering/test_peering.py``'s own
``_FakeZeroconfNamespace``), so the TXT/SRV-shape test never opens a real
mDNS socket.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

import pytest

from mbtools.registry.console_compat.relay_pool import (
    DEFAULT_NAMES_API_PORT,
    SERVICE_TYPE,
    TXT_REGISTRY_PORT,
    RelayPool,
)
from mbtools.registry.locks import KIND_RELAY, LockManager
from mbtools.registry.identity import ProbeResult
from mbtools.registry.store import STATE_CONNECTED, Store
from mbtools.relay.channel import LocalRelayChannel
from mbtools.relay.protocol import NORMALIZE_STEPS, RelayControl

LOCAL_UID = "aa00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
REMOTE_UID = "bb00" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"
PORT_PATH = "/dev/fake-relay0"

RADIOBRIDGE_BANNER = b"DEVICE:RADIOBRIDGE:relay:togov:1234\n"

NORMALIZE_ACKS: dict[bytes, bytes] = {
    b"!MODE RAW250\n": b"# mode: RAW250\n",
    b"!FRAG OFF\n": b"# frag: OFF\n",
    b"!ECHO OFF\n": b"# echo: OFF\n",
    b"!P 7\n": b"# channel: 0 group: 10 mode: RAW250 power: 7\n",
    b"!C 0\n": b"# channel: 0 group: 10\n",
}
DEFAULT_QUERY_REPLY = b"# channel: 0 group: 10 mode: RAW250 power: 7\n"


def relay_script() -> dict:
    """Every command a full acquire-or-release ``reset_and_normalize``
    cycle sends, scripted with its ack -- HELLO, !VER?, the normalize
    batch, its verification query, and !DEFAULTS (only sent on release,
    but harmless to script unconditionally)."""
    script = dict(NORMALIZE_ACKS)
    script[b"HELLO\n"] = RADIOBRIDGE_BANNER
    script[b"!VER?\n"] = b"# version: 1.2.3\n"
    script[b"?\n"] = DEFAULT_QUERY_REPLY
    script[b"!DEFAULTS\n"] = b"# stored config cleared\n"
    return script


class ScriptedSerial:
    """A pyserial-shaped fake driven by a command -> reply script --
    adapted from ``tests/relay/test_channel.py``'s own fake of the same
    name (see that module's own docstring for the shape this mirrors)."""

    def __init__(self, *, script: "dict[bytes, Any] | None" = None,
                 read_delay: float = 0.005, **serial_kwargs: object) -> None:
        self.script = dict(script or {})
        self._read_delay = read_delay
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.written: list[bytes] = []
        self.open_calls = 0
        self.close_calls = 0
        self.is_open = False
        self._dtr = False
        self._rts = False
        for key, value in serial_kwargs.items():
            setattr(self, key, value)
        self.port = serial_kwargs.get("port")

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value

    def open(self) -> None:
        self.open_calls += 1
        self.is_open = True

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def write(self, data: bytes) -> int:
        self.written.append(data)
        reply = self.script.get(data)
        if reply:
            with self._lock:
                self._buf.extend(reply)
        return len(data)

    def send_break(self, duration: float = 0.4) -> None:
        pass

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            if self._buf:
                n = min(size, len(self._buf))
                data = bytes(self._buf[:n])
                del self._buf[:n]
                return data
        time.sleep(self._read_delay)
        return b""

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass


def make_control() -> RelayControl:
    """Timeouts generous enough for a real background read thread but
    small enough to keep the suite fast -- same rationale as
    ``test_channel.py``'s own ``make_control``."""
    return RelayControl(open_settle=0.0, hello_timeout=0.5, hello_attempts=2,
                        post_close_settle=0.0, break_duration=0.05, break_settle=0.05)


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


def _recv_until(sock: socket.socket, marker: bytes, timeout: float = 2.0) -> bytes:
    sock.settimeout(timeout)
    buf = bytearray()
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


def _attach_local_relay(store: Store, uid: str = LOCAL_UID, port: str = PORT_PATH) -> None:
    store.upsert_attached(uid, port, VID_PID)
    store.apply_probe_result(
        uid,
        ProbeResult(role="RADIOBRIDGE", common_name="relay", device_name="togov",
                    serial="1234", raw="DEVICE:RADIOBRIDGE:relay:togov:1234"),
    )


def make_pool(store: Store, locks: LockManager, *, fake: "ScriptedSerial | None" = None,
              zeroconf: Any = None, **kwargs) -> RelayPool:
    fake = fake if fake is not None else ScriptedSerial(script=relay_script())

    def channel_factory(port: str) -> LocalRelayChannel:
        return LocalRelayChannel(port, open_settle=0.0, serial_factory=lambda **_kw: fake)

    return RelayPool(
        store=store,
        locks=locks,
        host="127.0.0.1",
        port=0,
        control=make_control(),
        channel_factory=channel_factory,
        zeroconf=zeroconf,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# fake zeroconf namespace (mirrors tests/registry/peering's own)
# ---------------------------------------------------------------------------


class _FakeServiceInfo:
    def __init__(self, type_, name, *, addresses, port, properties, server):
        self.type_ = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.properties = properties
        self.server = server


class _FakeZeroconf:
    def __init__(self):
        self.registered: list = []
        self.unregistered: list = []
        self.closed = False

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        self.unregistered.append(info)

    def close(self):
        self.closed = True


class _FakeZeroconfNamespace:
    def __init__(self):
        self.instance: "_FakeZeroconf | None" = None

    def Zeroconf(self):
        self.instance = _FakeZeroconf()
        return self.instance

    def ServiceInfo(self, type_, name, **kwargs):
        return _FakeServiceInfo(type_, name, **kwargs)


# ---------------------------------------------------------------------------
# the raw TCP contract (acceptance criteria 1, 2, 4)
# ---------------------------------------------------------------------------


def test_connection_gets_banner_first_then_raw_pipe(store, locks):
    _attach_local_relay(store)
    fake = ScriptedSerial(script=relay_script())
    pool = make_pool(store, locks, fake=fake, zeroconf=_FakeZeroconfNamespace())
    pool.start()
    try:
        client = socket.create_connection(("127.0.0.1", pool.bound_port), timeout=2.0)
        try:
            banner = _recv_until(client, b"\n")
            assert banner == RADIOBRIDGE_BANNER.rstrip(b"\n") + b"\r\n"

            # The acquire-time reset+normalize ran before the banner was
            # sent, in the documented order, with no !DEFAULTS (acquire
            # never clears stored config -- only release does).
            step_commands = [cmd for cmd, _ in NORMALIZE_STEPS]
            assert fake.written == [b"HELLO\n", b"!VER?\n", *step_commands, b"?\n"]

            # Once the banner has arrived, the connection is a raw,
            # transparent byte pipe: a client write reaches the channel
            # directly, with no framing.
            client.sendall(b"raw-payload")
            _wait_until(lambda: b"raw-payload" in fake.written)
        finally:
            client.close()

        # Reset-by-reconnect (acceptance criterion 2): on disconnect the
        # relay is reset/normalized again, this time with !DEFAULTS, and
        # the lock is released before being offered to the next
        # connection.
        _wait_until(lambda: b"!DEFAULTS\n" in fake.written)
        assert fake.written.count(b"HELLO\n") == 2
        assert fake.written[-1] == b"!DEFAULTS\n"
        _wait_until(lambda: locks.status(LOCAL_UID) is None)
    finally:
        pool.stop()


def test_no_free_relay_gets_legacy_error_line_and_closes(store, locks):
    # No relay attached at all -- nothing for the pool to offer.
    pool = make_pool(store, locks, zeroconf=_FakeZeroconfNamespace())
    pool.start()
    try:
        client = socket.create_connection(("127.0.0.1", pool.bound_port), timeout=2.0)
        try:
            reply = _recv_until(client, b"\n")
            assert reply.startswith(b"# ERROR: no relay available")
            # The connection is then closed -- never left hanging.
            client.settimeout(2.0)
            assert client.recv(1) == b""
        finally:
            client.close()
    finally:
        pool.stop()


def test_locked_relay_is_never_offered(store, locks):
    """A relay already held by someone else is not free -- the pool must
    never double-book a lock (acceptance criterion "never serves a
    peer-owned relay" generalizes to "never serves an already-locked
    one")."""
    _attach_local_relay(store)
    from mbtools.registry.locks import HolderRef

    locks.acquire(LOCAL_UID, KIND_RELAY, HolderRef(origin="local", ref="other", pid=1))
    pool = make_pool(store, locks, zeroconf=_FakeZeroconfNamespace())
    pool.start()
    try:
        client = socket.create_connection(("127.0.0.1", pool.bound_port), timeout=2.0)
        try:
            reply = _recv_until(client, b"\n")
            assert reply.startswith(b"# ERROR: no relay available")
        finally:
            client.close()
    finally:
        pool.stop()


def test_remote_owned_relay_is_never_offered(store, locks):
    """A peer-owned relay (``host`` set) must never be served locally
    (architecture Decision 5 / acceptance criterion)."""
    store.upsert_remote_attached(REMOTE_UID, "peerhost", PORT_PATH, VID_PID)
    store.apply_remote_probe(
        REMOTE_UID,
        ProbeResult(role="RADIOBRIDGE", common_name="relay", device_name="remrelay",
                    serial="9999", raw="DEVICE:RADIOBRIDGE:relay:remrelay:9999"),
    )
    pool = make_pool(store, locks, zeroconf=_FakeZeroconfNamespace())
    pool.start()
    try:
        client = socket.create_connection(("127.0.0.1", pool.bound_port), timeout=2.0)
        try:
            reply = _recv_until(client, b"\n")
            assert reply.startswith(b"# ERROR: no relay available")
        finally:
            client.close()
    finally:
        pool.stop()


# ---------------------------------------------------------------------------
# mDNS advertisement shape (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_mdns_advertisement_matches_robot_console_expectations(store, locks):
    """Service type, SRV port (the *live* bound socket, not config), and
    TXT ``registry=<names_api port>`` must exactly match what
    ``discovery/mdnsDiscovery.ts``/``watchers/mdnsWatcher.ts`` parse --
    cross-checked against that source directly (module docstring)."""
    ns = _FakeZeroconfNamespace()
    pool = make_pool(store, locks, zeroconf=ns, names_api_port=7999,
                     instance_host="torture")
    pool.start()
    try:
        assert ns.instance is not None
        assert len(ns.instance.registered) == 1
        info = ns.instance.registered[0]
        assert info.type_ == SERVICE_TYPE == "_mbrelay._tcp.local."
        assert info.name == "torture._mbrelay._tcp.local."
        assert info.server == "torture.local."
        # SRV port is the pool's own live bound port, read back from the
        # socket -- never the (here, 0/ephemeral) configured port.
        assert info.port == pool.bound_port
        assert info.port != 0
        assert info.properties[TXT_REGISTRY_PORT.encode()] == b"7999"
    finally:
        pool.stop()
    assert ns.instance.unregistered == [info]
    assert ns.instance.closed is True


def test_default_names_api_port_is_advertised_when_not_overridden(store, locks):
    ns = _FakeZeroconfNamespace()
    pool = make_pool(store, locks, zeroconf=ns)
    pool.start()
    try:
        info = ns.instance.registered[0]
        assert info.properties[TXT_REGISTRY_PORT.encode()] == str(DEFAULT_NAMES_API_PORT).encode()
    finally:
        pool.stop()


def test_start_stop_idempotent(store, locks):
    pool = make_pool(store, locks, zeroconf=_FakeZeroconfNamespace())
    pool.start()
    pool.start()  # no-op
    port = pool.bound_port
    pool.stop()
    pool.stop()  # no-op
    assert port != 0
