"""Tests for ``mbtools.registry.render`` (ticket 002) -- pure unit tests
over hand-built device-dict lists, no socket, no daemon, no ``Store``.
Mirrors the ticket's Testing section: "pure unit tests over hand-built
device-dict lists ... covering every STATE value (free / locked by
kind+pid / no-firmware / gone) and both table and JSON output shapes."

``tests/registry/cli/test_cli_list.py`` (sprint 001, unmodified per this
ticket's acceptance criteria) is the byte-for-byte regression check that
``mbregistry list`` itself still produces the same output through this
module -- these tests instead exercise :mod:`mbtools.registry.render`
directly, as ``mbdeploy list`` (ticket 008) will.
"""

from __future__ import annotations

from mbtools.registry.render import TABLE_HEADERS, render_json, render_table
from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
)


def _device(**overrides) -> dict:
    """A minimal device dict in the shape ``registry.client.list()``
    returns (``api._device_dict``'s ``asdict(record)`` plus
    ``lock_kind``/``lock_pid``/``endpoint``/``peer_reachable``) -- every
    field a real response carries, with test-friendly defaults an
    individual test overrides.

    ``host``/``remote_lock_kind``/``remote_lock_display``/``endpoint``
    default to ``None`` and ``peer_reachable`` defaults to ``True`` --
    sprint 003's (ticket 010) peer-owned-row fields, at the same
    "unset/local" defaults ``_device_dict`` folds in for a ``host IS
    NULL`` row, so every pre-sprint-003 test above (that never overrides
    them) keeps exercising exactly the local-row code path.
    """
    base = {
        "uid": "9900000011112222aaaaaaaa77778888",
        "short_uid": "aaaaaaaa",
        "port": "/dev/ttyACM0",
        "vid_pid": "0d28:0204",
        "role": None,
        "common_name": None,
        "device_name": None,
        "serial_payload": None,
        "raw_announcement": None,
        "state": STATE_ATTACHED_UNPROBED,
        "error_note": None,
        "flash_count": 0,
        "first_seen": 0.0,
        "last_seen": 0.0,
        "lock_kind": None,
        "lock_pid": None,
        "host": None,
        "remote_lock_kind": None,
        "remote_lock_display": None,
        "endpoint": None,
        "peer_reachable": True,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# STATE cell -- every value, via render_table
# ---------------------------------------------------------------------------


def test_free_unprobed_device_is_free_state_and_not_probed_yet_firmware():
    d = _device(short_uid="aaaaaaaa", state=STATE_ATTACHED_UNPROBED)
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "free" in data_line
    assert "(not probed yet)" in data_line


def test_connected_device_with_role_and_common_name_is_free_and_shows_role_slash_name():
    d = _device(
        short_uid="bbbbbbbb",
        state=STATE_CONNECTED,
        role="NEZHA2",
        common_name="robot",
        device_name="vevov",
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "free" in data_line
    assert "NEZHA2/robot" in data_line
    assert "vevov" in data_line


def test_locked_device_shows_locked_by_kind_pid_regardless_of_underlying_state():
    d = _device(
        short_uid="cccccccc",
        state=STATE_CONNECTED,
        role="RADIOBRIDGE",
        common_name="relay",
        lock_kind="serial",
        lock_pid=4821,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "locked by serial pid 4821" in data_line


def test_locked_device_takes_precedence_over_no_firmware_state():
    # A device can be locked (e.g. mid-flash) while its last-known state
    # is still "no firmware" -- the lock is the more useful thing to show
    # in the STATE column, per _state_cell's own docstring.
    d = _device(
        short_uid="dddddddd",
        state=STATE_CONNECTED_NO_FIRMWARE,
        lock_kind="flash",
        lock_pid=99,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "locked by flash pid 99" in data_line
    assert "no-firmware" not in data_line


def test_connected_no_firmware_device_is_no_firmware_state_and_firmware_cell():
    d = _device(short_uid="eeeeeeee", state=STATE_CONNECTED_NO_FIRMWARE)
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "no-firmware" in data_line
    assert "no firmware" in data_line


def test_disconnected_device_is_gone_state():
    d = _device(short_uid="ffffffff", state=STATE_DISCONNECTED)
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "gone" in data_line


# ---------------------------------------------------------------------------
# render_table -- headers, columns, empty list, error notes, sort order
# ---------------------------------------------------------------------------


def test_render_table_header_row_has_all_five_columns():
    out = render_table([_device()])
    header = out.splitlines()[0]
    for col in TABLE_HEADERS:
        assert col in header


def test_render_table_uses_port_and_dash_when_absent():
    with_port = _device(short_uid="11111111", port="/dev/ttyACM7")
    without_port = _device(short_uid="22222222", port=None)
    out = render_table([with_port, without_port])
    data_lines = out.splitlines()[2:4]
    assert data_lines[0].rstrip().endswith("/dev/ttyACM7")
    assert data_lines[1].rstrip().endswith("-")


def test_render_table_falls_back_to_last_eight_of_uid_when_short_uid_missing():
    d = _device(short_uid=None, uid="00112233445566778899aabbccddeeff0011223")
    out = render_table([d])
    assert d["uid"][-8:] in out


def test_render_table_empty_list_reports_no_devices_known():
    assert render_table([]) == "no devices known to the registry"


def test_render_table_appends_error_note_line_for_device_that_has_one():
    d = _device(
        short_uid="99999999",
        device_name="getez",
        state=STATE_CONNECTED_NO_FIRMWARE,
        error_note="no announcement received during probe",
    )
    out = render_table([d])
    assert "getez: no announcement received during probe" in out


def test_render_table_sorts_by_short_uid_then_uid():
    d_b = _device(short_uid="bbbbbbbb", device_name="second")
    d_a = _device(short_uid="aaaaaaaa", device_name="first")
    out = render_table([d_b, d_a])
    assert out.index("first") < out.index("second")


# ---------------------------------------------------------------------------
# render_json -- structure and sort order
# ---------------------------------------------------------------------------


def test_render_json_wraps_devices_key_verbatim():
    d = _device(short_uid="aaaaaaaa")
    payload = render_json([d])
    assert payload == {"devices": [d]}


def test_render_json_empty_list_is_empty_devices_list():
    assert render_json([]) == {"devices": []}


def test_render_json_sort_order_matches_render_table():
    d_b = _device(short_uid="bbbbbbbb", uid="bb")
    d_a = _device(short_uid="aaaaaaaa", uid="aa")
    payload = render_json([d_b, d_a])
    assert [d["short_uid"] for d in payload["devices"]] == ["aaaaaaaa", "bbbbbbbb"]


# ---------------------------------------------------------------------------
# HOST column, remote lock display, peer-unreachable rendering (ticket 010)
# ---------------------------------------------------------------------------


def test_render_table_header_row_has_host_column():
    out = render_table([_device()])
    header = out.splitlines()[0]
    assert "HOST" in header


def test_local_row_host_cell_is_local():
    d = _device(short_uid="aaaaaaaa", host=None, port="/dev/ttyACM7")
    out = render_table([d])
    data_line = out.splitlines()[2]
    # HOST sits just before PORT (TABLE_HEADERS) -- PORT stays the last
    # column, so a known PORT value anchors where HOST's cell ends.
    assert "local  /dev/ttyACM7" in data_line.rstrip()


def test_remote_row_reachable_shows_hostname_and_cached_lock_display():
    d = _device(
        short_uid="bbbbbbbb",
        device_name="vevov",
        state=STATE_CONNECTED,
        host="loki",
        peer_reachable=True,
        remote_lock_kind="serial",
        remote_lock_display="pid 4821",
        port="/dev/ttyACM7",
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "loki  /dev/ttyACM7" in data_line.rstrip()
    assert "locked by serial pid 4821" in data_line
    # A remote row's cached lock display must never fall back to the
    # local-only lock_kind/lock_pid fields (which are always None for a
    # peer-owned row -- see api._device_dict).
    assert "None" not in data_line


def test_remote_row_reachable_and_unlocked_is_free():
    d = _device(
        short_uid="cccccccc",
        state=STATE_CONNECTED,
        host="loki",
        peer_reachable=True,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "free" in data_line
    assert "loki" in data_line


def test_remote_row_unreachable_renders_peer_unreachable_regardless_of_cache():
    # Per the ticket's own Testing note: "renders 'peer unreachable'
    # regardless of its last-known state/lock cache" -- a stale
    # "connected" state and a stale cached lock must not leak through.
    d = _device(
        short_uid="dddddddd",
        state=STATE_CONNECTED,
        host="loki",
        peer_reachable=False,
        remote_lock_kind="flash",
        remote_lock_display="pid 42",
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "peer unreachable" in data_line
    assert "flash" not in data_line
    assert "loki" in data_line  # HOST cell still shows the owning peer


def test_remote_row_unreachable_wins_over_disconnected_state():
    d = _device(
        short_uid="eeeeeeee",
        state=STATE_DISCONNECTED,
        host="loki",
        peer_reachable=False,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "peer unreachable" in data_line
    assert "gone" not in data_line
