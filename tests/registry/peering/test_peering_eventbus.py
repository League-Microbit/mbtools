"""Tests for mbtools.registry.peering's ZeroMQ half (sprint 003 ticket
005) -- the PUB/REP event bus, per-peer snapshot+SUB links, and
peer-vanish/reconnect handling that ticket 004's mDNS discovery alone
never used.

Per this ticket's own testing guidance ("no mDNS involved -- construct
[peering instances] with an explicit peer address, reusing this ticket's
own REQ/REP+PUB/SUB code path directly"), every test here uses a *fake*
``zeroconf`` module (so ``PeerDiscovery.start()`` never opens a real mDNS
socket) but the *real* ``zmq`` package on loopback -- unlike zeroconf's
own discovery-only concern, the ZeroMQ half's whole job (snapshot
convergence, live event application, vanish detection) is only
meaningfully proven against real sockets, matching sprint.md's Test
Strategy note that this sprint needs "at least one two-process
integration test" for peering, not just unit tests.

A handful of tests at the top apply snapshot/event dicts directly against
a ``Store`` with no socket at all, proving the pure application logic in
isolation before the slower socket-based tests prove it end to end.
"""

from __future__ import annotations

import time

import pytest

from mbtools.registry import peering as peering_mod
from mbtools.registry.locks import KIND_FLASH
from mbtools.registry.peering import PeerDiscovery
from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
    Store,
)

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db")


