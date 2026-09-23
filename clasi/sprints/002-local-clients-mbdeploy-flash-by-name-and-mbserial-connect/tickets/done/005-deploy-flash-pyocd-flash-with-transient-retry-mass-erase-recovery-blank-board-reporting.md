---
id: '005'
title: 'deploy.flash: pyOCD flash with transient retry, mass-erase recovery, blank-board
  reporting'
status: done
use-cases:
- UC-008
depends-on: []
github-issue: ''
issue: mbdeploy-flash-by-name-via-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# deploy.flash: pyOCD flash with transient retry, mass-erase recovery, blank-board reporting

## Description

Create `mbtools.deploy.flash`, porting today's `mbdeploy`'s
`src/mbdeploy/flash.py` (`/Volumes/Proj/proj/robot-projects/mbdeploy`)
near-verbatim: the one place pyOCD's failure wording is matched against
known signatures, unchanged in shape from the existing, hardware-proven
implementation (`docs/acceptance/001-hardware.md`'s hodr/braeburn
transient-retry and braeburn's `0x67` mass-erase-recovery findings are
exactly what this module already handles today, in the old repo).

Port, with the target MCU constant coming from `mbtools.registry.flash`
(`DEFAULT_MCU = "nrf52833"`, already defined there for sprint 001's
minimal flash op — reuse it, don't redefine a second copy):

- `_validate_hex` (intelhex pre-flight check, before any pyocd
  invocation).
- `_TRANSIENT_SIGNATURES`/`_looks_transient` and
  `_LOCKED_SIGNATURES`/`_looks_locked` (the narrow, named,
  documented string-matching this project already treats as the one
  place to change if pyOCD's wording shifts).
- `_run_streamed` (streams pyocd's stdout/stderr through a `log`
  callback line-by-line, not buffered until exit).
- `flash_hex(uid, hex_path, target_mcu, log, board_name)`: first flash
  attempt; on a transient-looking failure, retry once; on a still-
  failing, locked-looking failure, CTRL-AP mass erase then retry once
  more; on any other failure, fail without erasing (the "unrecognized =
  don't erase" asymmetry — an unnecessary mass erase destroys a working
  board's firmware, a missed recovery only costs one manual `pyocd
  erase --mass`); if the mass erase succeeded but the retried flash still
  fails, report explicitly and unmissably (via `log`, not only return
  value) that the board "WAS ERASED AND NOW HAS NO FIRMWARE."

This module takes no lock itself and knows nothing about the registry —
it flashes whatever UID it's given. Ticket 007 is the caller that takes
the `flash`-kind lock via `registry.client` before invoking this module,
and calls `mark_flashed` (ticket 003) after.

Independent of tickets 001-004 and 006 — a standalone pyOCD wrapper, no
registry dependency — can be built in parallel with any of them.

## Acceptance Criteria

- [x] `flash_hex()`'s behavior matches today's `mbdeploy`'s `flash.py`
      exactly: transient-signature retry (exactly once), locked-signature
      mass-erase-then-retry (exactly once), unrecognized-failure fails
      without erasing, explicit blank-board report when a post-erase
      reflash still fails.
- [x] `_validate_hex` runs before any pyocd subprocess is constructed —
      a missing/unreadable/malformed hex file fails with a clear message
      before touching a board.
- [x] Every pyocd invocation's output streams through the `log` callback
      as it arrives (not buffered until the process exits) — this is
      what let `mbdeploy`'s existing remote streaming stay within its
      client-side read timeout during a real multi-second flash
      (`mbdeploy`'s own ticket 010 finding); this sprint's local-only
      `mbdeploy` still benefits from live progress in its own terminal.
- [x] The pyOCD invocation shape (`[sys.executable, "-m", "pyocd"]`, not
      a bare PATH lookup) matches sprint 001's `registry.flash`'s own
      precedent, for the same reason (an isolated venv may not put
      pyocd's console script on PATH).
- [x] No test in this ticket shells out to a real `pyocd` binary — the
      subprocess runner is injectable, same pattern as sprint 001's
      `registry.flash.Runner`.

## Testing

- **Existing tests to run**: none in `mbtools` yet (new module); confirm
  `uv run pytest tests/registry/` still passes (no shared code touched).
- **New tests to write**: ported near-verbatim from today's `mbdeploy`'s
  own `flash.py` test suite against an injected fake runner — transient-
  retry-then-succeed, transient-retry-then-still-fails, locked-then-
  mass-erase-then-succeed, locked-then-mass-erase-then-still-fails
  (blank-board report), unrecognized-failure-does-not-erase, and the
  `_validate_hex` pre-flight failure path.
- **Verification command**: `uv run pytest tests/deploy/`
