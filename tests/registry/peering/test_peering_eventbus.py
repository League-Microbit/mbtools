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
    SOURCE_DERIVED,
    SOURCE_REGISTRY,
    STATE_ATTACHED_NO_ANNOUNCE,
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
    Store,
)

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"

# Two well-formed names (CVCVC over zvgpt/uoiea, see
# mbtools.relay.naming/tests/registry/store/test_store_name_registry.py's
# own picks) whose derived (channel, group) pairs are distinct from each
# other -- used by the name-registry replication tests below.
NAME_A = "zuzuz"
NAME_B = "tatat"


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
    """Sprint 007, ticket 003: a peer's genuinely-known-blank board
    (wire value ``STATE_CONNECTED_NO_FIRMWARE``) is applied via
    ``store.apply_known_blank`` and lands on the same
    ``STATE_CONNECTED_NO_FIRMWARE`` state here too -- not downgraded to
    ``STATE_ATTACHED_NO_ANNOUNCE`` the way a naive reuse of
    ``apply_remote_probe(uid, None)`` (-> ``apply_probe_result``, which
    ticket 001 narrowed to mean didn't-announce) would have done."""
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store, "alpha", {"type": "identity", "uid": UID, "state": STATE_CONNECTED_NO_FIRMWARE}
    )

    assert store.get(UID).state == STATE_CONNECTED_NO_FIRMWARE


def test_apply_event_identity_attached_no_announce(store):
    """Sprint 007, ticket 003: the didn't-announce wire value is now
    recognized at all -- before this ticket it matched neither branch and
    was silently dropped (the uid's state never advanced)."""
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store, "alpha", {"type": "identity", "uid": UID, "state": STATE_ATTACHED_NO_ANNOUNCE}
    )

    assert store.get(UID).state == STATE_ATTACHED_NO_ANNOUNCE


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


def test_apply_event_lock_state_ignores_label_and_since_keys(store):
    """Sprint 008, ticket 002: even when an incoming ``lock_state`` event
    carries ``label``/``since`` keys (sent alongside a baked-in
    ``display``, per ``lock_event_payload``), the receiving side only
    ever reads ``kind``/``display`` off it -- no new SQLite column or
    queryable field is added by this ticket (Design Rationale Decision
    2's "no new PUB field" is about this replicated shape)."""
    peering_mod._apply_event(store, "alpha", {"type": "attach", "uid": UID, "port": "p", "vid_pid": "v"})

    peering_mod._apply_event(
        store,
        "alpha",
        {
            "type": "lock_state",
            "uid": UID,
            "kind": "flash",
            "display": "pid 4821 (alice-laptop, 3m)",
            "label": "alice-laptop",
            "since": 1000.0,
        },
    )

    record = store.get(UID)
    assert record.remote_lock_kind == "flash"
    assert record.remote_lock_display == "pid 4821 (alice-laptop, 3m)"
    assert not hasattr(record, "remote_lock_label")
    assert not hasattr(record, "remote_lock_since")


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


def test_apply_snapshot_device_known_blank(store):
    """Sprint 007, ticket 003: a snapshot's known-blank state is applied
    via apply_known_blank, not downgraded to didn't-announce."""
    peering_mod._apply_snapshot_device(
        store,
        "alpha",
        {"uid": UID, "port": "p", "vid_pid": "v", "state": STATE_CONNECTED_NO_FIRMWARE},
    )

    assert store.get(UID).state == STATE_CONNECTED_NO_FIRMWARE


def test_apply_snapshot_device_attached_no_announce(store):
    """Sprint 007, ticket 003: the didn't-announce wire value is now
    recognized on the snapshot path too."""
    peering_mod._apply_snapshot_device(
        store,
        "alpha",
        {"uid": UID, "port": "p", "vid_pid": "v", "state": STATE_ATTACHED_NO_ANNOUNCE},
    )

    assert store.get(UID).state == STATE_ATTACHED_NO_ANNOUNCE


# ---------------------------------------------------------------------------
# Local ownership wins over stale peer sync (sprint 005 ticket 011) --
# proving the rejection reaches through both application paths peering
# routes into Store._upsert_device, and that publishing never re-asserts
# a disconnected local row's ownership.
# ---------------------------------------------------------------------------


