"""Tests for sprint 008 ticket 001's additions to mbtools.registry.peering:

- the pure wire-payload builder functions (``daemon_event_payload``/
  ``lock_event_payload``/``name_set_payload``/``name_clear_payload``)
  extracted from ``PeerDiscovery.publish_daemon_event``/``publish_lock_event``/
  ``publish_name_set``/``publish_name_clear`` so ``registry.cli``'s own
  event-bus fan-out closures can build the identical dict without a live
  ``PeerDiscovery``;
- ``EVENT_PEER_UP``/``EVENT_PEER_DOWN``, published by
  ``PeerDiscovery._on_peer_reachable``/``_on_peer_unreachable`` to an
  injected ``EventBus`` only, never onward over this host's own PUB
  socket to a third host.

The peer_up/peer_down tests reuse this package's existing "two real
PeerDiscovery instances over loopback, fake zeroconf" pattern
(tests/registry/peering/test_peering_eventbus.py) since reachability
detection is only meaningfully proven against a real snapshot/PUB link,
per that module's own docstring.
"""

from __future__ import annotations

import time

import pytest
import zmq

from mbtools.registry.eventbus import EventBus
from mbtools.registry.identity import ProbeResult
from mbtools.registry.peering import (
    EVENT_ATTACH,
    EVENT_DETACH,
    EVENT_IDENTITY,
    EVENT_PEER_DOWN,
    EVENT_PEER_UP,
    PeerDiscovery,
    daemon_event_payload,
    lock_event_payload,
    name_clear_payload,
    name_set_payload,
)
from mbtools.registry.store import STATE_CONNECTED, STATE_DISCONNECTED, Store

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
VID_PID = "0d28:0204"


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# fake zeroconf -- no real mDNS socket opened, same shape as
# test_peering_eventbus.py's own fake (duplicated here per this project's
# "one fixture set per test module" convention).
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


def _make_peering(store: Store, *, host: str, pub_port: int, snapshot_port: int, eventbus=None):
    return PeerDiscovery(
        store=store,
        host=host,
        advertise_address="127.0.0.1",
        remote_port=pub_port + 1000,
        pub_port=pub_port,
        snapshot_port=snapshot_port,
        zeroconf=_FakeZeroconfNamespace(),
        eventbus=eventbus,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


# ---------------------------------------------------------------------------
# pure payload builders -- no socket, no store
# ---------------------------------------------------------------------------


def _attached_record(store: Store, uid: str = UID):
    store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)
    return store.get(uid)


def _identified_record(store: Store, uid: str = UID):
    store.upsert_attached(uid, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        uid,
        ProbeResult(
            role="robot", common_name="Robot", device_name="alpha", serial="123", raw="DEVICE:alpha"
        ),
    )
    return store.get(uid)


def test_daemon_event_payload_attach(store):
    record = _attached_record(store)
    assert daemon_event_payload(EVENT_ATTACH, record) == {
        "uid": record.uid,
        "port": record.port,
        "vid_pid": record.vid_pid,
    }


def test_daemon_event_payload_detach(store):
    record = _attached_record(store)
    assert daemon_event_payload(EVENT_DETACH, record) == {"uid": record.uid}


def test_daemon_event_payload_identity(store):
    record = _identified_record(store)
    payload = daemon_event_payload(EVENT_IDENTITY, record)
    assert payload == {
        "uid": record.uid,
        "state": record.state,
        "role": record.role,
        "common_name": record.common_name,
        "device_name": record.device_name,
        "serial_payload": record.serial_payload,
        "raw_announcement": record.raw_announcement,
    }
    assert record.state == STATE_CONNECTED


def test_daemon_event_payload_suppresses_attach_for_disconnected_record(store):
    record = _attached_record(store)
    store.mark_disconnected(record.uid)
    disconnected = store.get(record.uid)
    assert disconnected.state == STATE_DISCONNECTED

    assert daemon_event_payload(EVENT_ATTACH, disconnected) is None
    assert daemon_event_payload(EVENT_IDENTITY, disconnected) is None
    # A detach for the same record is never suppressed.
    assert daemon_event_payload(EVENT_DETACH, disconnected) == {"uid": disconnected.uid}


def test_lock_event_payload():
    assert lock_event_payload("uid-1", "serial", "locked by pid 42") == {
        "uid": "uid-1",
        "kind": "serial",
        "display": "locked by pid 42",
    }


def test_lock_event_payload_release_shape():
    assert lock_event_payload("uid-1", None, None) == {
        "uid": "uid-1",
        "kind": None,
        "display": None,
    }


def test_name_set_payload(store):
    entry = store.set("zuzuz", 20, 100)
    assert name_set_payload(entry) == {
        "name": entry.name,
        "channel": entry.channel,
        "group": entry.group,
        "source": entry.source,
        "updated": entry.updated,
    }


