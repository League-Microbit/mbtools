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

import re

from mbtools.registry.render import TABLE_HEADERS, render_json, render_table
from mbtools.registry.store import (
    STATE_ATTACHED_NO_ANNOUNCE,
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

    ``chip_identity_name``/``chip_identity_serial`` (sprint 007, ticket
    001) default to ``None`` -- the chip-identity cache is unset until
    ticket 003's SWD read populates it; a test exercising the ``NAME``
    fallback sets ``chip_identity_name`` directly, per this ticket's own
    Description ("write this ticket's tests against a store row you set
    the cache column on directly").
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
        "chip_identity_name": None,
        "chip_identity_serial": None,
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


def test_attached_no_announce_device_is_no_answer_state_and_unknown_firmware():
    """Sprint 007, ticket 001 (SUC-002): a board that was probed but gave
    no usable announcement is distinct from a known-blank board -- STATE
    reads ``no-answer`` and FIRMWARE reads ``unknown``, never
    ``no-firmware``/``no firmware``."""
    d = _device(short_uid="gggggggg", state=STATE_ATTACHED_NO_ANNOUNCE)
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "no-answer" in data_line
    assert "unknown" in data_line
    assert "no-firmware" not in data_line
    assert "no firmware" not in data_line


def test_locked_device_takes_precedence_over_no_announce_state():
    d = _device(
        short_uid="hhhhhhhh",
        state=STATE_ATTACHED_NO_ANNOUNCE,
        lock_kind="flash",
        lock_pid=99,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "locked by flash pid 99" in data_line
    assert "no-answer" not in data_line


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


def test_render_table_never_prints_a_line_after_the_table():
    """Sprint 007, ticket 001 (SUC-003): render_table's output never
    contains a line after the table, even for a device with a non-empty
    error_note -- exactly one header, one rule, one row per device."""
    devices = [
        _device(
            short_uid="99999999",
            device_name="getez",
            state=STATE_ATTACHED_NO_ANNOUNCE,
            error_note="no announcement received during probe",
        ),
        _device(short_uid="aaaaaaaa"),
    ]
    out = render_table(devices)
    assert "no announcement received during probe" not in out
    assert "getez" in out  # the row itself is still shown, just no trailer
    assert len(out.splitlines()) == 2 + len(devices)


def test_render_table_name_falls_back_to_chip_identity_when_device_name_blank():
    """Sprint 007, ticket 001: a board that never announces still shows
    its cached chip identity in NAME once ticket 003's SWD read has
    populated it -- exercised here directly against a device dict with
    the cache column set, per this ticket's own Description."""
    d = _device(
        short_uid="cafecafe",
        device_name=None,
        state=STATE_ATTACHED_NO_ANNOUNCE,
        chip_identity_name="tovez",
        chip_identity_serial=2314287040,
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "tovez" in data_line


def test_render_table_name_prefers_device_name_over_chip_identity():
    d = _device(
        short_uid="babebabe",
        device_name="vevov",
        state=STATE_CONNECTED,
        chip_identity_name="tovez",
    )
    out = render_table([d])
    data_line = out.splitlines()[2]
    assert "vevov" in data_line
    assert "tovez" not in data_line


def test_render_table_name_is_dash_when_neither_device_name_nor_chip_identity_set():
    d = _device(short_uid="deaddead", device_name=None, chip_identity_name=None)
    out = render_table([d])
    data_line = out.splitlines()[2]
    # Cells are joined with two spaces and individually contain at most
    # single internal spaces ("(not probed yet)", "locked by X pid N"),
    # so splitting on runs of 2+ spaces safely recovers each cell.
    cells = re.split(r"\s{2,}", data_line.rstrip())
    assert cells[1] == "-"  # NAME is TABLE_HEADERS[1]


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


def test_render_json_carries_error_note_as_structured_field_not_prose():
    """Sprint 007, ticket 001 (SUC-003): --json's structured field for a
    device with an error condition carries equivalent detail to what the
    old free-text table trailer line said -- as a plain field on the
    device dict, not a rendered string."""
    d = _device(
        short_uid="99999999",
        state=STATE_ATTACHED_NO_ANNOUNCE,
        error_note="no announcement received during probe",
    )
    payload = render_json([d])
    assert payload["devices"][0]["error_note"] == "no announcement received during probe"


def test_render_json_distinguishes_no_announce_from_known_blank_state():
    no_announce = _device(short_uid="11111111", state=STATE_ATTACHED_NO_ANNOUNCE)
    known_blank = _device(short_uid="22222222", state=STATE_CONNECTED_NO_FIRMWARE)
    payload = render_json([no_announce, known_blank])
    states = {d["short_uid"]: d["state"] for d in payload["devices"]}
    assert states["11111111"] == STATE_ATTACHED_NO_ANNOUNCE
    assert states["22222222"] == STATE_CONNECTED_NO_FIRMWARE
    assert states["11111111"] != states["22222222"]


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
