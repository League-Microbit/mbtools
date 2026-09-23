"""mbtools.registry.identity — announcement probe and both-dialect parser.

Per sprint.md's Architecture (module "identity"), this is the *only*
module in mbtools that opens a serial port to read a board's announcement
(:func:`probe`), and the *only* module that parses one (both dialects, via
:func:`probe`'s internal parser). No other module in mbtools re-parses
announcements independently (spec, cross-cutting §2) — that invariant is
enforced by convention (this being the one place it happens), not by code.

Ported from ``mbdeploy/src/mbdeploy/devices.py`` (``probe_type``,
``is_relay``) and ``microbit-radio-relay/server/src/mbrelay/inventory.py``
(``DeviceRecord.short_uid``) and ``.../mbrelay/cli.py`` (``_port_holder``).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

try:  # pyserial is a declared dependency, but keep this importable without
    # a real port available (mirrors devices.py's own optional import).
    import serial as _pyserial  # type: ignore
except Exception:  # pragma: no cover
    _pyserial = None  # type: ignore

from mbtools.common import DAPLINK_VID_PID

logger = logging.getLogger(__name__)

BAUD_RATE = 115200

#: Per-``readline()`` read timeout passed to the underlying ``Serial``
#: object — how long one read is allowed to block for before giving up
#: and letting :func:`probe`'s own deadline loop try again. Ported from
#: ``devices.py``'s ``probe_type``.
_READ_TIMEOUT_S = 0.12

#: Settle delay after ``open()`` and before writing ``HELLO`` — gives a
#: board that announces spontaneously on reset a moment to start sending
#: before the input buffer is cleared. Ported from ``probe_type``.
_SETTLE_DELAY_S = 0.3


@dataclass(frozen=True)
class ProbeResult:
    """Parsed identity from a board's announcement line.

    ``role``/``common_name``/``device_name``/``serial`` are blank when
    every line seen during the probe window failed to parse against
    either dialect — SUC-001's "malformed announcement" outcome (role/name
    left blank, raw line kept, not dropped). This is kept distinct from an
    outright timeout (no line arrived at all), which :func:`probe` reports
    as ``None`` instead of a blank :class:`ProbeResult`.
    """

    role: str
    common_name: str
    device_name: str
    serial: str
    raw: str


def is_micro_bit_port(vid: int | None, pid: int | None) -> bool:
    """Is a USB (vid, pid) pair a micro:bit's DAPLink debug/CDC interface?

    Lets a caller decide a device is worth probing from USB data alone,
    before ever opening a port — the same VID:PID filter
    ``mbtools.registry.usbwatch`` (ticket 003) applies on the enumeration
    side, sharing :data:`mbtools.common.DAPLINK_VID_PID` so the two checks
    can't drift apart.
    """
    return (vid, pid) == DAPLINK_VID_PID


def is_relay(role: str | None) -> bool:
    """True if an announcement's role names a radio relay/bridge.

    Matches both ``RADIORELAY`` and the firmware's actual ``RADIOBRIDGE``
    by looking for either token case-insensitively. Ported from
    ``mbdeploy``'s ``devices.is_relay``.
    """
    if not role:
        return False
    upper = role.upper()
    return "RELAY" in upper or "BRIDGE" in upper


def short_uid(uid: str) -> str:
    """A short but actually distinguishing slice of a DAPLink UID.

    DAPLink UIDs are 48 hex chars laid out as ``board(4) family(4)
    hic(8) unique(16) pad(8) hic(8)``. Both ends are shared by every board
    carrying the same interface chip, so a tail slice alone names every
    board on a bench identically — the unique field is the middle 16
    chars, and ``uid[16:24]`` is its first 8. Ported from ``mbrelay``'s
    ``DeviceRecord.short_uid``, including its fallback: a UID too short to
    have that middle field (malformed or truncated) falls back to its own
    last 8 characters rather than raising.
    """
    return uid[16:24] if len(uid) >= 32 else uid[-8:]


def port_holder(port: str) -> str:
    """Best-effort: which process(es) hold ``port`` open right now.

    Shells out to ``lsof -t <port>`` for holder PIDs, then ``ps -o
    command=`` for each one, so a "port busy" error can name the offending
    program instead of surfacing a bare ``OSError``. Ported from
    ``mbrelay``'s ``_port_holder``. Swallows every ``lsof``/``ps`` failure
    (missing binary, permission denied, timeout, ...) and falls back to
    the generic "another program" — this is a diagnostic nicety, never
    allowed to turn a probe failure into a crash of its own.
    """

    def run(*cmd: str) -> str:
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=3
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    holders = [
        f"{run('ps', '-o', 'command=', '-p', pid).strip()[:60] or '?'} (pid {pid})"
        for pid in run("lsof", "-t", port).split()[:3]
    ]
    return ", ".join(holders) or "another program"


def _parse_announcement(text: str) -> tuple[str, str, str, str] | None:
    """Parse one announcement line against both dialects.

    TWO announcement dialects, same five fields in the same order --
    sentinel, role, common_name, device_name, serial:

        relay  DEVICE:RADIOBRIDGE:relay:getez:1779042496
               (microbit-radio-relay/docs/announce.md)
        robot  device NEZHA2 robot vevov 1198504156
               (radio-robot-lib/docs/design/protocol.md S6)

    Only the colon dialect was accepted until 2026-08-27, so every ROBOT
    failed to type: ``probe_type`` returned ``None``, ``probe_all`` took
    its preserve-existing-fields branch, and role/common_name/device_name/
    serial were never written. The DEVICE NAME column still filled in,
    because board_name comes from the separate SWD path
    (read_device_id -> friendly_name), which masked the gap.

    Consequence in the field: a robot kept whatever stale role its
    registry entry last held. vevov, reflashed from relay to robot
    firmware, stayed labelled RADIOBRIDGE, and ``mbdeploy deploy vevov``
    refused with "relay is a relay" on a board that had been a robot for
    days.

    The robot dialect is space-delimited and lowercase because the v6
    wire protocol dropped ':' as a field separator when it retired v5
    (pxt-nezha-diffdrive dfca4f8, 2026-08-23); the announcement line went
    with it. ``probe_type`` was never updated to follow — this parser,
    and only this parser, is where that gets fixed and stays fixed: no
    other module in mbtools re-parses announcements.

    Returns ``None`` (not a raise) for anything that isn't one of the two
    known shapes — the caller treats that as "no match" for this line and
    keeps reading, per SUC-001's malformed-announcement error flow.
    """
    if text.startswith("DEVICE:"):
        parts = text.split(":")
        if len(parts) >= 5:
            # Serial may itself contain ':' -- rejoin the tail.
            return parts[1], parts[2], parts[3], ":".join(parts[4:])
        return None
    if text.startswith("device "):
        parts = text.split()
        if len(parts) >= 5:
            # Space-delimited: the serial is a single bare token (the
            # decimal FICR.DEVICEID[1]), so any extra trailing tokens are
            # not part of it and are ignored.
            return parts[1], parts[2], parts[3], parts[4]
        return None
    return None


def probe(
    port: str,
    timeout_s: float = 1.6,
    *,
    serial_factory: Callable[..., Any] | None = None,
    settle_s: float | None = None,
) -> ProbeResult | None:
    """Open ``port``, send ``HELLO``, and parse the announcement.

    Opens with ``dsrdtr=False, rtscts=False`` and DTR/RTS held low (per
    brief §3.2 step 4 / spec cross-cutting §4), waits :data:`_SETTLE_DELAY_S`
    for a spontaneous announcement, resets the input buffer, writes
    ``HELLO\\n``, then reads lines until one parses against either
    dialect or ``timeout_s`` elapses. The port is always closed before
    returning.

    Returns:
    - A :class:`ProbeResult` with all fields populated, on a matching
      line.
    - A :class:`ProbeResult` with ``role``/``common_name``/
      ``device_name``/``serial`` blank and ``raw`` set to the last
      unparseable line seen, if at least one line arrived but none of
      them matched either dialect (a malformed announcement — SUC-001's
      error flow keeps this distinct from a plain timeout).
    - ``None`` if nothing arrived at all before ``timeout_s`` elapsed, or
      if the port could not be opened (busy or otherwise) — never an
      exception. On an open failure, a best-effort diagnostic naming the
      holding process (see :func:`port_holder`) is logged at ``WARNING``.

    ``serial_factory`` and ``settle_s`` are test-only escape hatches: a
    test passes ``serial_factory=`` a callable that returns an
    ``mbtools.testing.fakes.FakeSerial`` instance, and ``settle_s=0`` to
    skip the real-hardware settle delay a fake port doesn't need.
    Production code leaves both at their defaults (``serial.Serial`` and
    :data:`_SETTLE_DELAY_S`).
    """
    factory = serial_factory
    if factory is None:
        if _pyserial is None:  # pragma: no cover - pyserial is a dependency
            return None
        factory = _pyserial.Serial
    settle = _SETTLE_DELAY_S if settle_s is None else settle_s

    ser = None
    try:
        ser = factory(
            baudrate=BAUD_RATE, timeout=_READ_TIMEOUT_S, dsrdtr=False, rtscts=False
        )
        ser.port = port
        ser.dtr = False
        ser.rts = False
        ser.open()
    except Exception as exc:
        holder = port_holder(port)
        logger.warning(
            "identity.probe: %s did not open (held by %s): %s", port, holder, exc
        )
        return None

    try:
        if settle:
            time.sleep(settle)
        ser.reset_input_buffer()
        ser.write(b"HELLO\n")
        ser.flush()

        malformed_raw: str | None = None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            text = raw.decode("utf-8", "ignore").strip()
            if not text:
                continue
            fields = _parse_announcement(text)
            if fields is not None:
                role, common_name, device_name, serial_no = fields
                return ProbeResult(
                    role=role,
                    common_name=common_name,
                    device_name=device_name,
                    serial=serial_no,
                    raw=text,
                )
            malformed_raw = text
        if malformed_raw is not None:
            return ProbeResult(
                role="", common_name="", device_name="", serial="", raw=malformed_raw
            )
        return None
    except Exception:
        logger.exception("identity.probe: %s failed while reading", port)
        return None
    finally:
        if ser is not None and getattr(ser, "is_open", False):
            ser.close()
