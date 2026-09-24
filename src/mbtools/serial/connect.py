"""mbtools.serial.connect -- session logic for ``mbserial`` (ticket 009).

Per sprint.md's Architecture (module ``serial.connect``) and SUC-004: give
a caller (the CLI, or a library) a raw, not-rebooted serial connection to
a locally-attached board. This is the *only* place ``mbserial`` opens a
port -- resolve ``<name>`` via ``registry.client.find()``, lock it
(``kind=serial``, fail fast on ``locked`` per SUC-005 -- no retry, no
blocking wait, same as ticket 007's flash lock), open the port directly
with DTR/RTS held low by default (no reboot), or, if ``reset=True`` was
requested, deliberately reset the board as part of connect. Returns a
:class:`Session` usable by both ``serial.cli`` and a library caller.

**Why "open the port directly" and not always through a registry TCP
stream**: sprint.md's own Decision ("mbserial's local transport opens the
port directly, not always through a registry TCP stream") -- spec §5.2
leaves this open, and always-TCP would require designing the registry's
still-undesigned remote control channel (reset/BREAK/DTR) now, guessing
at sprint 003's protocol. Opening the local port directly needs nothing
new from the registry beyond the lock it already grants.

**Reset semantics -- why a platform branch, not one BREAK-always
approach**: spec cross-cutting §4 / docs/brief.md §8: on Linux, closing
and reopening a port does *not* reset a DAPLink target -- only a serial
BREAK does. On macOS, a plain reopen *does* reset it. Today's
``mbdeploy`` never resets on connect at all (its ``console.py``'s
``open_port`` always holds DTR/RTS low), so this platform branch is new
code here, not a port -- ``_reset_via_break``/``_reset_via_reopen`` below.
``_current_platform`` is a one-line wrapper around ``platform.system()``
so a test can inject either branch regardless of which OS the suite
actually runs on (sprint.md's Testing note: "the other platform's branch
covered via an injectable 'which platform' seam rather than skipped
outright").

**Ported unchanged from mbdeploy's ``console.py``**: :func:`send_command`
and :func:`interact` (and the DTR/RTS-low half of ``open_port``, as
:func:`open_no_reboot`) -- both are already duck-typed against a
serial-like object per that module's own docstring, so zero lines of
their own logic changed crossing into this module.

**Shared with ``registry.remote_api`` (sprint 003, ticket 007)**:
:func:`open_no_reboot` is a public, module-level function (not a
``Session``/``connect()``-internal helper) precisely so
``registry.remote_api``'s ``stream`` op can open the *server's own*
local port the exact same "DTR/RTS held low, no reboot" way this
module's local ``connect()`` does, rather than a second, drifting copy
of the same four pyserial calls -- ticket 007's own acceptance criterion
("reuse that helper rather than duplicating").

**Session shape**: :class:`Session` forwards the same six members
mbdeploy's ``remote.SocketSerial`` adapter exposes
(``reset_input_buffer``/``write``/``flush``/``readline``/``read``/
``in_waiting`` -- spec §5.1's own reference shape, "the SocketSerial idea
from mbdeploy's remote.py") so a library caller can use it directly
wherever a serial-like object is expected, even though this ticket's own
transport is a direct local port, not a socket. :meth:`Session.close` is
idempotent and is the one place the ``serial``-kind lock is released --
called on every exit path (normal Ctrl-D, Ctrl-C already handled inside
:func:`interact`, or an explicit library-caller close) per SUC-004's own
postcondition.
"""

from __future__ import annotations

import platform
import sys
import threading
import time
from typing import Any, Callable

from mbtools.registry.client import RegistryClient, RegistryClientError

try:  # pyserial is a declared dependency, but keep this importable without
    # a real port available (mirrors identity.py's/devices.py's own
    # optional import).
    import serial as _pyserial  # type: ignore
except Exception:  # pragma: no cover
    _pyserial = None  # type: ignore

__all__ = [
    "ConnectError",
    "Session",
    "connect",
    "interact",
    "send_command",
    "open_no_reboot",
    "BAUD_RATE",
    "DEFAULT_TIMEOUT",
    "OPEN_SETTLE",
    "IDLE_GAP",
    "READ_TIMEOUT",
    "BREAK_DURATION",
]

