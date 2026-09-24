"""mbtools.registry.flash — the registry's minimal pyOCD flash operation.

Per sprint.md's Architecture (module "flash") and ticket 007's own
Description, this is deliberately minimal: it wraps exactly one pyOCD
invocation and nothing else. No diagnostics, no retry-on-transient-error,
no mass-erase recovery, no blank-board reporting -- all of that "fancy
work" stays in ``mbdeploy`` (sprint 002), per brief §3.6's "if you don't
need to put flashing in MB Registry, don't."

The pyOCD invocation shape (invoking ``pyocd`` through the current
interpreter, ``[sys.executable, "-m", "pyocd"]``, rather than a bare PATH
lookup -- mbtools is typically installed via an isolated venv where the
console script isn't on PATH but the package is importable) and the
``intelhex`` pre-flight hex validation are ported from ``mbdeploy``'s
``flash.py``. Its failure-signature matching and retry/mass-erase logic
(``_TRANSIENT_SIGNATURES``/``_LOCKED_SIGNATURES`` and friends) are **not**
ported -- that recovery logic is ``mbdeploy``'s job, not the registry's
minimal op.

**Contract with locks and store** (per sprint.md's Design Rationale, "a
flash-kind lock's release is the re-probe trigger"): :meth:`FlashOp.flash_hex`
requires a ``flash``-kind lock already held on ``uid`` -- it does not take
the lock itself, and it does not release it either. The caller (eventually
``mbdeploy`` in sprint 002, or a direct API call in this sprint's tests)
takes the lock via ``locks``/the API first, and releases it after
:meth:`flash_hex` returns; that release is what ticket 006's daemon-core
hook turns into a re-probe. This module's own job ends at "flashed,
streamed the log, recorded the attempt".
"""

from __future__ import annotations

import functools
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Callable

import intelhex

from mbtools.registry.locks import KIND_FLASH, LockManager
from mbtools.registry.store import Store

__all__ = [
    "FlashOp",
    "FlashResult",
    "FlashLockNotHeldError",
    "HexValidationError",
    "DEFAULT_MCU",
    "DEFAULT_NO_PROGRESS_TIMEOUT_S",
    "check_device_permission",
    "looks_permission_denied",
    "run_streamed_with_watchdog",
]

#: The nRF52833 is the only target this registry op flashes today (both
#: robot and relay firmware run on it). Ported from ``mbdeploy``'s
#: ``devices.DEFAULT_MCU`` -- kept as a local constant rather than an
#: import since ``mbtools`` does not depend on the ``mbdeploy`` package.
DEFAULT_MCU = "nrf52833"

# Invoke pyocd through the running interpreter rather than as a bare PATH
# lookup -- mbtools is typically installed via an isolated venv, so pyocd
# (a declared dependency) is importable here but its console script may
# not be on PATH. Ported verbatim from mbdeploy's flash.py.
_PYOCD = [sys.executable, "-m", "pyocd"]

#: The injectable pyOCD-subprocess runner's shape: given the argv and a
#: log callback, run it, relaying each output line to ``log`` as it
#: arrives (not buffered until exit), and return the process's exit code.
#: :func:`_default_runner` is the real, production implementation;
#: every test in this ticket injects a fake instead (ticket's own
#: acceptance criterion: "no test ... shells out to a real pyocd binary").
Runner = Callable[[list[str], Callable[[str], None]], int]

# ---------------------------------------------------------------------------
# Ticket 009: pyOCD permission fail-fast + no-progress watchdog.
#
# The issue this ticket closes
# (``non-root-usb-access-and-pyocd-permission-hang.md``, sprint.md
# Decision 8): without USB permission, pyOCD's own ``ConnectHelper``
# (``core/helpers.py::get_all_connected_probes``) treats an inaccessible
# CMSIS-DAP device as simply not enumerated -- confirmed by reading that
# module in this project's own ``.venv`` -- so it prints
# "Waiting for a debug probe matching unique ID '<uid>' to be
# connected..." *once*, then loops silently (``sleep(0.01)``) forever: no
# further output, no error, no timeout. Two independent defenses, per
# Decision 8:
#
# 1. :func:`check_device_permission` -- a proactive ``os.access`` check on
#    the device's own path, run *before* pyocd is ever invoked. This is
#    the primary fix: it catches the common case (a known, existing,
#    permission-denied device node) in well under a second, with zero
#    subprocess spawned.
# 2. :func:`run_streamed_with_watchdog` -- the fallback for whatever (1)
#    doesn't catch (an unknown/not-yet-resolved ``port``, or a race): a
#    bounded *no-progress* timeout, not a total-runtime cap (a slow but
#    genuinely-progressing mass erase must not be cut off), plus an
#    early-exit the instant a line of pyocd's own output names a
#    permission/access problem -- no reason to wait out the full
#    no-progress window once pyocd has already said what's wrong.
#
# Both are defined once here (not duplicated in ``flashlogic.py``) and
# imported by that module exactly like :data:`DEFAULT_MCU` already is --
# see that constant's own comment for why ``flashlogic`` imports from
# this module rather than the other way around.
# ---------------------------------------------------------------------------

