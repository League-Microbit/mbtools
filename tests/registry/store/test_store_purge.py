"""Tests for sprint 010 ticket 001's ``Store.candidates_for_purge``/
``Store.purge`` -- the data-layer half of ``mbregistry rescan``.

Runs against a real SQLite file in tmp_path, same convention as
test_store.py/test_store_peer_host.py -- no mock database layer. This
module only exercises the store primitives themselves: no locks, no API,
no CLI -- see sprint.md's Step 3 ("store doesn't know about locks") and
this ticket's own scope note.
"""

from __future__ import annotations

import itertools

import pytest

from mbtools.registry.identity import ProbeResult
from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    PurgeCandidates,
    Store,
    format_vid_pid,
)

UID_LOCAL_GONE = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID_LOCAL_ATTACHED = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"
UID_UNREACHABLE_UNPROBED = (
    "9900" + "0000" + "11112222" + "1111222233334444" + "77778888" + "6e052820"
)
UID_UNREACHABLE_CONNECTED = (
    "9900" + "0000" + "11112222" + "5555666677778888" + "77778888" + "6e052820"
)
UID_REACHABLE_STALE = "9900" + "0000" + "11112222" + "9999aaaabbbbcccc" + "77778888" + "6e052820"
UID_REACHABLE_LIVE = "9900" + "0000" + "11112222" + "ddddeeeeffff0000" + "77778888" + "6e052820"
VID_PID = format_vid_pid(0x0D28, 0x0204)


def _clock(start: float = 1000.0, step: float = 1.0):
    counter = itertools.count()
    return lambda: start + step * next(counter)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db", now_fn=_clock())


def _probe(device_name: str = "vevov") -> ProbeResult:
    return ProbeResult(
        role="NEZHA2",
        common_name="robot",
        device_name=device_name,
        serial="123",
        raw=f"device NEZHA2 robot {device_name} 123",
    )


# ---------------------------------------------------------------------------
# candidates_for_purge: local disconnected rows
# ---------------------------------------------------------------------------


def test_local_disconnected_row_is_a_candidate(store):
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)

    candidates = store.candidates_for_purge()

    assert candidates.local_disconnected == (UID_LOCAL_GONE,)
    assert candidates.device_uids == (UID_LOCAL_GONE,)


def test_local_still_attached_row_is_never_a_candidate(store):
    store.upsert_attached(UID_LOCAL_ATTACHED, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(UID_LOCAL_ATTACHED, _probe())

    candidates = store.candidates_for_purge()

    assert candidates.device_uids == ()


def test_candidates_for_purge_does_not_mutate_anything(store):
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)

    store.candidates_for_purge()

    # The row is still there, untouched -- candidates_for_purge is a pure
    # read.
    record = store.get(UID_LOCAL_GONE)
    assert record is not None
    assert record.state == STATE_DISCONNECTED


# ---------------------------------------------------------------------------
# candidates_for_purge: unreachable peer, any device state
# ---------------------------------------------------------------------------


def test_unreachable_peers_devices_are_candidates_regardless_of_state(store):
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM0", VID_PID)
    store.upsert_remote_attached(UID_UNREACHABLE_CONNECTED, "braeburn", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID_UNREACHABLE_CONNECTED, _probe())
    store.mark_peer_unreachable("braeburn")

    candidates = store.candidates_for_purge()

    assert set(candidates.unreachable_peer_owned) == {
        UID_UNREACHABLE_UNPROBED,
        UID_UNREACHABLE_CONNECTED,
    }
    assert candidates.unreachable_peers == ("braeburn",)


def test_reachable_peers_non_disconnected_devices_are_never_candidates(store):
    store.record_peer_seen("loki", "loki:7440")
    store.upsert_remote_attached(UID_REACHABLE_LIVE, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID_REACHABLE_LIVE, _probe())

    candidates = store.candidates_for_purge()

    assert candidates.device_uids == ()
    assert candidates.unreachable_peers == ()