#: Default serial baud rate -- mirrors today's ``mbdeploy``'s own
#: ``cli.py`` ``_DEFAULT_BAUD``.
BAUD_RATE = 115200

#: Default one-shot reply timeout -- mirrors ``mbdeploy``'s own
#: ``cli.py`` ``_DEFAULT_CONNECT_TIMEOUT``.
DEFAULT_TIMEOUT = 2.0

#: Seconds to let a freshly opened port settle before writing to it --
#: ported unchanged from ``console.py``'s ``OPEN_SETTLE``.
OPEN_SETTLE = 0.3

#: How long the board may stay silent, after it has said something,
#: before its reply is treated as finished -- ported unchanged from
#: ``console.py``'s ``IDLE_GAP``.
IDLE_GAP = 0.4

#: Per-read block time -- ported unchanged from ``console.py``'s
#: ``READ_TIMEOUT``.
READ_TIMEOUT = 0.1

#: Grace period after stdin ends -- ported unchanged from ``console.py``'s
#: ``EOF_DRAIN``.
EOF_DRAIN = 0.4

#: How long to assert BREAK for on the Linux ``--reset`` path -- matches
#: ``microbit-radio-relay``'s own ``break_duration_ms`` default (its
#: ``RelayControl.hello`` uses a break for the same "kick a board out of
#: a stuck state" reason).
BREAK_DURATION = 0.4

#: Settle delay after a deliberate reset (BREAK or reopen) before the
#: session is handed back -- gives the board's boot banner a moment to
#: clear before the caller starts talking to it.
RESET_SETTLE = 0.3

#: Lock kind this module takes -- a plain wire-protocol string, not an
#: import from ``registry.locks`` (``deploy.cli``'s own "Literal
#: strings, not imports" convention -- see that module's docstring for
#: why: it keeps ``serial`` from ever importing ``locks``/``store``
#: directly, per sprint.md's component-diagram boundary note).
_LOCK_KIND_SERIAL = "serial"


class ConnectError(RuntimeError):
    """A serial session could not be established -- the port is busy,
    pyserial isn't installed, or a deliberate reset (BREAK/reopen)
    failed. Mirrors ``mbdeploy``'s ``console.ConsoleError`` (this is
    where ``open_port`` was ported to), renamed since this module also
    owns resolve/lock, not just the open step.
    """


def _current_platform() -> str:
    """``platform.system()`` -- ``"Linux"``/``"Darwin"``/``"Windows"``.

    A one-line wrapper, not a direct ``platform.system()`` call at each
    reset site, so a test can monkeypatch this one function (or pass
    :func:`connect`'s own ``platform_name=`` override) to exercise the
    branch for the platform the test isn't actually running on -- see
    the module docstring's "Reset semantics" note.
    """
    return platform.system()


def open_no_reboot(factory: Callable[..., Any], port: str, baud: int, settle: float) -> Any:
    """Open ``port`` at ``baud`` with DTR/RTS held low, and let it settle.

    Ported unchanged from ``mbdeploy``'s ``console.open_port``: DAPLink
    resets the target when DTR is asserted, so the modem lines are
    cleared *before* the port is opened -- connecting to a robot must not
    reboot it (spec cross-cutting §4 / SUC-004's own main flow).

    Public (not module-private) since ``registry.remote_api``'s ``stream``
    op (sprint 003, ticket 007) calls this exact function to open its own
    local port the same no-reboot way -- see the module docstring's
    "Shared with registry.remote_api" note.
    """
    ser = factory(baudrate=baud, timeout=READ_TIMEOUT, dsrdtr=False, rtscts=False)
    ser.port = port
    ser.dtr = False
    ser.rts = False
    try:
        ser.open()
    except Exception as exc:  # serial.SerialException and friends
        raise ConnectError(f"cannot open {port}: {exc}") from exc
    if settle:
        time.sleep(settle)
    return ser


def _reset_via_break(ser: Any, port: str, duration: float, settle: float) -> Any:
    """Linux ``--reset`` path: assert a serial BREAK on the already-open
    ``ser``.

    Closing and reopening a port does not reset a DAPLink target on
    Linux (spec cross-cutting §4, measured on Ubuntu 24.04 per
    ``microbit-radio-relay``'s own ``relay.py`` docstring) -- only a
    BREAK condition does, so this is new code, not a port: today's
    ``mbdeploy`` never resets on connect at all.
    """
    try:
        ser.send_break(duration)
    except Exception as exc:
        raise ConnectError(f"cannot send reset BREAK to {port}: {exc}") from exc
    if settle:
        time.sleep(settle)
    return ser


