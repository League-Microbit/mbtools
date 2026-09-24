"""registry.flashlogic — pyOCD flash with transient retry and mass-erase recovery.

Per sprint 003's Architecture (Decision 4 / Step 3's module table), this is
the shared flash-with-recovery implementation moved here from
``mbtools.deploy.flash`` (itself ported near-verbatim from ``mbdeploy``'s
own ``src/mbdeploy/flash.py`` in sprint 002 -- see that sprint's ticket
005): the one place pyOCD's failure wording is matched against known
signatures, unchanged in shape from the existing, hardware-proven
implementation. ``docs/acceptance/001-hardware.md``'s hodr/braeburn
transient-probe-timeout retries and braeburn's ``0x67`` mass-erase-recovery
finding are exactly what this module handles.

This module takes no lock itself and knows nothing about the registry --
it flashes whatever UID it's given, streaming log lines to its caller. It
is called from two places: locally, by ``deploy.cli`` via the
``mbtools.deploy.flash`` re-export shim (unchanged since sprint 002); and
remotely, by ``registry.remote_api`` (ticket 009 in this sprint), which
invokes it directly after taking a ``flash``-kind lock over the wire.
Both callers get the exact same recovery behavior because both call this
one implementation -- this move changes only where the code lives, not
what it does.

Unlike ``mbtools.registry.flash`` (sprint 001's deliberately minimal
registry op -- one pyOCD invocation, no retry, no recovery, per that
module's own docstring), this module carries the "fancy work": a blind
retry on a transient-looking probe/communication failure, a CTRL-AP
mass erase (with one retry) on a locked/protected-device signature, and
an explicit, unmissable "no firmware" report when a post-erase reflash
still fails. ``DEFAULT_MCU`` is imported from ``mbtools.registry.flash``
rather than redefined here -- see that module's own docstring for why
the nRF52833 constant lives there.
"""

from __future__ import annotations

import sys
from typing import Callable

import intelhex

from mbtools.registry.universal_hex import UniversalHexError, normalized_hex
from mbtools.registry.flash import (
    DEFAULT_MCU,
    DEFAULT_NO_PROGRESS_TIMEOUT_S,
    check_device_permission,
    looks_permission_denied,
    run_streamed_with_watchdog,
)

__all__ = [
    "DEFAULT_MCU",
    "DEFAULT_NO_PROGRESS_TIMEOUT_S",
    "flash_hex",
]

# Invoke pyocd through the running interpreter rather than as a bare PATH
# lookup. mbtools is typically installed via an isolated venv, so pyocd (a
# declared dependency) is importable here but its console script is not
# necessarily on PATH. Mirrors sprint 001's registry.flash._PYOCD and
# today's mbdeploy.flash._PYOCD.
_PYOCD = [sys.executable, "-m", "pyocd"]


def _log(log: Callable[[str], None] | None, message: str) -> None:
    """Route a status/error line to ``log`` if given, else to stderr.

    ``log=None`` must never go silent -- callers rely on these lines
    landing on stderr exactly as before this function existed.
    """
    if log is None:
        print(message, file=sys.stderr)
    else:
        log(message)


def _validate_hex(hex_path: str) -> str | None:
    """Parse ``hex_path`` with ``intelhex`` before any pyocd invocation.

    This is a pre-flight check, not a flash attempt: it never touches a
    board, only the file on disk. Returns ``None`` if ``hex_path`` parses
    as a valid Intel HEX file, or a short, human-readable message
    otherwise -- covering a missing/unreadable file (``OSError``, e.g.
    ``FileNotFoundError``/``PermissionError``) and a malformed one
    (``intelhex.IntelHexError`` and its subclasses, e.g. a bad record or
    checksum) without leaking either exception's raw traceback.
    """
    try:
        intelhex.IntelHex().loadhex(hex_path)
    except OSError as exc:
        return f"cannot read hex file {hex_path!r}: {exc}"
    except intelhex.IntelHexError as exc:
        return f"invalid hex file {hex_path!r}: {exc}"
    return None