def test_apply_event_attach_does_not_overwrite_local_connected_row(store):
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_mod._apply_event(
        store, "hodr", {"type": "attach", "uid": UID, "port": "/dev/ttyACM9", "vid_pid": "x"}
    )

    record = store.get(UID)
    assert record.host is None
    assert record.port == "/dev/ttyACM0"


def test_apply_snapshot_device_does_not_overwrite_local_connected_row(store):
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_mod._apply_snapshot_device(
        store,
        "hodr",
        {
            "uid": UID,
            "port": "/dev/ttyACM9",
            "vid_pid": "x",
            "state": STATE_DISCONNECTED,
        },
    )

    # hodr's own stale snapshot row says "disconnected" -- exactly the
    # production shape (hodr's own copy of a board it no longer has is
    # disconnected on hodr's side too) -- but torture's row is locally
    # owned and never went through the rejected upsert_remote_attached
    # call, so the trailing mark_remote_detached in _apply_snapshot_device
    # never even runs against it via the remote path; the row it *does*
    # find by uid is still torture's own local, connected row and must be
    # untouched.
    record = store.get(UID)
    assert record.host is None
    assert record.port == "/dev/ttyACM0"
    assert record.state == STATE_ATTACHED_UNPROBED


def test_apply_event_attach_takes_over_local_disconnected_row(store):
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")
    store.mark_disconnected(UID)

    peering_mod._apply_event(
        store, "hodr", {"type": "attach", "uid": UID, "port": "/dev/ttyACM9", "vid_pid": "x"}
    )

    record = store.get(UID)
    assert record.host == "hodr"
    assert record.port == "/dev/ttyACM9"


def test_apply_event_detach_does_not_disconnect_local_connected_row(store):
    # The downstream half of the same bug: even once the "attach" claim
    # is rejected, a stale "detach" for the same uid must not be allowed
    # to flip our own locally-owned row to disconnected either -- that
    # would still empty out console_compat.relay_pool for a board that
    # never actually left.
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_mod._apply_event(store, "hodr", {"type": "detach", "uid": UID})

    record = store.get(UID)
    assert record.host is None
    assert record.state == STATE_ATTACHED_UNPROBED


def test_apply_event_identity_does_not_overwrite_local_connected_row(store):
    from mbtools.registry.identity import ProbeResult

    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")
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

    peering_mod._apply_event(
        store,
        "hodr",
        {
            "type": "identity",
            "uid": UID,
            "state": STATE_CONNECTED,
            "role": "NEZHA2",
            "common_name": "impostor",
            "device_name": "impostor",
            "serial_payload": "0",
            "raw_announcement": "device NEZHA2 impostor impostor 0",
        },
    )

    record = store.get(UID)
    assert record.host is None
    assert record.device_name == "vevov"


def test_apply_event_detach_applies_when_attributed_to_sender(store):
    # Positive case for the same guard: a legitimate detach from the peer
    # that actually owns the row still works.
    peering_mod._apply_event(
        store, "hodr", {"type": "attach", "uid": UID, "port": "/dev/ttyACM0", "vid_pid": "x"}
    )

    peering_mod._apply_event(store, "hodr", {"type": "detach", "uid": UID})

    record = store.get(UID)
    assert record.host == "hodr"
    assert record.state == STATE_DISCONNECTED


def test_publish_daemon_event_suppresses_attach_for_disconnected_record(store):
    peering = _make_peering(store, host="torture", pub_port=17592, snapshot_port=17593)
    published: list[tuple[str, dict]] = []
    peering.publish_event = lambda event_type, payload: published.append((event_type, payload))

    record = store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")
    store.mark_disconnected(UID)
    disconnected = store.get(UID)
    assert disconnected.state == STATE_DISCONNECTED

    peering.publish_daemon_event(peering_mod.EVENT_ATTACH, disconnected)
    peering.publish_daemon_event(peering_mod.EVENT_IDENTITY, disconnected)

    assert published == []

    # A genuine detach publish for the same record is never suppressed.
    peering.publish_daemon_event(peering_mod.EVENT_DETACH, disconnected)
    assert len(published) == 1
    assert published[0][0] == peering_mod.EVENT_DETACH

    # And a live (non-disconnected) record still publishes normally.
    peering.publish_daemon_event(peering_mod.EVENT_ATTACH, record)
    assert len(published) == 2
    assert published[1][0] == peering_mod.EVENT_ATTACH