def _reset_via_reopen(
    ser: Any, factory: Callable[..., Any], port: str, baud: int, settle: float
) -> Any:
    """macOS ``--reset`` path: close ``ser`` and open a fresh connection
    to reset the target.

    A plain reopen *does* reset a DAPLink target on macOS (spec
    cross-cutting §4) -- but only if the reopen is allowed to toggle DTR
    the ordinary way, which is exactly what :func:`open_no_reboot`
    deliberately suppresses. So this reopens with pyserial's own
    defaults (no DTR/RTS preset before ``open()``), not via
    :func:`open_no_reboot`, then returns the line state to the held-low
    idle default once the reset has happened -- the reset is a one-time
    act, not the ongoing line state a caller's session should see.
    """
    try:
        ser.close()
    except Exception:
        pass  # already gone; the reopen below is what matters
    try:
        new_ser = factory(baudrate=baud, timeout=READ_TIMEOUT, dsrdtr=False, rtscts=False)
        new_ser.port = port
        new_ser.open()
    except Exception as exc:
        raise ConnectError(f"cannot reopen {port} to reset it: {exc}") from exc
    if settle:
        time.sleep(settle)
    new_ser.dtr = False
    new_ser.rts = False
    return new_ser


def send_command(
    ser: Any, message: str, timeout: float, idle_gap: float = IDLE_GAP
) -> list[str]:
    """Send ``message`` as one line and return the reply lines.

    Ported unchanged from ``mbdeploy``'s ``console.send_command``.
    ``timeout`` is the whole budget for the exchange: the board gets that
    long to say anything at all, and the read stops early once it has
    answered and then stayed quiet for ``idle_gap`` -- a board that
    streams continuously is therefore cut off at ``timeout`` rather than
    hanging the command.
    """
    ser.reset_input_buffer()
    ser.write(message.encode("utf-8") + b"\n")
    ser.flush()

    lines: list[str] = []
    deadline = time.time() + timeout
    quiet_after = deadline  # nothing heard yet: wait out the full budget
    while True:
        now = time.time()
        if now >= deadline or (lines and now >= quiet_after):
            return lines
        raw = ser.readline()
        if not raw:
            continue
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if text:
            lines.append(text)
        quiet_after = time.time() + idle_gap


def interact(ser: Any) -> int:
    """Relay stdin to ``ser`` and ``ser`` to stdout until EOF or Ctrl-C.

    Ported unchanged from ``mbdeploy``'s ``console.interact``. Returns
    the exit code for the session (always ``0`` -- ending the session is
    not a failure). Ctrl-C is caught here and simply stops the relay
    early (no EOF drain); it never propagates out of this function, so a
    caller (``serial.cli``, or a library caller's own ``try``) always
    sees a normal return, never an interrupted one -- the ``serial``-kind
    lock is released by :meth:`Session.close`, called by the caller after
    this returns.
    """
    stop = threading.Event()

    def _pump() -> None:
        while not stop.is_set():
            try:
                data = ser.read(max(1, ser.in_waiting))
            except Exception:
                break  # port went away; the main loop will notice
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()

    reader = threading.Thread(target=_pump, name="mbserial-read", daemon=True)
    reader.start()
    try:
        while True:
            line = sys.stdin.readline()
            if not line:  # Ctrl-D, or the end of a pipe
                break
            ser.write(line.encode("utf-8"))
            ser.flush()
        time.sleep(EOF_DRAIN)
    except KeyboardInterrupt:
        pass  # Ctrl-C means stop now -- don't linger
    finally:
        stop.set()
        reader.join(timeout=1.0)
    return 0


