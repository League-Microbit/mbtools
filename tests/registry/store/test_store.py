"""Tests for mbtools.registry.store -- the SQLite device database and
re-probe-eligibility fields (ticket 004).

Every test here runs against a real SQLite file in tmp_path, per sprint.md's
Test Strategy ("store CRUD runs against a real SQLite file in tmp_path") --
no mock database layer, since SQLite itself is the thing being tested.
"""

from __future__ import annotations

import itertools
import sqlite3
import sys

import pytest

from mbtools.registry import store as store_mod
from mbtools.registry.identity import ProbeResult, short_uid
from mbtools.registry.store import (
    STATE_ATTACHED_NO_ANNOUNCE,
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
    Store,
    format_vid_pid,
)

UID = "9900" + "0000" + "11112222" + "3333444455556666" + "77778888" + "6e052820"
UID2 = "9900" + "0000" + "11112222" + "aaaabbbbccccdddd" + "77778888" + "6e052820"
VID_PID = format_vid_pid(0x0D28, 0x0204)


def _clock(start: float = 1000.0, step: float = 1.0):
    """A deterministic, monotonically-increasing now_fn -- so a test can
    assert on first_seen/last_seen/last_probe ordering without sleeping or
    depending on real wall-clock time."""
    counter = itertools.count()
    return lambda: start + step * next(counter)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "devices.db", now_fn=_clock())


# ---------------------------------------------------------------------------
# Schema / construction
# ---------------------------------------------------------------------------


def test_schema_created_on_first_use(tmp_path):
    db_path = tmp_path / "sub" / "devices.db"
    assert not db_path.exists()
    Store(db_path)
    assert db_path.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "DEFAULT_DB_PATH is bound once, at real import time, from "
        "registry.paths.default_db_path()'s own platform dispatch -- "
        "on real Windows it is genuinely a %ProgramData%-rooted path, "
        "not /var/lib/... (see that function's own docstring); this "
        "test's subject is the non-Windows default specifically"
    ),
)
def test_default_db_path_comes_from_registry_paths():
    from mbtools.registry.paths import default_db_path

    assert store_mod.DEFAULT_DB_PATH == default_db_path()


def test_reopening_existing_db_does_not_lose_data(tmp_path):
    db_path = tmp_path / "devices.db"
    first = Store(db_path, now_fn=_clock())
    first.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    first.close()

    second = Store(db_path, now_fn=_clock())
    record = second.get(UID)
    assert record is not None
    assert record.uid == UID


# ---------------------------------------------------------------------------
# upsert_attached
# ---------------------------------------------------------------------------


def test_upsert_attached_creates_new_record(store):
    record = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert record.uid == UID
    assert record.short_uid == short_uid(UID)
    assert record.port == "/dev/ttyACM0"
    assert record.vid_pid == VID_PID
    assert record.state == STATE_ATTACHED_UNPROBED
    assert record.role is None
    assert record.common_name is None
    assert record.device_name is None
    assert record.serial_payload is None
    assert record.raw_announcement is None
    assert record.error_note is None
    assert record.flash_count == 0
    assert record.last_probe == 0.0
    assert record.first_seen == record.last_seen


def test_upsert_attached_updates_port_and_last_seen_without_touching_announcement(
    store,
):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )
    before = store.get(UID)

    after = store.upsert_attached(UID, "/dev/ttyACM1", VID_PID)

    assert after.port == "/dev/ttyACM1"
    assert after.last_seen > before.last_seen
    assert after.role == before.role
    assert after.common_name == before.common_name
    assert after.device_name == before.device_name
    assert after.raw_announcement == before.raw_announcement
    # Already connected and still attached (no disconnect happened) -- not
    # a reattach, so state/last_probe are untouched (SUC-007's "daemon
    # restart is not a reattach" data-side contract).
    assert after.state == STATE_CONNECTED
    assert after.last_probe == before.last_probe


def test_upsert_attached_after_disconnect_resets_probe_eligibility(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )
    store.increment_flash_count(UID)
    probed = store.get(UID)
    assert probed.last_probe != 0.0

    store.mark_disconnected(UID)
    reattached = store.upsert_attached(UID, "/dev/ttyACM2", VID_PID)

    assert reattached.state == STATE_ATTACHED_UNPROBED
    assert reattached.last_probe == 0.0
    assert store.needs_probe(UID) is True
    # History survives the disconnect/reattach cycle.
    assert reattached.flash_count == 1
    assert reattached.first_seen == probed.first_seen


# ---------------------------------------------------------------------------
# apply_probe_result
# ---------------------------------------------------------------------------