def test_publish_lock_event_bakes_label_and_since_into_display(store):
    """Sprint 008, ticket 002, Design Rationale Decision 2: a peer's
    replicated ``remote_lock_display`` string carries label/since as
    baked-in text, not a new SQLite column or PUB field -- proven here at
    the ``publish_lock_event``/``lock_event_payload`` boundary, before
    ``test_apply_event_lock_state_sets_display_cache`` proves the
    receiving side never reads the ``label``/``since`` keys back out."""
    peering = _make_peering(store, host="torture", pub_port=17594, snapshot_port=17595)
    published: list[tuple[str, dict]] = []
    peering.publish_event = lambda event_type, payload: published.append((event_type, payload))

    peering.publish_lock_event(UID, "flash", "pid 4821", "alice-laptop", time.time())

    assert len(published) == 1
    event_type, payload = published[0]
    assert event_type == peering_mod.EVENT_LOCK_STATE
    assert payload["display"].startswith("pid 4821 (alice-laptop, ")
    assert payload["label"] == "alice-laptop"
    assert payload["since"] is not None

    # No label/since -- display is passed through unchanged, same as
    # before this ticket.
    published.clear()
    peering.publish_lock_event(UID, "flash", "pid 4821")
    assert published[0][1]["display"] == "pid 4821"

    # A release (kind/display both None) never grows a suffix.
    published.clear()
    peering.publish_lock_event(UID, None, None)
    assert published[0][1]["display"] is None


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
# ticket 002 (sprint 004): name_registry replication -- pure application
# logic, no socket at all (mirrors the device-row tests above).
# ---------------------------------------------------------------------------


def test_apply_event_name_set_creates_entry(store):
    peering_mod._apply_event(
        store, "alpha", {"type": "name_set", "name": NAME_A, "channel": 20, "group": 100}
    )

    entry = store.get_name(NAME_A)
    assert entry is not None
    assert entry.channel == 20
    assert entry.group == 100
    # Applying always lands as SOURCE_REGISTRY on the receiving side,
    # regardless of the origin row's own source -- see peering.py's
    # module docstring "Name registry replication" note.
    assert entry.source == SOURCE_REGISTRY


def test_apply_event_name_set_overwrites_existing_entry(store):
    store.set(NAME_A, 20, 100)

    peering_mod._apply_event(
        store, "alpha", {"type": "name_set", "name": NAME_A, "channel": 30, "group": 200}
    )

    entry = store.get_name(NAME_A)
    assert (entry.channel, entry.group) == (30, 200)


def test_apply_event_name_clear_removes_entry(store):
    store.set(NAME_A, 20, 100)

    peering_mod._apply_event(store, "alpha", {"type": "name_clear", "name": NAME_A})

    assert store.get_name(NAME_A) is None


def test_apply_event_name_clear_of_absent_entry_is_a_no_op(store):
    peering_mod._apply_event(store, "alpha", {"type": "name_clear", "name": NAME_A})

    assert store.get_name(NAME_A) is None


def test_apply_event_name_set_malformed_is_dropped_not_raised(store):
    # Missing "channel"/"group" -- logged and dropped, never raised, same
    # "a single malformed event must not crash the receive loop" policy
    # _apply_event already applies to device events (see
    # test_apply_event_for_unknown_uid_is_dropped_not_raised above).
    peering_mod._apply_event(store, "alpha", {"type": "name_set", "name": NAME_A})
    peering_mod._apply_event(store, "alpha", {"type": "name_clear"})

    assert store.get_name(NAME_A) is None


def test_apply_snapshot_name_applies_entry(store):
    peering_mod._apply_snapshot_name(
        store, {"name": NAME_A, "channel": 20, "group": 100, "source": SOURCE_DERIVED, "updated": 1.0}
    )

    entry = store.get_name(NAME_A)
    assert entry is not None
    assert (entry.channel, entry.group) == (20, 100)