def _wait_until(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    """Poll ``predicate`` until it's true or ``timeout`` elapses -- every
    socket-based test below drives real threads, so assertions can't
    fire the instant a call returns; this bounds the wait instead of
    sleeping a fixed guess (sprint.md's Test Strategy convention, same
    spirit as ``daemon``'s own manually-advanced-clock tests, adapted for
    "this really does involve a real background thread").
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Fake zeroconf -- no real mDNS socket opened, mirrors test_peering.py's own
# _FakeZeroconfNamespace (duplicated here, not imported, to keep this test
# module self-contained -- see tests/registry/daemon's own precedent of one
# fixture set per test module).
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


class _FakeServiceBrowser:
    def __init__(self, zc, type_, listener=None):
        self.zc = zc
        self.type_ = type_
        self.listener = listener
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _FakeZeroconfNamespace:
    ServiceInfo = staticmethod(_FakeServiceInfo)
    ServiceBrowser = staticmethod(_FakeServiceBrowser)

    def __init__(self):
        self.instance = _FakeZeroconf()

    def Zeroconf(self):
        return self.instance


def _make_peering(store: Store, *, host: str, pub_port: int, snapshot_port: int) -> PeerDiscovery:
    """A ``PeerDiscovery`` with fake mDNS (no real socket) and the real
    ``zmq`` package (default) on loopback-only ports distinct from this
    project's production defaults, so a stray real ``mbregistry`` on the
    dev machine can never collide with a test run.
    """
    return PeerDiscovery(
        store=store,
        host=host,
        advertise_address="127.0.0.1",
        remote_port=_remote_port(pub_port),
        pub_port=pub_port,
        snapshot_port=snapshot_port,
        zeroconf=_FakeZeroconfNamespace(),
    )


def _remote_port(pub_port: int) -> int:
    """A remote-API port distinct from ``pub_port``/``snapshot_port`` --
    these tests never actually open ``registry.remote_api`` (ticket 006),
    so this only needs to be a stable, distinct value to put in a
    ``store.peer.endpoint`` string.
    """
    return pub_port + 1000


# ---------------------------------------------------------------------------
# pure application logic -- no socket at all
# ---------------------------------------------------------------------------


def test_apply_event_attach_creates_remote_row(store):
    peering_mod._apply_event(
        store, "alpha", {"type": "attach", "uid": UID, "port": "/dev/ttyACM0", "vid_pid": "0d28:0204"}
    )

    record = store.get(UID)
    assert record is not None
    assert record.host == "alpha"
    assert record.port == "/dev/ttyACM0"
    assert record.state == STATE_ATTACHED_UNPROBED


def test_apply_event_identity_connected_sets_announcement_fields(store):
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store,
        "alpha",
        {
            "type": "identity",
            "uid": UID,
            "state": STATE_CONNECTED,
            "role": "NEZHA2",
            "common_name": "vevov",
            "device_name": "vevov",
            "serial_payload": "1198504156",
            "raw_announcement": "device NEZHA2 robot vevov 1198504156",
        },
    )

    record = store.get(UID)
    assert record.state == STATE_CONNECTED
    assert record.role == "NEZHA2"
    assert record.device_name == "vevov"
    # the identity event never carried port/vid_pid -- must be untouched.
    assert record.port == "p"


def test_apply_event_identity_no_firmware(store):
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store, "alpha", {"type": "identity", "uid": UID, "state": STATE_CONNECTED_NO_FIRMWARE}
    )

    assert store.get(UID).state == STATE_CONNECTED_NO_FIRMWARE


def test_apply_event_detach_marks_disconnected(store):
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(store, "alpha", {"type": "detach", "uid": UID})

    assert store.get(UID).state == STATE_DISCONNECTED


def test_apply_event_lock_state_sets_display_cache(store):
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store, "alpha", {"type": "lock_state", "uid": UID, "kind": "flash", "display": "pid 4821"}
    )

    record = store.get(UID)
    assert record.remote_lock_kind == "flash"
    assert record.remote_lock_display == "pid 4821"

    peering_mod._apply_event(
        store, "alpha", {"type": "lock_state", "uid": UID, "kind": None, "display": None}
    )
    record = store.get(UID)
    assert record.remote_lock_kind is None
    assert record.remote_lock_display is None


def test_apply_event_for_unknown_uid_is_dropped_not_raised(store):
    # No preceding "attach" -- detach/identity/lock_state for a uid this
    # store has never heard of must be logged and dropped, never raise.
    peering_mod._apply_event(store, "alpha", {"type": "detach", "uid": "no-such-uid"})
    peering_mod._apply_event(
        store, "alpha", {"type": "identity", "uid": "no-such-uid", "state": STATE_CONNECTED}
    )
    peering_mod._apply_event(
        store, "alpha", {"type": "lock_state", "uid": "no-such-uid", "kind": "flash", "display": "x"}
    )
    assert store.get("no-such-uid") is None


def test_apply_snapshot_device_connected(store):
    peering_mod._apply_snapshot_device(
        store,
        "alpha",
        {
            "uid": UID,
            "port": "/dev/ttyACM0",
            "vid_pid": "0d28:0204",
            "state": STATE_CONNECTED,
            "role": "NEZHA2",
            "common_name": "vevov",
            "device_name": "vevov",
            "serial_payload": "1198504156",
            "raw_announcement": "device NEZHA2 robot vevov 1198504156",
        },
    )

    record = store.get(UID)
    assert record.host == "alpha"
    assert record.state == STATE_CONNECTED
    assert record.device_name == "vevov"


def test_apply_snapshot_device_disconnected(store):
    peering_mod._apply_snapshot_device(
        store,
        "alpha",
        {"uid": UID, "port": "p", "vid_pid": "v", "state": STATE_DISCONNECTED},
    )

    assert store.get(UID).state == STATE_DISCONNECTED


def test_snapshot_payload_round_trips_through_apply(store):
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")
    payload = peering_mod._snapshot_payload(store)
    assert len(payload) == 1
    assert payload[0]["uid"] == UID
    assert payload[0]["state"] == STATE_ATTACHED_UNPROBED

    other_store = Store(store.db_path.parent / "other.db")
    peering_mod._apply_snapshot_device(other_store, "alpha", payload[0])
    assert other_store.get(UID).state == STATE_ATTACHED_UNPROBED
    assert other_store.get(UID).host == "alpha"


# ---------------------------------------------------------------------------
# real loopback sockets -- snapshot-then-stream convergence
# ---------------------------------------------------------------------------


def test_snapshot_then_stream_convergence_end_to_end(store, tmp_path):
    """This ticket's own "at least one two-process integration test":
    two real ``PeerDiscovery`` instances on loopback with distinct ports,
    connected via ``connect_peer()`` directly (no mDNS) -- proves
    snapshot bootstrap, then a live attach + identity + lock_state event,
    all converge into the second instance's own ``Store``.
    """
    store_b = Store(tmp_path / "beta.db")

    # alpha already has one fully-probed device before beta ever connects.
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")
    from mbtools.registry.identity import ProbeResult

    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2",
            common_name="vevov",
            device_name="vevov",
            serial="1198504156",
            raw="device NEZHA2 robot vevov 1198504156",
        ),
    )

    peering_a = _make_peering(store, host="alpha", pub_port=17542, snapshot_port=17543)
    peering_b = _make_peering(store_b, host="beta", pub_port=17552, snapshot_port=17553)

    try:
        peering_a.start()
        peering_b.start()

        # In the real mDNS-driven path, ticket 004's ``_BrowseListener``
        # calls ``store.record_peer_seen`` before ``on_peer_ready`` (this
        # ticket's ``connect_peer``) ever fires -- see this ticket's own
        # acceptance criterion, "sent once right after ticket 004 records
        # a newly-discovered peer". Reproduced explicitly here since this
        # test bypasses mDNS entirely.
        store_b.record_peer_seen("alpha", f"127.0.0.1:{_remote_port(17542)}")
        peering_b.connect_peer("alpha", "127.0.0.1", 17542, 17543)

        # 1. snapshot bootstrap
        assert _wait_until(lambda: store_b.get(UID) is not None)
        record = store_b.get(UID)
        assert record.host == "alpha"
        assert record.state == STATE_CONNECTED
        assert record.device_name == "vevov"
        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is True)

        # 2. live attach event for a second, not-yet-probed device
        second = store.upsert_attached(UID2, "/dev/ttyACM1", "0d28:0204")
        peering_a.publish_daemon_event("attach", second)

        assert _wait_until(lambda: store_b.get(UID2) is not None)
        assert store_b.get(UID2).host == "alpha"
        assert store_b.get(UID2).state == STATE_ATTACHED_UNPROBED

        # 3. live identity event for that same device
        probed = store.apply_probe_result(
            UID2,
            ProbeResult(role="NEZHA2", common_name="loki", device_name="loki", serial="x", raw="raw"),
        )
        peering_a.publish_daemon_event("identity", probed)

        assert _wait_until(lambda: store_b.get(UID2).state == STATE_CONNECTED)
        assert store_b.get(UID2).device_name == "loki"

        # 4. live lock_state event
        peering_a.publish_lock_event(UID2, KIND_FLASH, "pid 4821")

        assert _wait_until(lambda: store_b.get(UID2).remote_lock_kind == KIND_FLASH)
        assert store_b.get(UID2).remote_lock_display == "pid 4821"

        # 5. live detach event
        detached = store.mark_disconnected(UID2)
        peering_a.publish_daemon_event("detach", detached)

        assert _wait_until(lambda: store_b.get(UID2).state == STATE_DISCONNECTED)
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_rep_socket_never_blocks_pub_socket(store, tmp_path):
    """Explicit acceptance check: a slow/stalled snapshot request must
    never delay this host's own event publishing -- separate sockets, so
    true by construction, proven here by making the REP handler's own
    store read slow and confirming a concurrently published PUB event
    still arrives promptly.
    """
    real_snapshot_fn = store.snapshot_local_devices

    def slow_snapshot():
        time.sleep(1.0)
        return real_snapshot_fn()

    store.snapshot_local_devices = slow_snapshot

    peering_a = _make_peering(store, host="alpha", pub_port=17562, snapshot_port=17563)

    import zmq

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    req = ctx.socket(zmq.REQ)
    req.setsockopt(zmq.RCVTIMEO, 5000)

    try:
        peering_a.start()
        sub.connect("tcp://127.0.0.1:17562")
        req.connect("tcp://127.0.0.1:17563")
        time.sleep(0.2)  # let the SUB subscription propagate

        req.send(b"snapshot")  # this REP handler is now sleeping for 1s

        started = time.monotonic()
        peering_a.publish_event("attach", {"uid": UID})
        sub.setsockopt(zmq.RCVTIMEO, 2000)
        message = sub.recv()
        elapsed = time.monotonic() - started

        assert b"attach" in message
        assert elapsed < 0.5  # well before the REP handler's 1s sleep finishes

        reply = req.recv()  # drain the still-pending REP reply
        assert reply is not None
    finally:
        sub.close(linger=0)
        req.close(linger=0)
        ctx.term()
        peering_a.stop()
        store.close()


# ---------------------------------------------------------------------------
# peer-vanish and reconnect (Decision 5)
# ---------------------------------------------------------------------------


def test_peer_vanish_marks_unreachable_and_reconnect_marks_reachable_again(store, tmp_path):
    store_b = Store(tmp_path / "beta.db")
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_a = _make_peering(store, host="alpha", pub_port=17572, snapshot_port=17573)
    peering_b = _make_peering(store_b, host="beta", pub_port=17582, snapshot_port=17583)

    peering_a2 = None
    try:
        peering_a.start()
        peering_b.start()
        store_b.record_peer_seen("alpha", f"127.0.0.1:{_remote_port(17572)}")
        peering_b.connect_peer("alpha", "127.0.0.1", 17572, 17573)

        assert _wait_until(lambda: store_b.get_peer("alpha") is not None)
        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is True)

        # Kill alpha's side of the link entirely -- beta's SUB socket must
        # observe the drop via zmq.EVENT_DISCONNECTED, not a guessed
        # timeout, and mark the peer unreachable within a bounded wait.
        peering_a.stop()

        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is False, timeout=10.0)

        # Reconnect: a fresh PeerDiscovery for "alpha" (distinct ports,
        # modeling a restarted process re-advertising with whatever ports
        # it actually bound -- ticket 005's own "new mDNS advertisement or
        # --peer retry" reconnect trigger), re-running the snapshot
        # exchange via connect_peer() the same way a real reconnect would.
        peering_a2 = _make_peering(store, host="alpha", pub_port=17592, snapshot_port=17593)
        peering_a2.start()
        peering_b.connect_peer("alpha", "127.0.0.1", 17592, 17593)

        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is True, timeout=10.0)
    finally:
        peering_b.stop()
        if peering_a2 is not None:
            peering_a2.stop()
        store.close()
        store_b.close()
