"""Tests for mbtools.registry.store -- the SQLite device database and
re-probe-eligibility fields (ticket 004).

Every test here runs against a real SQLite file in tmp_path, per sprint.md's
Test Strategy ("store CRUD runs against a real SQLite file in tmp_path") --
no mock database layer, since SQLite itself is the thing being tested.
"""

from __future__ import annotations

import itertools
import sys

import pytest

from mbtools.registry import store as store_mod
from mbtools.registry.identity import ProbeResult, short_uid
from mbtools.registry.store import (
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


def test_apply_probe_result_none_marks_no_firmware_and_preserves_fields(store):
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

    assert record.state == STATE_CONNECTED_NO_FIRMWARE
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

    assert record.state == STATE_CONNECTED_NO_FIRMWARE
    assert record.role is None
    assert record.device_name is None
    assert record.last_probe != 0.0


def test_apply_probe_result_unknown_uid_raises_key_error(store):
    with pytest.raises(KeyError):
        store.apply_probe_result(UID, None)


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