def test_snapshot_name_payload_round_trips_through_apply(store):
    store.set(NAME_A, 20, 100)
    store.resolve(NAME_B)  # source=derived

    payload = peering_mod._snapshot_name_payload(store)
    assert {row["name"] for row in payload} == {NAME_A, NAME_B}

    other_store = Store(store.db_path.parent / "other_names.db")
    for row in payload:
        peering_mod._apply_snapshot_name(other_store, row)

    assert (other_store.get_name(NAME_A).channel, other_store.get_name(NAME_A).group) == (20, 100)
    original_b = store.get_name(NAME_B)
    replicated_b = other_store.get_name(NAME_B)
    assert (replicated_b.channel, replicated_b.group) == (original_b.channel, original_b.group)


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


# ---------------------------------------------------------------------------
# ticket 002 (sprint 004): name_registry replication -- real loopback
# sockets, mirroring this module's existing device-row integration tests
# above (snapshot-then-stream convergence, peer vanish).
# ---------------------------------------------------------------------------


def test_name_registry_set_and_clear_propagate_over_event_bus(store, tmp_path):
    """Acceptance: a ``name_registry`` ``set`` on host A publishes onto
    the PUB bus and is visible in host B's ``Store`` within one
    event-bus tick, with no polling on B's side -- and a ``clear``
    likewise propagates.
    """
    store_b = Store(tmp_path / "beta_names_setclear.db")

    peering_a = _make_peering(store, host="alpha", pub_port=17682, snapshot_port=17683)
    peering_b = _make_peering(store_b, host="beta", pub_port=17692, snapshot_port=17693)

    try:
        peering_a.start()
        peering_b.start()
        store_b.record_peer_seen("alpha", f"127.0.0.1:{_remote_port(17682)}")
        peering_b.connect_peer("alpha", "127.0.0.1", 17682, 17683)
        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is True)

        entry = store.set(NAME_A, 20, 100)
        peering_a.publish_name_set(entry)

        assert _wait_until(lambda: store_b.get_name(NAME_A) is not None)
        replicated = store_b.get_name(NAME_A)
        assert (replicated.channel, replicated.group) == (20, 100)

        store.clear(NAME_A)
        peering_a.publish_name_clear(NAME_A)

        assert _wait_until(lambda: store_b.get_name(NAME_A) is None)
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_name_registry_snapshot_catches_up_late_joining_peer(store, tmp_path):
    """Acceptance: a peer joining after entries already exist receives the
    current ``name_registry`` rows via the existing snapshot exchange --
    no separate name-registry-specific snapshot call.
    """
    store.set(NAME_A, 20, 100)
    store.resolve(NAME_B)

    store_b = Store(tmp_path / "beta_names_snapshot.db")
    peering_a = _make_peering(store, host="alpha", pub_port=17702, snapshot_port=17703)
    peering_b = _make_peering(store_b, host="beta", pub_port=17712, snapshot_port=17713)

    try:
        peering_a.start()
        peering_b.start()
        store_b.record_peer_seen("alpha", f"127.0.0.1:{_remote_port(17702)}")
        peering_b.connect_peer("alpha", "127.0.0.1", 17702, 17703)

        assert _wait_until(lambda: store_b.get_name(NAME_A) is not None)
        assert _wait_until(lambda: store_b.get_name(NAME_B) is not None)
        assert (store_b.get_name(NAME_A).channel, store_b.get_name(NAME_A).group) == (20, 100)
        original_b = store.get_name(NAME_B)
        replicated_b = store_b.get_name(NAME_B)
        assert (replicated_b.channel, replicated_b.group) == (original_b.channel, original_b.group)
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_name_registry_survives_peer_link_drop(store, tmp_path):
    """Acceptance: a peer-link drop degrades name-registry replication the
    same way it already degrades device-state replication (sets
    ``peer.reachable = false``; existing rows are not deleted or
    reverted).
    """
    store.set(NAME_A, 20, 100)
    store_b = Store(tmp_path / "beta_names_vanish.db")

    peering_a = _make_peering(store, host="alpha", pub_port=17722, snapshot_port=17723)
    peering_b = _make_peering(store_b, host="beta", pub_port=17732, snapshot_port=17733)
    try:
        peering_a.start()
        peering_b.start()
        store_b.record_peer_seen("alpha", f"127.0.0.1:{_remote_port(17722)}")
        peering_b.connect_peer("alpha", "127.0.0.1", 17722, 17723)

        assert _wait_until(lambda: store_b.get_name(NAME_A) is not None)

        peering_a.stop()
        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is False, timeout=10.0)

        # The replicated name row must survive the vanish untouched --
        # only peer.reachable flips, per Decision 5.
        replicated = store_b.get_name(NAME_A)
        assert replicated is not None
        assert (replicated.channel, replicated.group) == (20, 100)
    finally:
        peering_b.stop()
        store.close()
        store_b.close()


