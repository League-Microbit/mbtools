"""Tests for sprint 008 ticket 001's own additions to
``registry.cli.assemble_registry``: the always-constructed
``registry.eventbus.EventBus`` and the four adapter closures
(``_event_callback``/``_lock_display_callback``/``_name_set_callback``/
``_name_clear_callback``) that publish to it and, only when a
``PeerDiscovery`` was actually constructed, also call the matching
``PeerDiscovery.publish_*``.

Per sprint.md's Design Rationale (Decision 3) and ticket 001's own
Implementation Notes, the closures are the right unit to test directly
for "peering still gets called when it's on" / "the bus still gets
called when peering is off" -- this module does both, plus one
end-to-end test proving a real ``watch`` client on the local socket
actually observes attach/lock/name events with no ZeroMQ involved
(SUC-001's own ``--no-peering`` scenario).

Follows tests/registry/cli/test_cli_run_peering.py's own conventions
(fake zeroconf, real loopback zmq, ``FakeUSBSource``), duplicated here
per this project's "one fixture set per test module" precedent rather
than imported.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import time

import pytest
import zmq

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry.cli import assemble_registry
from mbtools.registry.locks import KIND_SERIAL, HolderRef
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeUSBSource, unavailable_chip_identity_session_factory

VID, PID_ = DAPLINK_VID_PID


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


def _wait_until(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


class _Client:
    """A minimal newline-delimited-JSON client over a real AF_UNIX
    socket, mirroring tests/registry/api/test_api.py's own ``_Client``."""

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

    def close(self) -> None:
        for f in (self.wfile, self.rfile):
            try:
                f.close()
            except OSError:
                pass
        self.sock.close()


# ---------------------------------------------------------------------------
# fake zeroconf -- no real mDNS socket opened, same shape as
# test_cli_run_peering.py's own fake.
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
    def register_service(self, info, allow_name_change=False):
        pass

    def unregister_service(self, info):
        pass

    def close(self):
        pass


class _FakeServiceBrowser:
    def __init__(self, zc, type_, listener=None):
        pass

    def cancel(self):
        pass


class _FakeZeroconfNamespace:
    ServiceInfo = staticmethod(_FakeServiceInfo)
    ServiceBrowser = staticmethod(_FakeServiceBrowser)

    def __init__(self):
        self.instance = _FakeZeroconf()

    def Zeroconf(self):
        return self.instance


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-run-eventbus-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# closures: bus-only under --no-peering
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_event_callback_publishes_to_bus_only_when_no_peering(tmp_path, socket_dir):
    uid = _uid("noevt111")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        no_peering=True,
    )
    assert peering is None
    q = api._eventbus.subscribe()
    try:
        daemon.run_once()  # attaches uid -> fires the "attach" event hook

        event = q.get(timeout=2.0)
        assert event["type"] == "attach"
        assert event["uid"] == uid
    finally:
        store.close()


@pytest.mark.requires_af_unix
def test_lock_display_callback_publishes_to_bus_only_when_no_peering(tmp_path, socket_dir):
    uid = _uid("noevt222")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        no_peering=True,
    )
    assert peering is None
    q = api._eventbus.subscribe()
    try:
        daemon.locks.acquire(uid, KIND_SERIAL, HolderRef(origin="local", ref="1", pid=1))

        event = q.get(timeout=2.0)
        assert event == {
            "type": "lock_state",
            "host": event["host"],
            "uid": uid,
            "kind": KIND_SERIAL,
            "display": event["display"],
            "label": None,
            "since": event["since"],
        }
    finally:
        store.close()


@pytest.mark.requires_af_unix
def test_name_callbacks_publish_to_bus_only_when_no_peering(tmp_path, socket_dir):
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        no_peering=True,
    )
    assert peering is None
    q = api._eventbus.subscribe()
    try:
        entry = store.set("zuzuz", 20, 100)
        api._name_set_callback(entry)
        set_event = q.get(timeout=2.0)
        assert set_event["type"] == "name_set"
        assert set_event["name"] == "zuzuz"

        api._name_clear_callback("zuzuz")
        clear_event = q.get(timeout=2.0)
        assert clear_event == {"type": "name_clear", "host": clear_event["host"], "name": "zuzuz"}
    finally:
        store.close()