def test_apply_probe_result_success_updates_announcement_fields(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    record = store.apply_probe_result(
        UID,
        ProbeResult(
            role="RADIOBRIDGE", common_name="relay", device_name="getez",
            serial="1779042496", raw="DEVICE:RADIOBRIDGE:relay:getez:1779042496",
        ),
    )

    assert record.state == STATE_CONNECTED
    assert record.role == "RADIOBRIDGE"
    assert record.common_name == "relay"
    assert record.device_name == "getez"
    assert record.serial_payload == "1779042496"
    assert record.raw_announcement == "DEVICE:RADIOBRIDGE:relay:getez:1779042496"
    assert record.error_note is None
    assert record.last_probe != 0.0


def test_apply_probe_result_none_marks_no_announce_and_preserves_fields(store):
    """Sprint 007, ticket 001: a plain silent probe (``result=None``) now
    lands on ``STATE_ATTACHED_NO_ANNOUNCE`` ("we asked and got nothing
    back"), not ``STATE_CONNECTED_NO_FIRMWARE`` -- that constant is
    reserved from this ticket forward for a case the store can actually
    assert is blank (see :meth:`Store.apply_known_blank` below)."""
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )
    before = store.get(UID)

    record = store.apply_probe_result(UID, None)

    assert record.state == STATE_ATTACHED_NO_ANNOUNCE
    assert record.error_note
    # Previously-known announcement fields are untouched -- this is the
    # "preserve existing announcement fields unchanged" rule this module
    # ports from mbdeploy/mbrelay.
    assert record.role == before.role
    assert record.common_name == before.common_name
    assert record.device_name == before.device_name
    assert record.serial_payload == before.serial_payload
    assert record.raw_announcement == before.raw_announcement
    assert record.last_probe > before.last_probe


def test_apply_probe_result_none_on_never_probed_device(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    record = store.apply_probe_result(UID, None)

    assert record.state == STATE_ATTACHED_NO_ANNOUNCE
    assert record.role is None
    assert record.device_name is None
    assert record.last_probe != 0.0


def test_apply_probe_result_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.apply_probe_result(UID, None)


# ---------------------------------------------------------------------------
# apply_known_blank (sprint 007, ticket 001) -- the genuinely-known-blank
# sibling of apply_probe_result(uid, None)
# ---------------------------------------------------------------------------


def test_apply_known_blank_marks_connected_no_firmware_and_preserves_fields(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="JOYSTICK", common_name="joystick", device_name="togov",
            serial="123", raw="DEVICE:JOYSTICK:joystick:togov:123",
        ),
    )
    before = store.get(UID)

    record = store.apply_known_blank(UID)

    assert record.state == STATE_CONNECTED_NO_FIRMWARE
    assert record.error_note
    # Same "preserve existing announcement fields" rule as
    # apply_probe_result(uid, None) -- a known-blank re-probe (e.g. after a
    # failed mass-erase reflash) must not silently keep showing the old
    # JOYSTICK/joystick firmware fields as if they were still live, but it
    # also must not erase the history of what was last seen there.
    assert record.role == before.role
    assert record.common_name == before.common_name
    assert record.device_name == before.device_name
    assert record.last_probe > before.last_probe


def test_apply_known_blank_distinct_error_note_from_plain_no_announce(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)

    no_announce = store.apply_probe_result(UID, None)
    known_blank = store.apply_known_blank(UID2)

    assert no_announce.state == STATE_ATTACHED_NO_ANNOUNCE
    assert known_blank.state == STATE_CONNECTED_NO_FIRMWARE
    assert no_announce.error_note != known_blank.error_note


def test_apply_known_blank_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.apply_known_blank(UID)


# ---------------------------------------------------------------------------
# chip-identity cache (sprint 007, ticket 001)
# ---------------------------------------------------------------------------


def test_set_chip_identity_persists_name_and_serial(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    record = store.set_chip_identity(UID, "tovez", 2314287040)

    assert record.chip_identity_name == "tovez"
    assert record.chip_identity_serial == 2314287040
    # Read-back via a fresh get() -- not just the returned record.
    assert store.get(UID).chip_identity_name == "tovez"
    assert store.get(UID).chip_identity_serial == 2314287040


def test_set_chip_identity_is_write_once(store):
    """Once a chip identity is cached for a uid, a second call must not
    overwrite it -- sprint.md SUC-001's "cached forever ... never
    re-read" postcondition, enforced here as the store's own second line
    of defense (the primary "don't call SWD twice" gate is
    ``registry.daemon``'s job, ticket 003)."""
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.set_chip_identity(UID, "tovez", 2314287040)

    record = store.set_chip_identity(UID, "zavaz", 999)

    assert record.chip_identity_name == "tovez"
    assert record.chip_identity_serial == 2314287040


def test_set_chip_identity_unaffected_by_later_announcement_only_update(store):
    """apply_probe_result with a real ProbeResult must not clear (or
    touch) the chip-identity cache columns -- they are independent of the
    announcement-derived fields."""
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.set_chip_identity(UID, "tovez", 2314287040)

    record = store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )

    assert record.chip_identity_name == "tovez"
    assert record.chip_identity_serial == 2314287040
    assert record.device_name == "vevov"  # announcement field still applied


def test_set_chip_identity_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.set_chip_identity(UID, "tovez", 2314287040)