def test_two_peers_independently_deriving_same_unseen_name_converge(store, tmp_path):
    """Acceptance: two peers independently deriving the same unseen name
    (e.g. two simultaneous ``resolve()`` calls before either has heard
    from the other) converge on the same value with no conflict/error
    surfaced. No live peering link is even needed for this to hold --
    ``naming.name_to_radio`` is a pure deterministic function of
    ``name``, per sprint.md Decision 2/this ticket's own Approach.
    """
    store_b = Store(tmp_path / "beta_names_derive.db")

    entry_a = store.resolve(NAME_A)
    entry_b = store_b.resolve(NAME_A)

    assert (entry_a.channel, entry_a.group) == (entry_b.channel, entry_b.group)


# ---------------------------------------------------------------------------
# ticket 009: connect_peer(remote_port=...) records the peer before
# connecting -- the fix for the pitfall ticket 005 left open (calling
# connect_peer alone, with no preceding record_peer_seen, connects and
# even snapshot-syncs successfully, but on_reachable's
# store.mark_peer_reachable then silently KeyErrors for a host with no
# peer row -- caught and logged, never surfacing). Ticket 009's --peer
# CLI flag is the caller that has no _BrowseListener doing the recording
# for it first (unlike every test above, which reproduces that
# mDNS-path pre-recording manually via record_peer_seen before
# connect_peer), so this is exactly its own code path, exercised
# directly.
# ---------------------------------------------------------------------------


def test_connect_peer_with_remote_port_records_peer_before_connecting(store, tmp_path):
    store_b = Store(tmp_path / "beta.db")
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_a = _make_peering(store, host="alpha", pub_port=17602, snapshot_port=17603)
    peering_b = _make_peering(store_b, host="beta", pub_port=17612, snapshot_port=17613)

    try:
        peering_a.start()
        peering_b.start()

        # No store_b.record_peer_seen(...) call here -- unlike every
        # other test in this module, this is deliberately the --peer
        # flag's own code path: connect_peer() alone, with remote_port
        # given, must do the recording itself.
        assert store_b.get_peer("alpha") is None
        peering_b.connect_peer(
            "alpha", "127.0.0.1", 17602, 17603, remote_port=_remote_port(17602)
        )

        assert _wait_until(lambda: store_b.get_peer("alpha") is not None)
        peer = store_b.get_peer("alpha")
        assert peer.reachable is True
        assert peer.endpoint == f"127.0.0.1:{_remote_port(17602)}"

        # And the snapshot itself still converges normally.
        assert _wait_until(lambda: store_b.get(UID) is not None)
        assert store_b.get(UID).host == "alpha"
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_connect_peer_without_remote_port_reproduces_ticket_005_behavior(store, tmp_path):
    """Documents the flip side of the fix above: omitting ``remote_port``
    (its default) is exactly ticket 005's original behavior -- the link
    still connects and snapshot-syncs, but with no preceding
    ``record_peer_seen``, no ``peer`` row ever appears, since
    ``_on_peer_reachable``'s ``mark_peer_reachable`` call finds nothing to
    mark and is caught/logged rather than raised. This is the exact
    pitfall :func:`test_connect_peer_with_remote_port_records_peer_before_connecting`
    fixes when a caller opts in via ``remote_port`` -- kept passing
    unchanged here since ticket 004/005's own mDNS-path tests all rely on
    this "connect_peer alone does not record" contract still holding when
    ``remote_port`` is not given.
    """
    store_b = Store(tmp_path / "beta.db")
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_a = _make_peering(store, host="alpha", pub_port=17622, snapshot_port=17623)
    peering_b = _make_peering(store_b, host="beta", pub_port=17632, snapshot_port=17633)

    try:
        peering_a.start()
        peering_b.start()

        peering_b.connect_peer("alpha", "127.0.0.1", 17622, 17623)

        # The snapshot still converges (the link itself works fine)...
        assert _wait_until(lambda: store_b.get(UID) is not None)
        # ...but no peer row was ever created, since nothing called
        # record_peer_seen and connect_peer's own remote_port was omitted.
        assert store_b.get_peer("alpha") is None
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