# ---------------------------------------------------------------------------
# closures: bus AND peering, both, when peering is on
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_event_callback_reaches_both_bus_and_peering_pub_when_peering_on(
    tmp_path, socket_dir
):
    uid = _uid("bothevt1")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        peer_pub_port=17902,
        peer_snapshot_port=17903,
        zeroconf=_FakeZeroconfNamespace(),
    )
    assert peering is not None
    q = api._eventbus.subscribe()

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)

    try:
        peering.start()
        sub.connect("tcp://127.0.0.1:17902")
        time.sleep(0.2)  # let the SUB subscription propagate

        daemon.run_once()  # attaches uid -> fires the event hook

        bus_event = q.get(timeout=2.0)
        assert bus_event["type"] == "attach"
        assert bus_event["uid"] == uid

        pub_message = sub.recv()
        assert b"attach" in pub_message
        assert uid.encode() in pub_message
    finally:
        sub.close(linger=0)
        ctx.term()
        peering.stop()
        store.close()


@pytest.mark.requires_af_unix
def test_lock_display_callback_reaches_both_bus_and_peering_pub_when_peering_on(
    tmp_path, socket_dir
):
    uid = _uid("bothevt2")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        peer_pub_port=17912,
        peer_snapshot_port=17913,
        zeroconf=_FakeZeroconfNamespace(),
    )
    q = api._eventbus.subscribe()

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 3000)

    try:
        peering.start()
        sub.connect("tcp://127.0.0.1:17912")
        time.sleep(0.2)

        daemon.locks.acquire(uid, KIND_SERIAL, HolderRef(origin="local", ref="1", pid=1))

        bus_event = q.get(timeout=2.0)
        assert bus_event["type"] == "lock_state"
        assert bus_event["uid"] == uid

        pub_message = sub.recv()
        assert b"lock_state" in pub_message
        assert uid.encode() in pub_message
    finally:
        sub.close(linger=0)
        ctx.term()
        peering.stop()
        store.close()


# ---------------------------------------------------------------------------
# end-to-end: a real `watch` client on the local socket, --no-peering,
# observes attach/lock_state/name_set/name_clear with no ZeroMQ at all
# (SUC-001's own scenario), and never sees peer_up/peer_down (there is no
# PeerDiscovery to produce them).
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_watch_client_on_no_peering_instance_sees_daemon_and_lock_and_name_events(
    tmp_path, socket_dir
):
    uid = _uid("watche2e")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)], []])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        chip_identity_session_factory=unavailable_chip_identity_session_factory,
        no_peering=True,
    )
    assert peering is None
    api.start()
    watcher = _Client(api.socket_path)
    try:
        assert watcher.request({"op": "watch"}) == {"ok": True}

        daemon.run_once()  # attach (+ this cycle's own probe attempt, which
        # fires a second "identity" event once it concludes -- the probe
        # itself fails in this test, since /dev/ttyACM0 isn't a real port
        # and no pyocd debug probe is attached, but daemon.py still
        # records and announces that outcome)
        attach_event = watcher.recv()
        assert attach_event["type"] == "attach"
        assert attach_event["uid"] == uid
        identity_event = watcher.recv()
        assert identity_event["type"] == "identity"
        assert identity_event["uid"] == uid

        daemon.locks.acquire(uid, KIND_SERIAL, HolderRef(origin="local", ref="1", pid=1))
        lock_event = watcher.recv()
        assert lock_event["type"] == "lock_state"
        assert lock_event["uid"] == uid
        assert lock_event["kind"] == KIND_SERIAL

        daemon.locks.release(uid, HolderRef(origin="local", ref="1", pid=1))
        release_event = watcher.recv()
        assert release_event["type"] == "lock_state"
        assert release_event["kind"] is None

        api._name_set_callback(store.set("zuzuz", 20, 100))
        name_set_event = watcher.recv()
        assert name_set_event["type"] == "name_set"
        assert name_set_event["name"] == "zuzuz"

        api._name_clear_callback("zuzuz")
        name_clear_event = watcher.recv()
        assert name_clear_event["type"] == "name_clear"
        assert name_clear_event["name"] == "zuzuz"

        daemon.run_once()  # detach
        detach_event = watcher.recv()
        assert detach_event["type"] == "detach"
        assert detach_event["uid"] == uid
    finally:
        watcher.close()
        api.stop()
        store.close()
