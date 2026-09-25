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

import time
from typing import Any

from mbtools.registry.locks import format_lock_suffix
from mbtools.registry.store import (
    STATE_ATTACHED_NO_ANNOUNCE,
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
#: list``, plus sprint 003's HOST column (ticket 010) inserted just
#: before PORT -- PORT stays the last column deliberately (rather than
#: appending HOST at the very end), since an existing pre-sprint-003
#: test (``tests/registry/render/test_render.py``'s own "uses port and
#: dash when absent") asserts a row *ends with* its PORT cell; keeping
#: PORT last is what makes that assertion -- and any other caller making
#: the same "PORT is the last column" assumption -- still true.
TABLE_HEADERS = ["STATE", "NAME", "UID", "FIRMWARE", "HOST", "PORT"]


def _sort_devices(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable display order shared by both :func:`render_table` and
    :func:`render_json` -- short UID when known, else the full UID.
    Sorting here (rather than leaving it to each caller) is what makes
    "shared verbatim" true for order, not just cell formatting: a caller
    that fetches the same devices from ``registry.client.list()`` gets
    the same order out of either render function.
    """
    return sorted(devices, key=lambda d: d.get("short_uid") or d["uid"])


def _state_cell(device: dict[str, Any], now: float) -> str:
    """UC-004's STATE column: ``free`` / ``locked by <kind> pid <n>`` /
    ``no-answer`` / ``no-firmware`` / ``gone`` / ``peer unreachable``.
    Lock status (folded into the ``list`` response by
    ``api._device_dict``) takes precedence over either no-firmware state
    -- a device can be locked (e.g. mid-flash) while its last-known state
    is still one of those, and the lock is the more useful thing to show.

    Sprint 008 (ticket 002): a local row's lock cell appends the held
    lock's label/since text (``format_lock_suffix``, e.g. ``"locked by
    serial pid 4821 (alice-laptop, 12m)"``) when ``lock_since`` is set --
    ``now`` (threaded down from :func:`render_table`, never read
    internally, keeping this module's "no I/O outside its own arguments"
    contract) is what the elapsed age is computed against. A peer-owned
    row needs no equivalent change here: its ``remote_lock_display``
    cell already carries the same text, baked in by
    ``registry.peering.publish_lock_event`` (Design Rationale Decision 2)
    before it ever reaches this store.

    Sprint 007 (ticket 001): ``no-answer`` (:data:`STATE_ATTACHED_NO_ANNOUNCE`)
    is a probe that got nothing back but doesn't know the board is blank;
    ``no-firmware`` (:data:`STATE_CONNECTED_NO_FIRMWARE`) is reserved for
    a case this store can actually assert is blank (e.g. a flash-triggered
    re-probe that still gets nothing after a mass erase). See
    ``registry.store``'s module-level constants for the full distinction.

    Sprint 003 (ticket 010): a peer-owned row (``device["host"]`` set)
    reads a different pair of fields for both checks, per sprint.md
    Decision 3 -- ``peer_reachable``/``remote_lock_kind``/
    ``remote_lock_display`` (a replicated display cache) instead of
    ``lock_kind``/``lock_pid`` (which would require a live cross-host
    call this module never makes). ``peer_reachable is False`` wins over
    every other check, including a cached ``disconnected`` state or lock
    display -- per the ticket's own Testing note, "regardless of its
    last-known state/lock cache": once the link to the owning host is
    down, nothing this row already knows is trustworthy enough to show
    instead.
    """
    host = device.get("host")
    if host is not None and not device.get("peer_reachable", True):
        return "peer unreachable"
    if device["state"] == STATE_DISCONNECTED:
        return "gone"
    if host is not None:
        lock_kind = device.get("remote_lock_kind")
        if lock_kind:
            return f"locked by {lock_kind} {device.get('remote_lock_display')}"
    else:
        lock_kind = device.get("lock_kind")
        if lock_kind:
            suffix = format_lock_suffix(
                device.get("lock_label"), device.get("lock_since"), now=now
            )
            return f"locked by {lock_kind} pid {device.get('lock_pid')}{suffix}"
    if device["state"] == STATE_ATTACHED_NO_ANNOUNCE:
        return "no-answer"
    if device["state"] == STATE_CONNECTED_NO_FIRMWARE:
        return "no-firmware"
    return "free"


def _host_cell(device: dict[str, Any]) -> str:
    """HOST column (ticket 010): ``"local"`` for a ``host IS NULL`` row
    (this registry's own device), the owning peer's hostname otherwise.
    ``"local"`` rather than a blank cell -- documented choice, per the
    ticket's "implementer's call" -- so the column always shows a
    resolvable value and an empty cell is never ambiguous with a missing
    column.
    """
    return device.get("host") or "local"


def _firmware_cell(device: dict[str, Any]) -> str:
    """The FIRMWARE/version column -- ``mbrelay``'s ``_firmware_cell``
    precedent (``server/src/mbrelay/cli.py`` around line 406) for
    distinguishing "unknown/never asked" from a real value, adapted to
    this store's fields: no dedicated firmware-version field exists here
    (see ``store.DeviceRecord``), so ``role``/``common_name`` from the
    device's own announcement stand in for it.

    Sprint 007 (ticket 001): ``unknown`` for a didn't-announce board
    (:data:`STATE_ATTACHED_NO_ANNOUNCE` -- a probe ran and got nothing,
    but the board plausibly has firmware we simply don't hear from);
    ``no firmware`` only for a board known to be blank
    (:data:`STATE_CONNECTED_NO_FIRMWARE`).
    """
    if device["state"] == STATE_ATTACHED_UNPROBED:
        return "(not probed yet)"
    if device["state"] == STATE_ATTACHED_NO_ANNOUNCE:
        return "unknown"
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


def render_table(devices: list[dict[str, Any]], *, now: float | None = None) -> str:
    """The STATE/NAME/UID/FIRMWARE/HOST/PORT table -- exactly one header
    line, one rule line, and one row per device, nothing else (sprint 007,
    ticket 001 / SUC-003: ``render_table``'s output never contains a line
    after the table, even for a device with a non-empty ``error_note`` --
    that detail is only available via :func:`render_json`'s structured
    form now, not appended as free-text prose here). Available to any
    caller (``mbdeploy list``, ticket 008) without going through the CLI.
    Callers print the return value with a single ``print()`` call; it
    carries no trailing newline of its own.

    The ``NAME`` cell falls back to the device's cached chip identity
    (``chip_identity_name``, sprint 007 ticket 001) when ``device_name``
    is blank -- a board that never announces over serial still has a
    name once its chip identity has been read over SWD (ticket 003).

    An empty ``devices`` list renders as the same "no devices known to
    the registry" line ``mbregistry list`` has always shown instead of an
    empty table.

    ``now`` (sprint 008, ticket 002), if given, is the timestamp a local
    row's lock-age suffix (see :func:`_state_cell`) is computed against
    -- a test passes a fixed value for a deterministic assertion.
    Defaults to :func:`time.time` (this function's only I/O, mirroring
    every other module's ``now_fn``-style convention rather than
    threading a clock through every caller that doesn't care about it).
    """
    if now is None:
        now = time.time()
    devices = _sort_devices(devices)

    if not devices:
        return "no devices known to the registry"

    rows = [
        [
            _state_cell(d, now),
            d.get("device_name") or d.get("chip_identity_name") or "-",
            d.get("short_uid") or d["uid"][-8:],
            _firmware_cell(d),
            _host_cell(d),
            d.get("port") or "-",
        ]
        for d in devices
    ]
    return _table(rows, TABLE_HEADERS)


def render_json(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``--json``-equivalent structured form -- the same
    ``{"devices": [...]}`` shape ``mbregistry list --json`` has always
    ``json.dumps``'d, with devices in the same sort order
    :func:`render_table` uses. Callers that want the text serialize it
    themselves (``json.dumps(render_json(devices), indent=2)``) -- this
    function stops at the structure, not the wire text, so a caller that
    wants the dict itself (e.g. to inspect it, not print it) doesn't pay
    for a round trip through a string.

    Each device dict carries its own ``error_note`` (and, sprint 007
    ticket 001, ``chip_identity_name``/``chip_identity_serial``) field
    verbatim -- the same detail :func:`render_table` used to print as a
    free-text line after the table now only reaches a caller through this
    structured field, per SUC-003's "``--json`` carries the same
    per-device detail as a structured field" requirement.
    """
    return {"devices": _sort_devices(devices)}