#: How long ``run_streamed_with_watchdog`` waits for a *new* line of pyocd
#: output before concluding it has hung and killing it. Deliberately a
#: no-progress (bounded-silence) timeout, not a total-runtime cap --
#: mass-erase recovery legitimately takes a while, and this only fires
#: once pyocd has actually gone quiet. 60s is chosen conservatively
#: longer than any silent stretch observed in this project's own
#: hardware acceptance logs (docs/acceptance/*.md) -- a real flash's
#: erase/program/verify phases each produce output well inside that
#: window. ASSUMPTION: revisit against ticket 011's real-hardware
#: acceptance run if a legitimately slow, silent pyocd phase ever
#: false-positives.
DEFAULT_NO_PROGRESS_TIMEOUT_S = 60.0

#: Substrings (already lower-cased for matching) drawn from pyOCD's own
#: source for a USB permission/access failure -- not guessed at. Grep
#: hits used to build this list (all in this project's ``.venv``'s
#: installed pyocd):
#: ``probe/pydapaccess/interface/pyusb_v2_backend.py`` /
#: ``pyusb_backend.py`` (both build ``"%s while trying to interrogate a
#: USB device ... This can probably be remedied with a udev rule."`` on
#: ``errno.EACCES``, where ``%s`` is ``str(usb.core.USBError)`` --
#: libusb1's own wording for that errno is
#: ``"[Errno 13] Access denied (insufficient permissions)"``) and
#: ``probe/picoprobe.py``/``probe/stlink/usb.py`` (same ``EACCES``
#: pattern). pyOCD's ``flash``/``erase``/``reset`` subcommands all default
#: to ``logging.WARNING`` (``subcommands/load_cmd.py``,
#: ``erase_cmd.py``, ``reset_cmd.py``), and these are logged via
#: ``LOG.warning``, so they reach a plain (non-verbose) invocation's
#: output by default -- this list is not gated on ``-v``.
_PERMISSION_SIGNATURES = (
    "access denied",
    "permission denied",
    "insufficient permissions",
    "udev rule",
    "errno 13",
)


def looks_permission_denied(output: str) -> bool:
    """True if ``output`` (a pyocd output line, or several joined by
    newlines) names a USB permission/access failure -- see
    :data:`_PERMISSION_SIGNATURES`'s own comment for where this wording
    comes from. Shared by :func:`run_streamed_with_watchdog` (per-line,
    for its early-exit) and ``flashlogic.flash_hex`` (against the whole
    accumulated output, to decide "immediately fatal, never retried").
    """
    lowered = output.lower()
    return any(sig in lowered for sig in _PERMISSION_SIGNATURES)


def check_device_permission(port: str | None) -> str | None:
    """Return a short, actionable error message if ``port`` is a device
    node that exists but this process cannot read/write, else ``None``.

    ``None`` is also returned when ``port`` is falsy (unknown -- nothing
    to check) or when it does not exist on disk: a genuinely-absent
    device is a different problem ("no such device", already reported
    elsewhere via the registry's ``find``/probe path) and this function's
    job is permission, not existence -- treating a missing path as a
    permission failure would misreport a disconnected/renumbering board
    as a udev-rule problem.

    Uses ``os.access(port, os.R_OK | os.W_OK)`` per the ticket's own
    Approach. This is necessarily a *check*, not a guarantee (a TOCTOU
    race against a replug is possible, same as any other pre-flight
    check in this codebase, e.g. :func:`_validate_hex`) -- the
    :func:`run_streamed_with_watchdog` early-exit is the fallback for
    exactly that gap.
    """
    if not port:
        return None
    if not os.path.exists(port):
        return None
    if os.access(port, os.R_OK | os.W_OK):
        return None
    return (
        f"permission denied opening {port} -- this user has no "
        "read/write access to the device. Install the udev rule "
        "(run 'mbregistry install-service' as root -- ticket 008) then "
        "start a new session (or replug the board) for the new group "
        "membership to take effect, or run this command with sudo."
    )