# ---------------------------------------------------------------------------
# candidates_for_purge: reachable peer, stale disconnected row (005-011)
# ---------------------------------------------------------------------------


def test_reachable_peers_stale_disconnected_row_is_a_candidate(store):
    store.record_peer_seen("loki", "loki:7440")
    store.upsert_remote_attached(UID_REACHABLE_STALE, "loki", "/dev/ttyACM0", VID_PID)
    store.mark_remote_detached(UID_REACHABLE_STALE)

    candidates = store.candidates_for_purge()

    assert candidates.reachable_peer_stale_disconnected == (UID_REACHABLE_STALE,)
    # The peer itself is reachable, so it's not in the unreachable-peers set.
    assert candidates.unreachable_peers == ()


def test_purge_candidates_device_uids_combines_all_three_groups(store):
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)

    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM1", VID_PID)
    store.mark_peer_unreachable("braeburn")

    store.record_peer_seen("loki", "loki:7440")
    store.upsert_remote_attached(UID_REACHABLE_STALE, "loki", "/dev/ttyACM2", VID_PID)
    store.mark_remote_detached(UID_REACHABLE_STALE)

    candidates = store.candidates_for_purge()

    assert set(candidates.device_uids) == {
        UID_LOCAL_GONE,
        UID_UNREACHABLE_UNPROBED,
        UID_REACHABLE_STALE,
    }


# ---------------------------------------------------------------------------
# candidates_for_purge: unreachable peer rows
# ---------------------------------------------------------------------------


def test_unreachable_peer_row_is_a_candidate(store):
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.mark_peer_unreachable("braeburn")

    candidates = store.candidates_for_purge()

    assert candidates.unreachable_peers == ("braeburn",)


def test_reachable_peer_row_is_never_a_candidate(store):
    store.record_peer_seen("loki", "loki:7440")

    candidates = store.candidates_for_purge()

    assert candidates.unreachable_peers == ()


# ---------------------------------------------------------------------------
# purge: actually deletes what candidates_for_purge found
# ---------------------------------------------------------------------------


def test_purge_deletes_local_disconnected_row(store):
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)
    candidates = store.candidates_for_purge()

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert result.removed_device_uids == (UID_LOCAL_GONE,)
    assert store.get(UID_LOCAL_GONE) is None


def test_purge_deletes_unreachable_peer_and_its_devices(store):
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM0", VID_PID)
    store.upsert_remote_attached(UID_UNREACHABLE_CONNECTED, "braeburn", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID_UNREACHABLE_CONNECTED, _probe())
    store.mark_peer_unreachable("braeburn")
    candidates = store.candidates_for_purge()

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert set(result.removed_device_uids) == {UID_UNREACHABLE_UNPROBED, UID_UNREACHABLE_CONNECTED}
    assert result.removed_peer_hosts == ("braeburn",)
    assert store.get(UID_UNREACHABLE_UNPROBED) is None
    assert store.get(UID_UNREACHABLE_CONNECTED) is None
    assert store.get_peer("braeburn") is None


def test_purge_deletes_reachable_peers_stale_disconnected_row(store):
    store.record_peer_seen("loki", "loki:7440")
    store.upsert_remote_attached(UID_REACHABLE_STALE, "loki", "/dev/ttyACM0", VID_PID)
    store.mark_remote_detached(UID_REACHABLE_STALE)
    candidates = store.candidates_for_purge()

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert result.removed_device_uids == (UID_REACHABLE_STALE,)
    assert store.get(UID_REACHABLE_STALE) is None
    # loki itself is reachable and was never a candidate -- still there.
    assert store.get_peer("loki") is not None