# ---------------------------------------------------------------------------
# Failure-signature matching
#
# These are the ONE named, documented place pyocd's failure wording is
# matched against: narrow substrings drawn from the concrete field reports
# behind this module's original (mbdeploy) implementation, and confirmed
# again against real hardware in this project's own
# docs/acceptance/001-hardware.md (hodr/braeburn transient timeouts;
# braeburn's 0x67 mass-erase-recovery finding). Anything that matches
# neither list is "not recoverable" -- flash_hex must never treat an
# unrecognized failure as a reason to mass-erase: an unnecessary mass
# erase destroys a working board's firmware, a missed recovery only costs
# one manual `pyocd erase --mass`.
# ---------------------------------------------------------------------------

#: A flaky USB/probe/communication problem, not a property of the board's
#: flash contents or protection state -- worth exactly one blind retry,
#: since the same flash often succeeds outright the second time.
_TRANSIENT_SIGNATURES = (
    "timeout reading from probe",
    "probe timeout",
    "communication failure",
    "communication fault",
    "transfer fault",
    "transfer error",
    "dapaccess",
)


def _looks_transient(output: str) -> bool:
    """True if ``output`` (pyocd's captured stdout/stderr) names a
    transient probe/communication problem worth one blind retry."""
    lowered = output.lower()
    return any(sig in lowered for sig in _TRANSIENT_SIGNATURES)


#: A locked/protected nRF (APPROTECT set, or a protected SoftDevice
#: region at 0x0) that rejects every flash-algorithm erase -- only a
#: CTRL-AP mass erase (ERASEALL) clears it. This is the *only* signature
#: that justifies a mass erase; anything that matches neither this list
#: nor ``_TRANSIENT_SIGNATURES`` above is deliberately treated as
#: unrecoverable rather than assumed locked -- an unrecognized failure
#: that was a real lock costs one manual ``pyocd erase --mass``; treating
#: an unrecognized failure as locked and erasing anyway can cost a
#: board's firmware.
_LOCKED_SIGNATURES = (
    "0x67",  # observed CMSIS-DAP fault code for a locked-device sector-erase failure
    "flash erase sector failure",
    "approtect",
    "access port protection",
    "authentication failed",
    "not authenticated",
    "device is locked",
    "target is locked",
)


def _looks_locked(output: str) -> bool:
    """True if ``output`` (pyocd's captured stdout/stderr) names a
    locked/protected-device signature recoverable only by a CTRL-AP mass
    erase."""
    lowered = output.lower()
    return any(sig in lowered for sig in _LOCKED_SIGNATURES)


def _run_streamed(
    cmd: list[str],
    log: Callable[[str], None] | None,
    no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
) -> tuple[int, str]:
    """Run ``cmd``, relaying its combined stdout/stderr through ``_log``
    line by line as it arrives, and return ``(exit_code, output_text)``.

    A thin, ``log``-may-be-``None``-safe wrapper over
    :func:`mbtools.registry.flash.run_streamed_with_watchdog` (ticket 009)
    -- that function requires a real callable (mirroring
    :data:`~mbtools.registry.flash.Runner`'s own contract), so this
    wrapper is what still lets every existing ``flash_hex`` caller here
    pass ``log=None`` and get the original stderr fallback via
    :func:`_log`, unchanged. The watchdog itself (no-progress kill, early
    permission-denied exit) is defined once in ``flash.py`` and not
    duplicated here -- see that module's own "Ticket 009" comment.

    ``output_text`` accumulates the exact same lines already relayed to
    ``log``/stderr (newline-joined), as a side buffer for signature
    matching (:func:`_looks_transient`/:func:`_looks_locked`/
    :func:`~mbtools.registry.flash.looks_permission_denied`) -- it does
    not change what is streamed or when, and it is not batching or
    deferring anything: every line still reaches ``log`` the instant it
    arrives.
    """
    return run_streamed_with_watchdog(
        cmd, lambda line: _log(log, line), no_progress_timeout
    )