# ---------------------------------------------------------------------------
# ticket 010: connect_peer/_PeerLink.start() never block their caller
# (Decision 10's candidate 3 -- found while investigating braeburn's
# mDNS/peering asymmetry, docs/acceptance/004-hardware.md). connect_peer
# is, for the real mDNS path, invoked synchronously from python-zeroconf's
# own ServiceBrowser callback-dispatch thread -- it must never stall that
# thread for the snapshot REQ/REP round trip's full timeout.
# ---------------------------------------------------------------------------


def test_connect_peer_returns_immediately_against_unreachable_peer(store, tmp_path):
    """The regression this fix targets: before ticket 010, connect_peer()
    called _PeerLink.start() inline, which did the (blocking) snapshot
    REQ/REP round trip before returning -- up to the full
    ``_DEFAULT_SNAPSHOT_TIMEOUT_MS`` (5s) for a peer with nothing
    listening on its snapshot port, exactly this test's setup. connect_peer
    must now return in well under that -- the snapshot fetch and its
    eventual timeout happen on _PeerLink's own thread instead.
    """
    peering_b = _make_peering(store, host="beta", pub_port=17652, snapshot_port=17653)

    try:
        peering_b.start()

        started = time.monotonic()
        # 17654/17655: real, bound-but-unused loopback ports -- nothing
        # answers the SUB connect or the snapshot REQ, so the snapshot
        # fetch this triggers is guaranteed to run the full
        # snapshot_timeout_ms before giving up.
        peering_b.connect_peer("ghost", "127.0.0.1", 17654, 17655)
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, (
            f"connect_peer() blocked its caller for {elapsed:.2f}s -- "
            "the snapshot fetch must run on its own thread, not inline"
        )
    finally:
        peering_b.stop()
        store.close()


def test_peer_link_stop_joins_in_flight_snapshot_thread(store, tmp_path):
    """PeerDiscovery.stop() (via each _PeerLink.stop()) must still wait
    for an in-flight snapshot fetch's own thread before returning --
    ticket 010's async dispatch must not turn into "fire and forget":
    every thread this module starts is still stoppable and joined, same
    as every other background thread here.
    """
    peering_b = _make_peering(store, host="beta", pub_port=17662, snapshot_port=17663)

    try:
        peering_b.start()
        peering_b.connect_peer("ghost", "127.0.0.1", 17664, 17665)

        with peering_b._peer_links_lock:
            link = peering_b._peer_links["ghost"]
        assert link._snapshot_thread is not None

        peering_b.stop()

        assert link._snapshot_thread is None
        # stop() joined it rather than abandoning it mid-fetch.
        assert not (link._recv_thread and link._recv_thread.is_alive())
    finally:
        store.close()


def test_snapshot_success_after_link_dropped_does_not_resurrect_reachable(store, tmp_path):
    """The actual race ticket 010's async-dispatch fix (above) opened, and
    the fix for it: moving the snapshot fetch off the caller's thread
    means it and the SUB socket's own disconnect detection are now two
    independent signals about the same link, with no ordering guarantee
    between them. Found empirically (not guessed) while investigating
    braeburn: a real timing-based repro of the original bug showed a
    peer that dropped its link within the snapshot fetch's own window
    ended up permanently stuck ``reachable=True`` even though
    ``EVENT_DISCONNECTED`` had already fired correctly -- a late,
    genuinely-successful snapshot reply overwrote it back. Reproduced
    here deterministically (``_link_dropped`` set by hand, not by racing
    real threads) rather than depending on that timing to land in CI.
    """
    peering_a = _make_peering(store, host="alpha", pub_port=17672, snapshot_port=17673)
    store_b = Store(tmp_path / "beta.db")

    peering_b = _make_peering(store_b, host="beta", pub_port=17682, snapshot_port=17683)
    reachable_calls: list[str] = []

    try:
        peering_a.start()
        peering_b.start()
        store_b.record_peer_seen("alpha", "127.0.0.1:18672")

        link = peering_mod._PeerLink(
            host="alpha",
            store=store_b,
            zmq_module=peering_mod._real_zmq,
            context=peering_b._zmq_ctx,
            pub_address="tcp://127.0.0.1:17672",
            snapshot_address="tcp://127.0.0.1:17673",
            on_unreachable=None,
            on_reachable=lambda host: reachable_calls.append(host),
        )
        # This is the race's own ordering, forced rather than awaited:
        # the link is declared dropped *before* its (still in-flight, in
        # the real race) snapshot fetch resolves -- alpha is genuinely up
        # here, so _fetch_snapshot() below succeeds on the wire exactly
        # like the late-arriving reply in the real race did.
        link._link_dropped.set()
        link._fetch_snapshot()

        assert reachable_calls == [], (
            "on_reachable fired for a link already observed dropped -- "
            "the exact resurrection bug this guard exists to prevent"
        )
    finally:
        peering_a.stop()
        peering_b.stop()
        store_b.close()