def test_new_device_row_has_no_chip_identity_yet(store):
    record = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert record.chip_identity_name is None
    assert record.chip_identity_serial is None


# ---------------------------------------------------------------------------
# chip-identity columns migrate cleanly onto a pre-existing devices.db
# ---------------------------------------------------------------------------


def test_chip_identity_columns_migrate_onto_pre_sprint_007_database(tmp_path):
    """A devices.db created before this ticket (missing the
    chip_identity_name/chip_identity_serial columns) opens and migrates
    cleanly -- mirrors the existing sprint-003 _NEW_DEVICE_COLUMNS
    migration test pattern: create a table shaped like the pre-ticket
    schema by hand, then open it through Store and confirm the new
    columns exist and a row (inserted before the migration) reads back
    with the new fields defaulting to None."""
    db_path = tmp_path / "pre-existing.db"
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
            last_probe REAL NOT NULL DEFAULT 0.0,
            host TEXT,
            remote_lock_kind TEXT,
            remote_lock_display TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO device (uid, short_uid, port, vid_pid, state, flash_count, "
        "first_seen, last_seen, last_probe) VALUES (?, ?, ?, ?, ?, 0, 0.0, 0.0, 0.0)",
        (UID, short_uid(UID), "/dev/ttyACM0", VID_PID, STATE_ATTACHED_UNPROBED),
    )
    conn.commit()
    conn.close()

    store = Store(db_path)
    try:
        record = store.get(UID)
        assert record is not None
        assert record.chip_identity_name is None
        assert record.chip_identity_serial is None

        # And the new columns are fully usable going forward.
        updated = store.set_chip_identity(UID, "tovez", 2314287040)
        assert updated.chip_identity_name == "tovez"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# mark_disconnected -- never deleted
# ---------------------------------------------------------------------------


def test_mark_disconnected_sets_state_and_updates_last_seen(store):
    before = store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    record = store.mark_disconnected(UID)

    assert record.state == STATE_DISCONNECTED
    assert record.last_seen > before.last_seen


def test_mark_disconnected_never_deletes_the_record(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    store.mark_disconnected(UID)

    uids = [d.uid for d in store.list_devices()]
    assert UID in uids
    assert store.get(UID) is not None


def test_mark_disconnected_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.mark_disconnected(UID)


# ---------------------------------------------------------------------------
# needs_probe
# ---------------------------------------------------------------------------


def test_needs_probe_true_for_never_probed_device(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert store.needs_probe(UID) is True


def test_needs_probe_false_for_already_probed_still_attached_device(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )
    assert store.needs_probe(UID) is False


def test_needs_probe_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.needs_probe(UID)


# ---------------------------------------------------------------------------
# increment_flash_count
# ---------------------------------------------------------------------------


def test_increment_flash_count_bumps_from_zero(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)

    record = store.increment_flash_count(UID)
    assert record.flash_count == 1

    record = store.increment_flash_count(UID)
    assert record.flash_count == 2


def test_increment_flash_count_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.increment_flash_count(UID)


# ---------------------------------------------------------------------------
# list_devices
# ---------------------------------------------------------------------------


def test_list_devices_empty_store(store):
    assert store.list_devices() == []


def test_list_devices_includes_disconnected(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)
    store.mark_disconnected(UID)

    devices = store.list_devices()

    uids = {d.uid for d in devices}
    assert uids == {UID, UID2}
    states = {d.uid: d.state for d in devices}
    assert states[UID] == STATE_DISCONNECTED
    assert states[UID2] == STATE_ATTACHED_UNPROBED


# ---------------------------------------------------------------------------
# get / find
# ---------------------------------------------------------------------------


def test_get_returns_none_for_unknown_uid(store):
    assert store.get(UID) is None


def test_find_by_exact_uid(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    record = store.find(UID)
    assert record is not None
    assert record.uid == UID


def test_find_by_short_uid(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    record = store.find(short_uid(UID))
    assert record is not None
    assert record.uid == UID


def test_find_by_device_name_case_insensitive(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov",
            serial="123", raw="device NEZHA2 robot vevov 123",
        ),
    )
    assert store.find("vevov").uid == UID
    assert store.find("VEVOV").uid == UID


def test_find_returns_none_when_nothing_matches(store):
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    assert store.find("no-such-token") is None


def test_find_precedence_uid_before_device_name(store):
    """A token that is device A's uid, but also happens to equal device
    B's device_name, must resolve to device A -- uid match wins, per
    resolve_target's precedence family."""
    store.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    store.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)
    store.apply_probe_result(
        UID2,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name=UID,
            serial="123", raw=f"device NEZHA2 robot {UID} 123",
        ),
    )

    assert store.find(UID).uid == UID


# ---------------------------------------------------------------------------
# format_vid_pid
# ---------------------------------------------------------------------------


def test_format_vid_pid():
    assert format_vid_pid(0x0D28, 0x0204) == "0d28:0204"