def flash_hex(
    uid: str,
    hex_path: str,
    target_mcu: str = DEFAULT_MCU,
    log: Callable[[str], None] | None = None,
    board_name: str | None = None,
    port: str | None = None,
    no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
) -> int:
    """Flash ``hex_path`` to the board behind ``uid``; see :func:`_flash_hex`.

    A micro:bit Universal Hex (V1 + V2 images in one file, as the
    micro:bit Foundation ships its firmware) is first reduced to the image
    for ``target_mcu`` (:mod:`mbtools.registry.universal_hex`), since
    neither ``intelhex`` nor pyOCD understands its block records.
    """
    try:
        with normalized_hex(hex_path, target_mcu) as flash_path:
            if flash_path != hex_path:
                _log(log, f"{hex_path} is a Universal Hex; flashing its {target_mcu} image.")
            return _flash_hex(
                uid, flash_path, target_mcu, log, board_name, port, no_progress_timeout
            )
    except UniversalHexError as exc:
        _log(log, f"Error: {exc}")
        return 1


def _flash_hex(
    uid: str,
    hex_path: str,
    target_mcu: str = DEFAULT_MCU,
    log: Callable[[str], None] | None = None,
    board_name: str | None = None,
    port: str | None = None,
    no_progress_timeout: float = DEFAULT_NO_PROGRESS_TIMEOUT_S,
) -> int:
    """Flash ``hex_path`` to the board behind ``uid``, with mass-erase recovery.

    A failed first flash whose output looks transient triggers one blind
    retry; a failure that persists (or never looked transient) triggers a
    CTRL-AP mass erase and one retry only if its output looks *locked*;
    any other failure returns without ever mass-erasing. A mass-erase
    failure returns its own return code without retrying the flash. A
    still-failing flash after a successful mass erase returns its return
    code, with an explicit "no firmware" report. Success returns the
    ``reset`` return code.

    Each pyocd subprocess's output is streamed through ``log`` as it
    arrives (see :func:`_run_streamed`) rather than captured and
    discarded, so a caller-supplied ``log`` sees progress throughout
    each invocation, not just at the fixed transition messages below.

    Before any of that: ``hex_path`` is validated with ``intelhex``
    (:func:`_validate_hex`). A missing, unreadable, or malformed hex file
    fails here, with a clear message routed through ``log``, before any
    ``pyocd`` subprocess is constructed or run -- so an operator-side file
    problem never reaches the board at all.

    Also before any ``pyocd`` subprocess: if ``port`` is given, its USB
    permission is checked (ticket 009,
    :func:`~mbtools.registry.flash.check_device_permission`) and a
    denial fails immediately with a clear, actionable message -- the
    primary defense against the bug this ticket closes (without USB
    permission, pyOCD retries opening the probe indefinitely and never
    times out on its own). ``port`` is optional and skipped (``None``)
    when unknown -- callers that don't have it yet still get the
    fallback protection below. A first flash whose output names a
    permission/access problem (caught as it streams, by
    :func:`_run_streamed`'s own early-exit) is likewise never retried --
    see the transient-retry paragraph below for why that matters.

    A first flash failure whose output looks transient (a probe timeout,
    a communication/transfer fault, or a ``DAPAccess`` error --
    :func:`_looks_transient`) is retried exactly once, logged visibly,
    before anything else is decided -- most flaky-USB failures simply
    succeed the second time with no change to the board's state at all.
    A failure whose output instead looks like a permission/access
    problem (:func:`~mbtools.registry.flash.looks_permission_denied`) is
    checked *first*, ahead of the transient check, and returns
    immediately without ever reaching this retry -- a permission problem
    is a host-configuration fact, not a flaky-USB blip, and retrying it
    would just repeat (or, worse, re-hang on) the same failure.

    A failure that persists past that (or that never looked transient in
    the first place) is mass-erased and retried only if its output looks
    *locked* (:func:`_looks_locked`) -- a `0x67` sector-erase failure, or
    auth/lock/APPROTECT wording. Any other failure (an invalid hex that
    slipped past validation, a bad ``target_mcu``, or anything else this
    module doesn't recognize) fails immediately **without** erasing:
    an unrecognized signature is deliberately never treated as "assume
    locked," because an unnecessary mass erase destroys a working
    board's firmware while a missed recovery only costs the operator one
    manual ``pyocd erase --mass``.

    If the mass erase itself succeeds but the retried flash still fails,
    the board has no firmware at all -- the erase already wiped it and
    reflashing didn't take. That is reported explicitly and unmissably
    through ``log`` (not only local stderr), naming the board via
    ``board_name`` (falling back to ``uid`` when not given).
    """
    hex_error = _validate_hex(hex_path)
    if hex_error is not None:
        _log(log, f"Error: {hex_error}")
        return 1

    # Ticket 009's primary fix: a proactive permission check, before any
    # pyocd subprocess is constructed or run -- see this function's own
    # docstring's "Also before any pyocd subprocess" paragraph.
    perm_error = check_device_permission(port)
    if perm_error is not None:
        _log(log, f"Error: {perm_error}")
        return 1

    # --- flash (with mass-erase recovery for locked parts) ---
    flash_cmd = [
        *_PYOCD, "flash",
        "-t", target_mcu,
        "--uid", uid,
        hex_path,
    ]
    rc, output = _run_streamed(flash_cmd, log, no_progress_timeout)
    if rc != 0 and looks_permission_denied(output):
        # Ticket 009's fallback: the pre-check above missed it (unknown
        # `port`, or a race), but pyocd's own output named the problem as
        # it streamed -- `_run_streamed` already killed the subprocess
        # and logged the specific message the instant that line arrived
        # (see flash.run_streamed_with_watchdog's own docstring), rather
        # than waiting out `no_progress_timeout`. Return here, without
        # ever reaching the transient-retry or mass-erase logic below --
        # see this function's own docstring for why a permission problem
        # must never be retried.
        return rc
    if rc != 0 and _looks_transient(output):
        _log(
            log,
            "flash failed with a transient-looking probe/communication "
            "error — retrying once before any mass-erase decision.",
        )
        rc, output = _run_streamed(flash_cmd, log, no_progress_timeout)

    if rc != 0 and _looks_locked(output):
        # A locked/protected nRF (APPROTECT set, or a protected SoftDevice
        # region at 0x0) rejects every flash-algorithm erase, so the flash
        # fails before it can program. Neither sector nor chip erase clears
        # that — only a CTRL-AP mass erase (ERASEALL), which also resets
        # APPROTECT. Recover by mass-erasing, then retry the flash once.
        _log(
            log,
            "flash failed — attempting CTRL-AP mass erase to recover a "
            "locked device, then retrying.",
        )
        erase_cmd = [
            *_PYOCD, "erase",
            "-t", target_mcu,
            "--uid", uid,
            "--mass",
        ]
        erase_rc, _erase_output = _run_streamed(erase_cmd, log, no_progress_timeout)
        if erase_rc != 0:
            _log(log, f"Error: mass erase failed (exit {erase_rc}).")
            return erase_rc
        rc, output = _run_streamed(flash_cmd, log, no_progress_timeout)
        if rc != 0:
            name = board_name or uid
            _log(
                log,
                f"Error: flash still failed after mass erase (exit {rc}) "
                f"— {name} WAS ERASED AND NOW HAS NO FIRMWARE. It will "
                "not run until it is successfully reflashed.",
            )
            return rc
    elif rc != 0:
        # No recognized signature -- neither transient (already retried
        # above) nor locked. Fail as-is, without ever mass-erasing: see
        # the "unrecognized = don't erase" rationale above.
        _log(
            log,
            f"Error: flash failed (exit {rc}) with no recognized "
            "recoverable signature -- not mass-erasing. If this device "
            "is actually locked/protected, run 'pyocd erase --mass' "
            "manually.",
        )
        return rc

    reset_cmd = [
        *_PYOCD, "reset",
        "-t", target_mcu,
        "--uid", uid,
    ]
    reset_rc, _reset_output = _run_streamed(reset_cmd, log, no_progress_timeout)
    return reset_rc