def test_purge_never_touches_connected_or_attached_rows(store):
    store.upsert_attached(UID_LOCAL_ATTACHED, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(UID_LOCAL_ATTACHED, _probe())
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM1", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)
    candidates = store.candidates_for_purge()

    store.purge(candidates.device_uids, candidates.unreachable_peers)

    record = store.get(UID_LOCAL_ATTACHED)
    assert record is not None
    assert record.state == STATE_CONNECTED


def test_purge_peer_row_does_not_cascade_delete_its_devices(store):
    # purge's two arguments are independent -- passing only the peer host,
    # not its device uids, must delete the peer row and leave its devices
    # alone (no foreign key, no cascade).
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM0", VID_PID)
    store.mark_peer_unreachable("braeburn")

    result = store.purge([], ["braeburn"])

    assert result.removed_peer_hosts == ("braeburn",)
    assert store.get_peer("braeburn") is None
    # The device row is untouched -- still there, still braeburn-owned.
    record = store.get(UID_UNREACHABLE_UNPROBED)
    assert record is not None
    assert record.host == "braeburn"


# ---------------------------------------------------------------------------
# purge: name_registry is never read or written
# ---------------------------------------------------------------------------


def test_purge_and_candidates_for_purge_never_touch_name_registry(store):
    store.set("zavaz", channel=3, group=1)
    before = store.listing()

    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)
    candidates = store.candidates_for_purge()
    store.purge(candidates.device_uids, candidates.unreachable_peers)

    after = store.listing()
    assert after == before


# ---------------------------------------------------------------------------
# Mid-reattach / mid-reconnect race: survives and is reported as not-removed
# ---------------------------------------------------------------------------


def test_uid_that_reattaches_between_candidates_and_purge_survives(store):
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)
    store.mark_disconnected(UID_LOCAL_GONE)
    candidates = store.candidates_for_purge()
    assert candidates.device_uids == (UID_LOCAL_GONE,)

    # The race: the device reattaches after candidates were computed, but
    # before purge() runs.
    store.upsert_attached(UID_LOCAL_GONE, "/dev/ttyACM0", VID_PID)

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert result.removed_device_uids == ()
    record = store.get(UID_LOCAL_GONE)
    assert record is not None
    assert record.state == STATE_ATTACHED_UNPROBED


def test_peer_that_reconnects_between_candidates_and_purge_survives(store):
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM0", VID_PID)
    store.mark_peer_unreachable("braeburn")
    candidates = store.candidates_for_purge()
    assert candidates.unreachable_peers == ("braeburn",)
    assert candidates.device_uids == (UID_UNREACHABLE_UNPROBED,)

    # The race: braeburn comes back before purge() runs.
    store.mark_peer_reachable("braeburn")

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert result.removed_device_uids == ()
    assert result.removed_peer_hosts == ()
    assert store.get_peer("braeburn") is not None
    assert store.get(UID_UNREACHABLE_UNPROBED) is not None


def test_peer_reseen_between_candidates_and_purge_survives(store):
    # record_peer_seen (a fresh discovery/reconnect, not just
    # mark_peer_reachable) is the other way a peer comes back to life.
    store.record_peer_seen("braeburn", "braeburn:7440")
    store.upsert_remote_attached(UID_UNREACHABLE_UNPROBED, "braeburn", "/dev/ttyACM0", VID_PID)
    store.mark_peer_unreachable("braeburn")
    candidates = store.candidates_for_purge()

    store.record_peer_seen("braeburn", "braeburn:7440")

    result = store.purge(candidates.device_uids, candidates.unreachable_peers)

    assert result.removed_device_uids == ()
    assert result.removed_peer_hosts == ()


# ---------------------------------------------------------------------------
# purge: absent uid/host is a no-op, not an error
# ---------------------------------------------------------------------------


def test_purge_absent_uid_and_host_is_a_no_op(store):
    result = store.purge(["no-such-uid"], ["no-such-host"])

    assert result.removed_device_uids == ()
    assert result.removed_peer_hosts == ()


def test_purge_with_empty_collections_is_a_no_op(store):
    result = store.purge([], [])

    assert result.removed_device_uids == ()
    assert result.removed_peer_hosts == ()


# ---------------------------------------------------------------------------
# PurgeCandidates is a plain, importable dataclass
# ---------------------------------------------------------------------------


def test_candidates_for_purge_returns_purge_candidates_instance(store):
    assert isinstance(store.candidates_for_purge(), PurgeCandidates)