class Session:
    """A locked, open serial session on one locally-attached board.

    Forwards the same six members mbdeploy's ``remote.SocketSerial``
    adapter exposes (module docstring's "Session shape" note), so a
    library caller can pass a :class:`Session` anywhere a serial-like
    object is expected. :meth:`interact`/:meth:`send_command` are thin
    convenience wrappers over this module's own ported functions.

    :meth:`close` is idempotent and is the one place the ``serial``-kind
    lock is released -- call it on every exit path.
    """

    def __init__(self, client: RegistryClient, uid: str, name: str, ser: Any) -> None:
        self._client = client
        self.uid = uid
        self.name = name
        self.ser = ser
        self._closed = False

    # -- duck-typed pass-through (spec §5.1's SocketSerial reference shape) --

    def reset_input_buffer(self) -> None:
        self.ser.reset_input_buffer()

    def write(self, data: bytes) -> int:
        return self.ser.write(data)

    def flush(self) -> None:
        self.ser.flush()

    def readline(self) -> bytes:
        return self.ser.readline()

    def read(self, size: int = 1) -> bytes:
        return self.ser.read(size)

    @property
    def in_waiting(self) -> int:
        return self.ser.in_waiting

    # -- convenience wrappers over this module's own ported functions ----

    def interact(self) -> int:
        return interact(self.ser)

    def send_command(
        self, message: str, timeout: float = DEFAULT_TIMEOUT, idle_gap: float = IDLE_GAP
    ) -> list[str]:
        return send_command(self.ser, message, timeout, idle_gap)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the port and release the ``serial``-kind lock, exactly
        once, however the session ends (module docstring). Safe to call
        more than once -- a caller doesn't need to track whether it
        already did.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self.ser.close()
        except Exception:
            pass
        try:
            self._client.unlock(self.uid)
        except RegistryClientError:
            pass  # already unlocked, or the connection is going away anyway

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def connect(
    client: RegistryClient,
    target: str,
    *,
    reset: bool = False,
    baud: int = BAUD_RATE,
    serial_factory: Callable[..., Any] | None = None,
    settle_s: float | None = None,
    reset_settle_s: float | None = None,
    platform_name: str | None = None,
) -> Session:
    """Resolve ``target``, lock it (``kind=serial``), and open its port.

    SUC-004's main flow: resolve via ``client.find()``; lock via
    ``client.lock(uid, kind="serial")`` (``DeviceLockedError``/any other
    ``RegistryClientError`` propagates unchanged -- SUC-005's fail-fast,
    no retry, no blocking wait); open the port with DTR/RTS held low
    (no reboot) unless ``reset=True``, in which case the board is
    deliberately reset as part of connect (BREAK on Linux, reopen on
    macOS -- :func:`_reset_via_break`/:func:`_reset_via_reopen`).

    If the port itself cannot be opened or reset, the lock this call just
    took is released before :class:`ConnectError` propagates -- never
    leaked on an open failure, even though resolve/lock already
    succeeded.

    ``serial_factory``/``settle_s``/``reset_settle_s``/``platform_name``
    are test-only escape hatches (mirrors ``identity.probe``'s own
    ``serial_factory``/``settle_s`` seam): a test passes a callable
    returning ``mbtools.testing.fakes.FakeSerial``, ``settle_s=0`` to
    skip the real-hardware settle delay, and ``platform_name=`` to force
    either reset branch regardless of which OS the suite runs on.
    Production code leaves all four at their defaults.
    """
    factory = serial_factory
    if factory is None:
        if _pyserial is None:  # pragma: no cover - pyserial is a dependency
            raise ConnectError(
                "pyserial is not installed, so no serial port can be opened."
            )
        factory = _pyserial.Serial
    settle = OPEN_SETTLE if settle_s is None else settle_s
    reset_settle = RESET_SETTLE if reset_settle_s is None else reset_settle_s
    active_platform = _current_platform() if platform_name is None else platform_name

    # -- 1. resolve --------------------------------------------------------
    device = client.find(target)
    uid = device["uid"]
    name = device.get("device_name") or uid
    port = device.get("port")
    if not port:
        raise ConnectError(f"{name} has no known port to open")

    # -- 2. lock (kind=serial) -- fail fast, no retry, no blocking wait ---
    client.lock(uid, _LOCK_KIND_SERIAL)

    # -- 3. open, then reset if asked -- unlock on any failure here so the
    # lock this call just took is never leaked on an open/reset failure.
    try:
        ser = open_no_reboot(factory, port, baud, settle)
        if reset:
            if active_platform == "Linux":
                ser = _reset_via_break(ser, port, BREAK_DURATION, reset_settle)
            else:
                ser = _reset_via_reopen(ser, factory, port, baud, reset_settle)
    except Exception:
        try:
            client.unlock(uid)
        except RegistryClientError:
            pass
        raise

    return Session(client, uid, name, ser)