def run_streamed_with_watchdog(
    cmd: list[str],
    log: Callable[[str], None],
    no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
) -> tuple[int, str]:
    """Run ``cmd``, relaying its combined stdout/stderr to ``log`` line by
    line as it arrives, and return ``(exit_code, output_text)``.

    Shared by :func:`_default_runner` (this module's own minimal flash
    op, which discards ``output_text``) and ``flashlogic.flash_hex``'s
    ``_run_streamed`` (which needs ``output_text`` for its own
    transient/locked failure-signature matching) -- ticket 009's fix
    applies once, here, rather than being duplicated per caller (per the
    ticket's own "factor the subprocess-running helper" option).

    A background reader thread -- not a bare blocking
    ``for line in proc.stdout`` -- is what makes the no-progress watchdog
    possible: ``Popen.stdout``'s iterator has no read timeout of its own,
    so the only way to notice "no line arrived for N seconds" is to hand
    the blocking read to its own thread and poll a queue with a timeout
    from here. The reader thread is a daemon thread and puts ``None`` as
    its own EOF sentinel once ``proc.stdout`` is exhausted (a real
    ``Popen``'s pipe closes -- ending the blocking read -- the instant
    the child exits or is killed, so this thread reliably finishes and is
    joined below; it is never left running past this function's return).

    Two ways this returns *without* reaching pyocd's own exit:

    - **No-progress timeout**: no line arrives within
      ``no_progress_timeout`` seconds of the last one (or of start) --
      the process is killed and a clear message is logged before
      returning.
    - **Early permission-denied exit**: the instant any single line
      matches :func:`looks_permission_denied`, the process is killed
      immediately -- *not* after the full ``no_progress_timeout`` --
      since pyocd has already said what's wrong (the Approach's "don't
      wait out the no-progress timeout when pyocd has already told us
      what's wrong").

    Either way the return value's exit code is guaranteed non-zero (a
    caller must never mistake a killed-for-hanging pyocd invocation for
    success), even if the platform's own killed-process exit code were
    ever ``0`` for some reason this module hasn't observed.
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    assert proc.stdout is not None  # guaranteed by stdout=PIPE above

    output_queue: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for raw_line in proc.stdout:  # type: ignore[union-attr]
                output_queue.put(raw_line.rstrip("\n"))
        finally:
            output_queue.put(None)  # this thread's own EOF sentinel

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    lines: list[str] = []
    killed = False
    while True:
        try:
            item = output_queue.get(timeout=no_progress_timeout)
        except queue.Empty:
            log(
                f"Error: pyocd produced no output for {no_progress_timeout:g}s "
                "-- treating it as hung and killing it (see "
                "'mbregistry install-service' -- ticket 008 -- if this "
                "turns out to be a permission problem)."
            )
            proc.kill()
            killed = True
            break
        if item is None:  # reader thread's own EOF sentinel
            break
        lines.append(item)
        log(item)
        if looks_permission_denied(item):
            log(
                "Error: pyocd reported a permission/access error opening "
                "the device -- not retrying or waiting for it to time "
                "out. Install the udev rule ('mbregistry install-service' "
                "-- ticket 008), then start a new session (or replug the "
                "board), or run this command with sudo."
            )
            proc.kill()
            killed = True
            break

    reader.join(timeout=5.0)
    rc = proc.wait()
    if killed and rc == 0:
        rc = 1  # guarantee failure semantics regardless of platform quirk
    return rc, "\n".join(lines)


class HexValidationError(Exception):
    """Raised by :meth:`FlashOp.flash_hex` when ``hex_path`` fails to parse
    with ``intelhex``, before any pyocd subprocess is constructed or run.

    Covers a missing/unreadable file (``OSError``, e.g.
    ``FileNotFoundError``/``PermissionError``) and a malformed one
    (``intelhex.IntelHexError`` and its subclasses). Raised, not returned
    as a :class:`FlashResult`, so it can never be mistaken for a completed
    (attempted) flash -- ``store.increment_flash_count`` is deliberately
    *not* called on this path, per the ticket's ``flash_count`` semantics
    ("incremented on each *completed* flash", where completed means
    "pyocd ran", not merely "was requested").
    """

    def __init__(self, hex_path: str, message: str) -> None:
        self.hex_path = hex_path
        super().__init__(message)


class FlashLockNotHeldError(Exception):
    """Raised by :meth:`FlashOp.flash_hex` when ``uid`` has no ``flash``-kind
    lock held at call time.

    This module requires evidence of a held flash-kind lock (it does not
    acquire one itself -- see the module docstring's "Contract with locks
    and store") so it can never be the thing that bypasses the locking
    contract the re-probe hook depends on. Raised rather than returned as
    a :class:`FlashResult` for the same reason as
    :class:`HexValidationError`: this is a caller-contract violation, not
    an attempted-and-failed flash, so ``increment_flash_count`` must never
    fire for it.
    """

    def __init__(self, uid: str) -> None:
        self.uid = uid
        super().__init__(f"{uid}: no flash-kind lock held -- refusing to flash")


@dataclass(frozen=True)
class FlashResult:
    """The outcome of one completed (i.e. actually invoked) pyOCD flash.

    Only ever constructed *after* pyocd has been run (or the injected
    runner has raised while attempting to run it) -- a precondition
    failure (no flash-kind lock, an invalid hex file) never reaches this
    far; those raise :class:`FlashLockNotHeldError`/
    :class:`HexValidationError` instead, before ``store.increment_flash_count``
    is ever called.

    ``exit_code`` is the pyocd subprocess's real exit code on a normal
    (even if non-zero, i.e. failed) completion, or ``None`` if the
    injected runner itself raised before producing one. ``error`` is
    ``None`` on success, and a short human-readable message otherwise.
    """

    success: bool
    exit_code: int | None
    error: str | None = None


def _validate_hex(hex_path: str) -> None:
    """Parse ``hex_path`` with ``intelhex`` before any pyocd invocation.

    A pre-flight check only -- it never touches a board, only the file on
    disk. Raises :class:`HexValidationError` for a missing/unreadable or
    malformed file; returns ``None`` (no exception) for a file that parses
    cleanly. Ported from ``mbdeploy``'s ``flash._validate_hex``, adapted to
    raise rather than return an optional message (see
    :class:`HexValidationError`'s own docstring for why).
    """
    try:
        intelhex.IntelHex().loadhex(hex_path)
    except OSError as exc:
        raise HexValidationError(
            hex_path, f"cannot read hex file {hex_path!r}: {exc}"
        ) from exc
    except intelhex.IntelHexError as exc:
        raise HexValidationError(
            hex_path, f"invalid hex file {hex_path!r}: {exc}"
        ) from exc


def _default_runner(
    cmd: list[str],
    log: Callable[[str], None],
    *,
    no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
) -> int:
    """Run ``cmd`` through :func:`run_streamed_with_watchdog`, discarding
    its accumulated output text (this minimal op does no failure-signature
    matching, so there is nothing to match it against), and return its
    exit code.

    The real, production :data:`Runner` -- used whenever :class:`FlashOp`
    is constructed without an injected one. ``no_progress_timeout`` is
    keyword-only with a default specifically so this still satisfies
    :data:`Runner`'s two-positional-argument shape when called as
    ``self._runner(cmd, log)`` -- :meth:`FlashOp.__init__` binds a
    non-default timeout via ``functools.partial`` when one is given,
    rather than widening ``Runner`` itself.
    """
    exit_code, _output = run_streamed_with_watchdog(cmd, log, no_progress_timeout)
    return exit_code


class FlashOp:
    """The registry's one minimal flash operation: pyOCD flash by UID.

    ``locks`` and ``store`` are injected references -- per sprint.md's
    Architecture, ``flash`` "requires a flash-kind lock already held" (read
    via ``locks.status``) and "marks the device flash-pending in the store
    on completion" (via ``store.increment_flash_count``). Both are the
    same instances a caller (a test, or eventually ticket 008's API
    dispatching against ``Daemon.locks``/the daemon's ``store``) already
    holds -- this class never constructs its own, mirroring the
    "don't construct a second LockManager" contract ticket 006 established
    for ``Daemon``.

    ``runner`` is the injectable pyOCD-subprocess seam (see :data:`Runner`)
    -- defaults to :func:`_default_runner` in production; every test in
    this ticket injects a fake instead, so no test ever shells out to a
    real ``pyocd`` binary or touches a real probe.

    ``no_progress_timeout`` (ticket 009) is only wired into the
    *production* default runner -- an injected ``runner`` owns its own
    behavior entirely, same as every other test seam in this class.
    """

    def __init__(
        self,
        *,
        locks: LockManager,
        store: Store,
        runner: Runner | None = None,
        target_mcu: str = DEFAULT_MCU,
        no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
    ) -> None:
        self._locks = locks
        self._store = store
        self._runner = (
            runner
            if runner is not None
            else functools.partial(
                _default_runner, no_progress_timeout=no_progress_timeout
            )
        )
        self._target_mcu = target_mcu

    def flash_hex(self, uid: str, hex_path: str, log: Callable[[str], None]) -> FlashResult:
        """Flash ``hex_path`` to the board behind ``uid`` over SWD.

        1. Requires a ``flash``-kind lock already held on ``uid``
           (:meth:`~mbtools.registry.locks.LockManager.status`) -- raises
           :class:`FlashLockNotHeldError` otherwise, before touching the
           hex file or pyocd at all.
        2. Validates ``hex_path`` with ``intelhex`` (:func:`_validate_hex`)
           -- raises :class:`HexValidationError` for an unreadable or
           malformed file, before invoking pyocd.
        3. Checks the device's own port for USB permission (ticket 009,
           :func:`check_device_permission`) -- looked up via
           ``store.find(uid)`` (the same ``store`` this class already
           holds, per its own docstring's "never construct a second
           ...") rather than a new parameter, since this class already
           has everything it needs to resolve ``uid`` to a port. A
           failure here returns a failed :class:`FlashResult` *without*
           calling ``store.increment_flash_count`` -- exactly like
           :class:`HexValidationError`'s own reasoning ("pyocd never
           ran, so this isn't an attempted flash") -- but as a return
           rather than a raise: unlike :class:`HexValidationError`/
           :class:`FlashLockNotHeldError`, both of this class's two
           direct callers (``api.py``/``remote_api.py``) already handle
           a failed :class:`FlashResult` as their generic "flash didn't
           work" path, so a return needs no new exception type or a new
           ``except`` clause at either call site to stay safe (an
           uncaught new exception type reaching ``api.py``'s bare
           per-connection request loop, which catches only
           ``(ConnectionError, OSError)``, would crash that connection's
           handling and leak the flash-kind lock -- worse than the hang
           this ticket fixes).
        4. Runs ``pyocd flash -t <target_mcu> --uid <uid> <hex_path>``
           through the injected ``runner``, relaying every output line to
           ``log`` as it arrives (never buffered and dumped at the end --
           ``runner`` is directly responsible for that, and
           :func:`_default_runner` honors it against a real subprocess,
           with the same no-progress watchdog
           :func:`run_streamed_with_watchdog` gives ``flashlogic``).
        5. Either way -- pyocd ran to completion (any exit code) or the
           runner itself raised attempting to run it -- calls
           ``store.increment_flash_count(uid)`` exactly once: an
           *attempted* flash counts, per the store's ``flash_count``
           semantics ("incremented on each *completed* flash", where
           completed means "pyocd ran to completion", not "succeeded").
        6. Returns a :class:`FlashResult` distinguishing success
           (``exit_code == 0``) from failure (non-zero exit, or the
           runner having raised -- ``exit_code`` is ``None`` in that
           case, ``error`` carries the exception's message).

        This method never releases ``uid``'s lock and never waits for or
        triggers a re-probe -- see the module docstring's "Contract with
        locks and store".
        """
        holder = self._locks.status(uid)
        if holder is None or holder.kind != KIND_FLASH:
            raise FlashLockNotHeldError(uid)

        _validate_hex(hex_path)

        record = self._store.find(uid)
        port = record.port if record is not None else None
        perm_error = check_device_permission(port)
        if perm_error is not None:
            log(f"Error: {perm_error}")
            return FlashResult(success=False, exit_code=None, error=perm_error)

        cmd = [*_PYOCD, "flash", "-t", self._target_mcu, "--uid", uid, hex_path]
        try:
            exit_code = self._runner(cmd, log)
        except Exception as exc:  # the injected runner raised
            self._store.increment_flash_count(uid)
            return FlashResult(success=False, exit_code=None, error=str(exc))

        self._store.increment_flash_count(uid)
        if exit_code == 0:
            return FlashResult(success=True, exit_code=exit_code)
        return FlashResult(
            success=False,
            exit_code=exit_code,
            error=f"pyocd flash failed (exit {exit_code})",
        )
