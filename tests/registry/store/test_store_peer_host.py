"""Tests for sprint-003 ticket 001's additions to mbtools.registry.store --
the ``host``/``remote_lock_*`` device columns, the ``peer`` table, and
``name@host`` resolution.

Runs against a real SQLite file in tmp_path, same convention as
test_store.py -- no mock database layer.
"""

from __future__ import annotations

import itertools
import sqlite3

import pytest

from mbtools.registry.identity import ProbeResult, short_uid
from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_DISCONNECTED,
    AmbiguousNameError,
    Store,
    format_vid_pid,
)

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"
VID_PID = format_vid_pid(0x0D28, 0x0204)


def _clock(start: float = 1000.0, step: float = 1.0):
    counter = itertools.count()
    return lambda: start + step * next(counter)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db", now_fn=_clock())


def _probe(device_name: str, role: str = "NEZHA2", common_name: str = "robot") -> ProbeResult:
    return ProbeResult(
        role=role,
        common_name=common_name,
        device_name=device_name,
        serial="123",
        raw=f"device {role} {common_name} {device_name} 123",
    )


# ---------------------------------------------------------------------------
# Migration: sprint-1/2 database -> sprint-003 schema, in place
# ---------------------------------------------------------------------------


def _create_sprint_1_2_db(db_path) -> None:
    """Build a database with exactly the sprint-1/2 schema (no host/peer),
    pre-populated with one row, mirroring what a live sprint-1/2
    deployment's devices.db looks like before this ticket's migration
    runs against it."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE device (
            uid TEXT PRIMARY KEY,
            short_uid TEXT NOT NULL,
            port TEXT,
            vid_pid TEXT,
            role TEXT,
            common_name TEXT,
            device_name TEXT,
            serial_payload TEXT,
            raw_announcement TEXT,
            state TEXT NOT NULL,
            error_note TEXT,
            flash_count INTEGER NOT NULL DEFAULT 0,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL,
            last_probe REAL NOT NULL DEFAULT 0.0
        )
        """
    )
    conn.execute(
        """
        INSERT INTO device (
            uid, short_uid, port, vid_pid, role, common_name, device_name,
            serial_payload, raw_announcement, state, error_note, flash_count,
            first_seen, last_seen, last_probe
        ) VALUES (?, ?, '/dev/ttyACM0', ?, 'NEZHA2', 'robot', 'vevov', '123',
                  'raw', 'connected', NULL, 2, 1000.0, 1000.0, 1000.0)
        """,
        (UID, short_uid(UID), VID_PID),
    )
    conn.commit()
    conn.close()


def test_migration_adds_new_columns_and_peer_table_without_losing_data(tmp_path):
    db_path = tmp_path / "devices.db"
    _create_sprint_1_2_db(db_path)

    store = Store(db_path, now_fn=_clock())

    # Existing row untouched.
    record = store.get(UID)
    assert record is not None
    assert record.device_name == "vevov"
    assert record.flash_count == 2
    assert record.state == STATE_CONNECTED
    # New columns exist and default to NULL for a pre-existing row.
    assert record.host is None
    assert record.remote_lock_kind is None
    assert record.remote_lock_display is None
    # New peer table exists and is usable.
    assert store.list_peers() == []
    store.record_peer_seen("loki", "loki:7440")
    assert store.get_peer("loki") is not None


def test_fresh_database_has_full_schema_via_same_migration_path(tmp_path):
    db_path = tmp_path / "devices.db"
    assert not db_path.exists()

    store = Store(db_path, now_fn=_clock())

    record = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert record.host is None
    assert record.remote_lock_kind is None
    assert record.remote_lock_display is None
    assert store.list_peers() == []
    peer = store.record_peer_seen("loki", "loki:7440")
    assert peer.host == "loki"


def test_reopening_migrated_db_does_not_re_migrate_or_lose_peer_data(tmp_path):
    db_path = tmp_path / "devices.db"
    _create_sprint_1_2_db(db_path)

    first = Store(db_path, now_fn=_clock())
    first.record_peer_seen("loki", "loki:7440")
    first.close()

    second = Store(db_path, now_fn=_clock())
    peer = second.get_peer("loki")
    assert peer is not None
    assert peer.endpoint == "loki:7440"
    record = second.get(UID)
    assert record is not None
    assert record.device_name == "vevov"


