"""mbtools.registry.render — shared device-list table/JSON rendering.

Per sprint.md's Architecture (module ``registry.render``) and spec §4.3
("`mbdeploy list` is a thin view over the registry; it can be shared with
`mbregistry list`"), this is the one place a list of device dicts (the
shape :meth:`mbtools.registry.client.RegistryClient.list` returns) turns
into either the STATE/NAME/UID/FIRMWARE/PORT table or the
``--json``-equivalent structured form. Pure presentation over
already-fetched data — no socket calls, no argparse, nothing that reaches
outside its own arguments.

Extracted from ``registry.cli``'s private ``_table``/``_state_cell``/
``_firmware_cell`` (sprint 001, ticket 009) rather than written fresh, so
``mbregistry list`` (refactored, ticket 002) and ``mbdeploy list`` (ticket
008, new) become this module's callers rather than two parallel
implementations of the same rendering (SUC-003: "no `mbdeploy`-specific
rendering code exists").
"""

from __future__ import annotations

from typing import Any

from mbtools.registry.store import (
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
)

__all__ = [
    "TABLE_HEADERS",
    "render_table",
    "render_json",
]

#: Column order for :func:`render_table` -- UC-004's STATE/NAME/UID/
#: FIRMWARE/PORT convention, unchanged from sprint 001's ``mbregistry
#: list``.
TABLE_HEADERS = ["STATE", "NAME", "UID", "FIRMWARE", "PORT"]


def _sort_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable display order shared by both :func:`render_table` and
    :func:`render_json` -- short UID when known, else the full UID.
    Sorting here (rather than leaving it to each caller) is what makes
    "shared verbatim" true for order, not just cell formatting: a caller
    that fetches the same devices from ``registry.client.list()`` gets
    the same order out of either render function.
    """
    return sorted(devices, key=lambda d: d.get("short_uid") or d["uid"])


def _state_cell(device: dict[str, Any]) -> str:
    """UC-004's STATE column: ``free`` / ``locked by <kind> pid <n>`` /
    ``no-firmware`` / ``gone``. Lock status (folded into the ``list``
    response by ``api._device_dict``) takes precedence over
    ``connected_no_firmware`` -- a device can be locked (e.g. mid-flash)
    while its last-known state is still "no firmware", and the lock is
    the more useful thing to show.
    """
    if device["state"] == STATE_DISCONNECTED:
        return "gone"
    lock_kind = device.get("lock_kind")
    if lock_kind:
        return f"locked by {lock_kind} pid {device.get('lock_pid')}"
    if device["state"] == STATE_CONNECTED_NO_FIRMWARE:
        return "no-firmware"
    return "free"


def _firmware_cell(device: dict[str, Any]) -> str:
    """The FIRMWARE/version column -- ``mbrelay``'s ``_firmware_cell``
    precedent (``server/src/mbrelay/cli.py`` around line 406) for
    distinguishing "unknown/never asked" from a real value, adapted to
    this store's fields: no dedicated firmware-version field exists here
    (see ``store.DeviceRecord``), so ``role``/``common_name`` from the
    device's own announcement stand in for it.
    """
    if device["state"] == STATE_ATTACHED_UNPROBED:
        return "(not probed yet)"
    if device["state"] == STATE_CONNECTED_NO_FIRMWARE:
        return "no firmware"
    role = device.get("role")
    common_name = device.get("common_name")
    if role and common_name:
        return f"{role}/{common_name}"
    return role or common_name or "-"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    """A minimal fixed-width table renderer -- ported from ``mbrelay``'s
    own ``_table`` (``server/src/mbrelay/cli.py``), the precedent this
    module's Description points at for the STATE/short-uid/FIRMWARE/port
    rendering convention.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    out = [header_line, "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append(
            "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(out)


def render_table(devices: list[dict[str, Any]]) -> str:
    """The STATE/NAME/UID/FIRMWARE/PORT table plus per-device error-note
    lines -- the same text ``mbregistry list`` has printed since sprint
    001, now available to any caller (``mbdeploy list``, ticket 008)
    without going through the CLI. Callers print the return value with a
    single ``print()`` call; it carries no trailing newline of its own,
    matching the byte-for-byte output the original inline
    ``print(_table(...))`` + one ``print()`` per error-note line produced.

    An empty ``devices`` list renders as the same "no devices known to
    the registry" line ``mbregistry list`` has always shown instead of an
    empty table.
    """
    devices = _sort_devices(devices)

    if not devices:
        return "no devices known to the registry"

    rows = [
        [
            _state_cell(d),
            d.get("device_name") or "-",
            d.get("short_uid") or d["uid"][-8:],
            _firmware_cell(d),
            d.get("port") or "-",
        ]
        for d in devices
    ]
    lines = [_table(rows, TABLE_HEADERS)]
    for d in devices:
        note = d.get("error_note")
        if note:
            label = d.get("device_name") or d.get("short_uid") or d["uid"]
            lines.append(f"  {label}: {note}")
    return "\n".join(lines)


def render_json(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``--json``-equivalent structured form -- the same
    ``{"devices": [...]}`` shape ``mbregistry list --json`` has always
    ``json.dumps``'d, with devices in the same sort order
    :func:`render_table` uses. Callers that want the text serialize it
    themselves (``json.dumps(render_json(devices), indent=2)``) -- this
    function stops at the structure, not the wire text, so a caller that
    wants the dict itself (e.g. to inspect it, not print it) doesn't pay
    for a round trip through a string.
    """
    return {"devices": _sort_devices(devices)}
