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

**Sprint 007, ticket 003 — SWD chip-identity read.** This module is also
now the *only* place mbtools reads a board's identity over SWD
(:func:`read_device_id`/:func:`read_chip_identity`), alongside its
existing serial ``HELLO`` transport (:func:`probe`) — the same "one place
this kind of I/O happens" boundary sprint.md's Architecture (Step 3)
assigns this module, extended to a second transport. Ported from
``mbdeploy``'s ``read_device_id``/``friendly_name``/``read_board_name``
(``mbdeploy/src/mbdeploy/devices.py``): ``connect_mode="attach"``,
``auto_unlock=False``, ``blocking=False``, no halt, no reset -- reading
``FICR.DEVICEID[1]`` is safe on a board already running firmware, and
needs no cooperating serial port at all. ``pyocd`` is an optional import,
mirroring this module's existing optional ``serial`` import, so the
module stays importable in an environment (e.g. this project's own CI)
that never installs it. ``session_factory`` is the test-only escape
hatch (mirroring :func:`probe`'s own ``serial_factory``): a test passes a
callable matching ``ConnectHelper.session_with_chosen_probe``'s call
signature/return shape (a context manager yielding an object with
``.target.read32(addr)``) instead of ever touching a real probe.
"""

from __future__ import annotations

import contextlib
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

try:  # pyocd is a declared dependency, but keep this importable without it
    # installed (mirrors the _pyserial optional import immediately above) --
    # matters for any environment (this project's own CI included) that
    # never installs pyocd.
    from pyocd.core.helpers import ConnectHelper as _ConnectHelper  # type: ignore
except Exception:  # pragma: no cover - pyocd is a declared dependency
    _ConnectHelper = None  # type: ignore

from mbtools.common import DAPLINK_VID_PID

logger = logging.getLogger(__name__)

BAUD_RATE = 115200

#: nRF5x ``FICR.DEVICEID[1]`` -- the 32-bit word the micro:bit runtime
#: hashes into the board's five-letter friendly name. Same address on
#: both nRF51 (micro:bit V1) and nRF52 (V2). Ported from ``mbdeploy``'s
#: ``devices.FICR_DEVICEID1``.
FICR_DEVICEID1 = 0x10000064

#: Default ``target_override`` for :func:`read_device_id`, tried before
#: falling back to ``None`` (pyOCD auto-detect) -- ported from
#: ``mbdeploy``'s ``devices.DEFAULT_MCU``.
DEFAULT_TARGET_MCU = "nrf52833"

#: CODAL's friendly-name codebook: five base-5 digits, alternating
#: consonants and vowels, most-significant digit first in the printed
#: name. Ported verbatim from ``mbdeploy``'s ``devices._NAME_CODEBOOK``.
_NAME_CODEBOOK = (
    ("z", "v", "g", "p", "t"),
    ("u", "o", "i", "e", "a"),
    ("z", "v", "g", "p", "t"),
    ("u", "o", "i", "e", "a"),
    ("z", "v", "g", "p", "t"),
)

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


@contextlib.contextmanager
def _quiet_pyocd():
    """Silence pyOCD's own logging for the duration of a read, so it can't
    interleave with ``mbregistry``'s own log/print output. Ported verbatim
    from ``mbdeploy``'s ``devices._quiet_pyocd``.
    """
    pyocd_logger = logging.getLogger("pyocd")
    previous = pyocd_logger.level
    pyocd_logger.setLevel(logging.CRITICAL + 1)
    try:
        yield
    finally:
        pyocd_logger.setLevel(previous)


def friendly_name(device_id: int) -> str:
    """The micro:bit five-letter name CODAL encodes from
    ``FICR.DEVICEID[1]``.

    This is CODAL's ``microbit_friendly_name()``: the 32-bit word is
    written out as five base-5 digits, and digit *i* (counting from the
    least significant) selects a letter from column *i* of
    :data:`_NAME_CODEBOOK`, landing at position ``4 - i`` of the name.
    Ported verbatim from ``mbdeploy``'s ``devices.friendly_name``
    (example: ``2314287040 -> "tovez"``).
    """
    n = device_id & 0xFFFFFFFF
    letters = [""] * 5
    for i in range(5):
        letters[4 - i] = _NAME_CODEBOOK[i][n % 5]
        n //= 5
    return "".join(letters)


def read_device_id(
    uid: str,
    target_mcu: str = DEFAULT_TARGET_MCU,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> int | None:
    """Read ``FICR.DEVICEID[1]`` from the board behind ``uid`` over SWD.

    The board's name is a property of the *target* nRF, not of the debug
    probe, so it can't be computed from ``uid`` alone -- but it can be
    read through the probe the UID names. Attaches without halting or
    resetting (``connect_mode="attach"``, ``auto_unlock=False``,
    ``blocking=False``), and needs no serial port and no cooperating
    firmware -- safe to call even while the board is silently running its
    own firmware. Ported from ``mbdeploy``'s ``devices.read_device_id``.

    ``target_mcu`` is tried first; on failure (part connect refused,
    wrong target guess), retried once with ``target_override=None``
    (pyOCD auto-detect) so a V1 micro:bit (nRF51, a different part from
    ``target_mcu``'s nRF52 default) is still readable without the caller
    having to know which generation of board it's talking to.

    Returns ``None`` if pyOCD is unavailable, the probe is busy (e.g. mid
    flash), or the target refuses the connection (locked part) on both
    attempts -- never raises.

    ``session_factory`` is a test-only escape hatch (mirroring
    :func:`probe`'s own ``serial_factory``): a callable matching
    ``ConnectHelper.session_with_chosen_probe``'s call signature (keyword
    arguments ``unique_id``, ``target_override``, ``connect_mode``,
    ``blocking``, ``auto_unlock``) and return shape (a context manager
    yielding an object with ``.target.read32(addr)``). Production code
    leaves it unset, using pyOCD's real ``ConnectHelper`` when installed.
    """
    factory = session_factory
    if factory is None:
        if _ConnectHelper is None:  # pragma: no cover - pyocd is a declared dependency
            return None
        factory = _ConnectHelper.session_with_chosen_probe

    for override in (target_mcu, None):
        try:
            with _quiet_pyocd():
                with factory(
                    unique_id=uid,
                    target_override=override,
                    connect_mode="attach",
                    blocking=False,
                    auto_unlock=False,
                ) as session:
                    return session.target.read32(FICR_DEVICEID1)
        except Exception:
            continue
    return None


def read_chip_identity(
    uid: str,
    target_mcu: str = DEFAULT_TARGET_MCU,
    *,
    session_factory: Callable[..., Any] | None = None,
) -> tuple[str, int] | None:
    """The board's ``(name, device_id)`` chip identity, read once over SWD.

    Convenience wrapper over :func:`read_device_id` + :func:`friendly_name`
    -- what :mod:`mbtools.registry.daemon` calls when a serial probe comes
    back with nothing usable and this uid has no chip identity cached yet
    (sprint.md SUC-001). ``None`` when the device id couldn't be read (see
    :func:`read_device_id`'s own docstring for why); never raises.
    """
    device_id = read_device_id(uid, target_mcu, session_factory=session_factory)
    if device_id is None:
        return None
    return friendly_name(device_id), device_id


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
    reset_first: bool = False,
) -> ProbeResult | None:
    """Open ``port``, send ``HELLO``, and parse the announcement.

    Opens with ``dsrdtr=False, rtscts=False`` and DTR/RTS held low (per
    brief §3.2 step 4 / spec cross-cutting §4). If ``reset_first`` is
    true, asserts a serial ``BREAK`` for :data:`mbtools.serial.connect
    .BREAK_DURATION` seconds (the same duration ``serial.connect`` and
    ``relay.protocol`` already use for a BREAK-based reset) immediately
    after ``open()`` and before anything else -- forcing a board that is
    parked in its data plane (e.g. a relay mid-forward) back into its own
    command plane before ``HELLO`` is ever written, so a radio-forwarded
    fragment of another device's announcement can never be read as this
    board's own identity. ``reset_first`` defaults to ``False``, in which
    case this function's behavior, byte for byte, is unchanged from
    before this parameter existed -- no ``send_break`` call, same
    read-window/retry logic. Then waits :data:`_SETTLE_DELAY_S` for a
    spontaneous announcement, resets the input buffer once, then writes
    ``HELLO\\n`` and reads lines for up to ``timeout_s`` (one "read
    window"), returning as soon as a line parses against either dialect.

    If that first window ends with nothing usable -- silence, or only a
    line that doesn't parse against either dialect -- ``HELLO`` is sent
    exactly one more time and a second, equally bounded window is read
    before giving up. This is the same "exactly one retry" house style
    used elsewhere in this project (mbdeploy's transient-probe retry),
    applied here per sprint 002's Design Rationale to mitigate a real,
    not-root-caused timing sensitivity a hardware acceptance pass
    surfaced (docs/acceptance/001-hardware.md's magni finding): a board
    that stays silent or sends something unparseable in the first window
    sometimes answers a second ``HELLO``. A board that answers within the
    first window is completely untouched by this -- no second ``HELLO``
    is ever sent, and its probe timing is unchanged. The retry never
    loops more than once, win or lose.

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

    **``TIOCEXCL`` (sprint 007, ticket 003 wiring)**: once the port is
    open, this function best-effort applies
    :func:`mbtools.registry.claims.protect_fd` to the underlying file
    descriptor -- the belt-and-suspenders guard registry.claims's module
    docstring describes, asking the kernel to refuse any *other*
    ``open()`` of this same device node for as long as this probe holds
    it (a stray ``pyocd``/``screen``/``minicom`` a developer left open,
    say, underneath an already-claimed uid). This is purely defensive: it
    never gates whether the probe proceeds, and a fake/test port with no
    real ``fileno()`` (every test in this module) is silently skipped,
    not an error.
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
        # Best-effort TIOCEXCL -- see this function's own docstring. A
        # fake/test port (no real fileno(), every test in this module)
        # or any other failure is silently swallowed: this is a
        # defense-in-depth extra, never the mechanism the probe itself
        # relies on.
        from mbtools.registry import claims as _claims

        _claims.protect_fd(ser.fileno())
    except Exception:
        pass

    try:
        if reset_first:
            # Deferred import: mbtools.serial.connect imports
            # mbtools.registry.client -> ... -> mbtools.registry.store,
            # which imports *this* module at module scope (ProbeResult,
            # short_uid) -- a top-level import here would be circular.
            # Importing inside the function, at call time rather than at
            # import time, breaks the cycle; every module involved is
            # already fully loaded by the time any caller actually probes.
            from mbtools.serial.connect import BREAK_DURATION

            ser.send_break(BREAK_DURATION)
        if settle:
            time.sleep(settle)
        ser.reset_input_buffer()

        malformed_raw: str | None = None
        # Exactly one bounded retry: two attempts total, never open-ended.
        # A first-window success returns immediately from inside the loop
        # below, so a board that answers promptly never sees a second
        # HELLO -- this is what keeps the already-passing case untouched.
        for attempt in range(2):
            ser.write(b"HELLO\n")
            ser.flush()
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
            if attempt == 0:
                logger.debug(
                    "identity.probe: %s first window unusable, sending HELLO once more",
                    port,
                )
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