def test_name_clear_payload():
    assert name_clear_payload("zuzuz") == {"name": "zuzuz"}


# ---------------------------------------------------------------------------
# peer_up / peer_down: published to an injected EventBus only, never
# onward over this host's own PUB socket to a third host.
# ---------------------------------------------------------------------------


def test_on_peer_reachable_publishes_peer_up_to_the_event_bus(store, tmp_path):
    store_b = Store(tmp_path / "beta.db")
    bus_b = EventBus()
    q = bus_b.subscribe()

    peering_a = _make_peering(store, host="alpha", pub_port=17802, snapshot_port=17803)
    peering_b = _make_peering(
        store_b, host="beta", pub_port=17812, snapshot_port=17813, eventbus=bus_b
    )

    try:
        peering_a.start()
        peering_b.start()

        peering_b.connect_peer(
            "alpha", "127.0.0.1", 17802, 17803, remote_port=18802
        )

        assert _wait_until(lambda: store_b.get_peer("alpha") is not None
                            and store_b.get_peer("alpha").reachable is True)

        event = q.get(timeout=5.0)
        assert event == {"type": EVENT_PEER_UP, "host": "alpha"}
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_on_peer_unreachable_publishes_peer_down_to_the_event_bus(store, tmp_path):
    store_b = Store(tmp_path / "beta2.db")
    bus_b = EventBus()
    # Subscribed before any connection is made -- avoids a race between
    # "the store field flips reachable=True" (mark_peer_reachable) and
    # "the peer_up publish actually runs" (the very next line in
    # _on_peer_reachable): both happen on the peer-link's own background
    # thread, so polling the store from this thread and only *then*
    # subscribing could miss (or spuriously catch) the peer_up event
    # depending on exactly where the two threads interleave.
    q = bus_b.subscribe()

    peering_a = _make_peering(store, host="alpha", pub_port=17822, snapshot_port=17823)
    peering_b = _make_peering(
        store_b, host="beta", pub_port=17832, snapshot_port=17833, eventbus=bus_b
    )

    try:
        peering_a.start()
        peering_b.start()
        peering_b.connect_peer(
            "alpha", "127.0.0.1", 17822, 17823, remote_port=18822
        )

        up_event = q.get(timeout=5.0)
        assert up_event == {"type": EVENT_PEER_UP, "host": "alpha"}

        peering_a.stop()

        down_event = q.get(timeout=10.0)
        assert down_event == {"type": EVENT_PEER_DOWN, "host": "alpha"}
    finally:
        peering_b.stop()
        store.close()
        store_b.close()


def test_peer_reachability_never_goes_out_over_this_hosts_own_pub_socket(store, tmp_path):
    """peer_up/peer_down are a purely local notification (sprint.md's own
    words) -- a third host subscribed to beta's PUB socket must never see
    either event type, even though beta's own EventBus does."""
    store_b = Store(tmp_path / "beta3.db")
    bus_b = EventBus()

    peering_a = _make_peering(store, host="alpha", pub_port=17842, snapshot_port=17843)
    peering_b = _make_peering(
        store_b, host="beta", pub_port=17852, snapshot_port=17853, eventbus=bus_b
    )

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVTIMEO, 500)

    try:
        peering_a.start()
        peering_b.start()
        sub.connect("tcp://127.0.0.1:17852")
        time.sleep(0.2)  # let the SUB subscription propagate

        peering_b.connect_peer("alpha", "127.0.0.1", 17842, 17843, remote_port=18842)
        assert _wait_until(lambda: store_b.get_peer("alpha") is not None
                            and store_b.get_peer("alpha").reachable is True)

        # Something real did cross the wire on *beta's own* PUB socket
        # (the one this test's SUB is connected to), proving the SUB
        # link itself works -- so a later empty poll for peer_up/down
        # actually means "never sent", not "SUB never connected".
        store_b.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
        peering_b.publish_daemon_event("attach", store_b.get(UID))
        message = sub.recv()
        assert b"attach" in message

        # No peer_up (or peer_down) bytes ever arrive on the wire.
        with pytest.raises(zmq.Again):
            while True:
                extra = sub.recv()
                assert b"peer_up" not in extra
                assert b"peer_down" not in extra
    finally:
        sub.close(linger=0)
        ctx.term()
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_eventbus_defaults_to_none_and_reachability_callbacks_still_work(store):
    """Every pre-ticket-008-001 caller/test omits ``eventbus`` -- the
    reachable/unreachable store bookkeeping must be completely
    unaffected."""
    peering = _make_peering(store, host="torture", pub_port=17862, snapshot_port=17863)
    store.record_peer_seen("gamma", "127.0.0.1:9000")

    peering._on_peer_reachable("gamma")  # must not raise with no eventbus
    assert store.get_peer("gamma").reachable is True

    peering._on_peer_unreachable("gamma")  # must not raise with no eventbus
    assert store.get_peer("gamma").reachable is False
