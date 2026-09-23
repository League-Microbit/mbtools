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

import subprocess
import sys
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


def _default_runner(cmd: list[str], log: Callable[[str], None]) -> int:
    """Run ``cmd``, relaying its combined stdout/stderr to ``log`` line by
    line as it arrives, and return its exit code.

    The real, production :data:`Runner` -- used whenever
    :class:`FlashOp` is constructed without an injected one. Uses
    ``subprocess.Popen`` rather than a single blocking ``subprocess.run()``
    specifically so pyocd's own progress output (erase/program/verify
    lines) reaches ``log`` throughout the run, not only once at exit --
    the same reasoning as mbdeploy's ``flash._run_streamed``, minus that
    function's side buffer of accumulated output (this minimal op does no
    failure-signature matching, so there is nothing to match the buffer
    against).
    """
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    assert proc.stdout is not None  # guaranteed by stdout=PIPE above
    for line in proc.stdout:
        log(line.rstrip("\n"))
    return proc.wait()


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
    """

    def __init__(
        self,
        *,
        locks: LockManager,
        store: Store,
        runner: Runner | None = None,
        target_mcu: str = DEFAULT_MCU,
    ) -> None:
        self._locks = locks
        self._store = store
        self._runner = runner if runner is not None else _default_runner
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
        3. Runs ``pyocd flash -t <target_mcu> --uid <uid> <hex_path>``
           through the injected ``runner``, relaying every output line to
           ``log`` as it arrives (never buffered and dumped at the end --
           ``runner`` is directly responsible for that, and
           :func:`_default_runner` honors it against a real subprocess).
        4. Either way -- pyocd ran to completion (any exit code) or the
           runner itself raised attempting to run it -- calls
           ``store.increment_flash_count(uid)`` exactly once: an
           *attempted* flash counts, per the store's ``flash_count``
           semantics ("incremented on each *completed* flash", where
           completed means "pyocd ran to completion", not "succeeded").
        5. Returns a :class:`FlashResult` distinguishing success
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