# ---------------------------------------------------------------------------
# ticket 009: peering handshake auth (sprint.md Decision 6)
# ---------------------------------------------------------------------------


def test_snapshot_handshake_with_matching_auth_token_succeeds(store, tmp_path):
    store_b = Store(tmp_path / "beta.db")
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_a = PeerDiscovery(
        store=store,
        host="alpha",
        advertise_address="127.0.0.1",
        remote_port=_remote_port(17642),
        pub_port=17642,
        snapshot_port=17643,
        zeroconf=_FakeZeroconfNamespace(),
        auth_token="s3cret",
    )
    peering_b = PeerDiscovery(
        store=store_b,
        host="beta",
        advertise_address="127.0.0.1",
        remote_port=_remote_port(17652),
        pub_port=17652,
        snapshot_port=17653,
        zeroconf=_FakeZeroconfNamespace(),
        auth_token="s3cret",
    )

    try:
        peering_a.start()
        peering_b.start()
        peering_b.connect_peer(
            "alpha", "127.0.0.1", 17642, 17643, remote_port=_remote_port(17642)
        )

        assert _wait_until(lambda: store_b.get(UID) is not None)
        assert _wait_until(lambda: store_b.get_peer("alpha").reachable is True)
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()


def test_snapshot_handshake_with_wrong_auth_token_is_rejected(store, tmp_path):
    """A mismatched (or missing) token degrades the same silent-no-op way
    a snapshot timeout does (module docstring's "Peering handshake auth"
    note) -- no snapshot applied, no peer marked reachable, no
    exception raised on either side.
    """
    store_b = Store(tmp_path / "beta.db")
    store.upsert_attached(UID, "/dev/ttyACM0", "0d28:0204")

    peering_a = PeerDiscovery(
        store=store,
        host="alpha",
        advertise_address="127.0.0.1",
        remote_port=_remote_port(17662),
        pub_port=17662,
        snapshot_port=17663,
        zeroconf=_FakeZeroconfNamespace(),
        auth_token="s3cret",
    )
    peering_b = PeerDiscovery(
        store=store_b,
        host="beta",
        advertise_address="127.0.0.1",
        remote_port=_remote_port(17672),
        pub_port=17672,
        snapshot_port=17673,
        zeroconf=_FakeZeroconfNamespace(),
        auth_token="wrong",
    )

    try:
        peering_a.start()
        peering_b.start()
        peering_b.connect_peer(
            "alpha", "127.0.0.1", 17662, 17663, remote_port=_remote_port(17662)
        )

        # Give the (failed) snapshot exchange time to complete -- there is
        # no success condition to _wait_until on, so this sleeps a bound
        # generous enough for the REQ/REP round trip, then asserts the
        # negative: no device from alpha's snapshot ever applied.
        time.sleep(0.5)
        assert store_b.get(UID) is None
        # The peer row does exist -- connect_peer's own remote_port
        # recorded it as "discovered", the same as the mDNS path always
        # has, independent of whether the snapshot handshake that follows
        # then succeeds or is refused.
        peer = store_b.get_peer("alpha")
        assert peer is not None
    finally:
        peering_a.stop()
        peering_b.stop()
        store.close()
        store_b.close()
        store_b.close()