# ---------------------------------------------------------------------------
# upsert_remote_attached / mark_remote_detached / apply_remote_probe
# ---------------------------------------------------------------------------


def test_upsert_remote_attached_creates_record_with_host(store):
    record = store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)

    assert record.uid == UID
    assert record.host == "loki"
    assert record.state == STATE_ATTACHED_UNPROBED
    assert record.last_probe == 0.0


def test_upsert_remote_attached_reattach_resets_probe_eligibility(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("vevov"))
    store.mark_remote_detached(UID)

    reattached = store.upsert_remote_attached(UID, "loki", "/dev/ttyACM2", VID_PID)

    assert reattached.state == STATE_ATTACHED_UNPROBED
    assert reattached.last_probe == 0.0
    assert reattached.host == "loki"
    # Announcement history from before the detach is preserved.
    assert reattached.device_name == "vevov"


def test_upsert_attached_never_sets_host(store):
    record = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert record.host is None


def test_mark_remote_detached_sets_disconnected_and_never_deletes(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)

    record = store.mark_remote_detached(UID)

    assert record.state == STATE_DISCONNECTED
    assert store.get(UID) is not None


def test_mark_remote_detached_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.mark_remote_detached(UID)


def test_apply_remote_probe_success_updates_announcement_fields(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)

    record = store.apply_remote_probe(UID, _probe("vevov"))

    assert record.state == STATE_CONNECTED
    assert record.device_name == "vevov"
    assert record.host == "loki"


def test_apply_remote_probe_none_preserves_existing_fields(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("vevov"))

    record = store.apply_remote_probe(UID, None)

    assert record.device_name == "vevov"
    assert record.error_note


def test_apply_remote_probe_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.apply_remote_probe(UID, None)


# ---------------------------------------------------------------------------
# apply_remote_lock_state -- never touches LockManager
# ---------------------------------------------------------------------------


def test_apply_remote_lock_state_sets_cache_columns(store):
    # Note: no LockManager is constructed anywhere in this test module --
    # apply_remote_lock_state must work without one, proving it never
    # consults real lock state.
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)

    record = store.apply_remote_lock_state(UID, "flash", "locked for flash by loki")

    assert record.remote_lock_kind == "flash"
    assert record.remote_lock_display == "locked for flash by loki"


def test_apply_remote_lock_state_clears_with_none(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_lock_state(UID, "flash", "locked for flash by loki")

    record = store.apply_remote_lock_state(UID, None, None)

    assert record.remote_lock_kind is None
    assert record.remote_lock_display is None


def test_apply_remote_lock_state_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.apply_remote_lock_state(UID, "flash", "locked")


def test_apply_remote_lock_state_never_touches_local_row_semantics(store):
    # A local row's remote_lock_* fields stay None; nothing in this store
    # module ever reads them for a local row (that's render's job, out of
    # scope for this ticket) -- this just proves upsert_attached doesn't
    # populate them incidentally.
    record = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert record.remote_lock_kind is None
    assert record.remote_lock_display is None


# ---------------------------------------------------------------------------
# peer CRUD
# ---------------------------------------------------------------------------


def test_record_peer_seen_creates_new_peer(store):
    peer = store.record_peer_seen("loki", "loki:7440")

    assert peer.host == "loki"
    assert peer.endpoint == "loki:7440"
    assert peer.reachable is True


def test_record_peer_seen_updates_existing_peer(store):
    store.record_peer_seen("loki", "loki:7440")
    store.mark_peer_unreachable("loki")

    updated = store.record_peer_seen("loki", "loki:7441")

    assert updated.endpoint == "loki:7441"
    assert updated.reachable is True


def test_mark_peer_unreachable_then_reachable(store):
    store.record_peer_seen("loki", "loki:7440")

    unreachable = store.mark_peer_unreachable("loki")
    assert unreachable.reachable is False

    reachable = store.mark_peer_reachable("loki")
    assert reachable.reachable is True


def test_mark_peer_unreachable_unknown_host_raises_key_error(store):
    with pytest.raises(KeyError):
        store.mark_peer_unreachable("loki")


def test_mark_peer_reachable_unknown_host_raises_key_error(store):
    with pytest.raises(KeyError):
        store.mark_peer_reachable("loki")


def test_mark_peer_unreachable_never_deletes_the_peer_row(store):
    store.record_peer_seen("loki", "loki:7440")
    store.mark_peer_unreachable("loki")

    assert store.get_peer("loki") is not None
    hosts = [p.host for p in store.list_peers()]
    assert "loki" in hosts


def test_list_peers_empty(store):
    assert store.list_peers() == []


def test_list_peers_returns_all(store):
    store.record_peer_seen("loki", "loki:7440")
    store.record_peer_seen("hodr", "hodr:7440")

    hosts = {p.host for p in store.list_peers()}
    assert hosts == {"loki", "hodr"}


def test_get_peer_returns_none_for_unknown_host(store):
    assert store.get_peer("loki") is None


# ---------------------------------------------------------------------------
# snapshot_local_devices
# ---------------------------------------------------------------------------


def test_snapshot_local_devices_excludes_remote_rows(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.upsert_remote_attached(UID2, "loki", "/dev/ttyACM1", VID_PID)

    snapshot = store.snapshot_local_devices()

    uids = {d.uid for d in snapshot}
    assert uids == {UID}


def test_snapshot_local_devices_empty_when_all_remote(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    assert store.snapshot_local_devices() == []


# ---------------------------------------------------------------------------
# find: name@host resolution and AmbiguousNameError
# ---------------------------------------------------------------------------


def test_find_name_at_host_resolves_remote_device(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("zavaz"))

    record = store.find("zavaz@loki")

    assert record is not None
    assert record.uid == UID


def test_find_name_at_host_is_case_insensitive_on_both_parts(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("zavaz"))

    assert store.find("ZAVAZ@LOKI").uid == UID
    assert store.find("Zavaz@Loki").uid == UID


def test_find_name_at_host_returns_none_when_host_does_not_match(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("zavaz"))

    assert store.find("zavaz@hodr") is None


def test_find_name_at_host_does_not_match_local_device(store):
    # A local device's host is NULL, not the string "local" -- @host
    # syntax has no way to name it, which is fine since a bare name
    # already resolves a local device unambiguously when there's no
    # collision.
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(UID, _probe("zavaz"))

    assert store.find("zavaz@local") is None


def test_find_bare_name_raises_ambiguous_when_two_hosts_share_it(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(UID, _probe("zavaz"))
    store.upsert_remote_attached(UID2, "loki", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID2, _probe("zavaz"))

    with pytest.raises(AmbiguousNameError) as excinfo:
        store.find("zavaz")

    assert "zavaz" in str(excinfo.value)
    assert None in excinfo.value.hosts
    assert "loki" in excinfo.value.hosts


def test_find_bare_name_ambiguous_across_two_remote_hosts(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("zavaz"))
    store.upsert_remote_attached(UID2, "hodr", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID2, _probe("zavaz"))

    with pytest.raises(AmbiguousNameError):
        store.find("zavaz")


def test_find_bare_name_resolves_cleanly_when_only_one_host_has_it(store):
    store.upsert_remote_attached(UID, "loki", "/dev/ttyACM0", VID_PID)
    store.apply_remote_probe(UID, _probe("zavaz"))
    store.upsert_remote_attached(UID2, "hodr", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID2, _probe("different-name"))

    record = store.find("zavaz")
    assert record.uid == UID


def test_find_uid_match_never_ambiguous_even_with_colliding_name_elsewhere(store):
    # A uid/short_uid match short-circuits before the ambiguity check is
    # ever reached, by definition (uid is globally unique).
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.upsert_remote_attached(UID2, "loki", "/dev/ttyACM1", VID_PID)
    store.apply_remote_probe(UID2, _probe(UID))  # device_name collides with UID

    record = store.find(UID)
    assert record.uid == UID


def test_find_short_uid_match_never_ambiguous(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    record = store.find(short_uid(UID))
    assert record.uid == UID
