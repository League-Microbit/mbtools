---
id: '003'
title: 'registry.flashlogic: extract shared flash-with-recovery logic from deploy.flash'
status: done
use-cases:
- SUC-003
depends-on: []
github-issue: ''
issue: mbdeploy-flash-by-name-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.flashlogic: extract shared flash-with-recovery logic from deploy.flash

## Description

Per sprint.md's Architecture (Decision 4), move `flash_hex` and its
transient/locked-signature-matching helpers out of `mbtools.deploy.flash`
into a new module, `mbtools.registry.flashlogic`, so both the existing
local flash path (`deploy.cli`, unchanged) and the new remote flash path
(`registry.remote_api`, ticket 009) call the exact same implementation.
`mbtools.deploy.flash` becomes a thin re-export shim so no existing
import or test in the already-hardware-validated local path
(`docs/acceptance/001-hardware.md`) needs to change.

This is a pure move, not a rewrite: `flash_hex` already has no CLI/
argparse dependency (module docstring: "This module takes no lock
itself and knows nothing about the registry"), so relocating it changes
only where the code lives, not what it does.

## Acceptance Criteria

- [x] `mbtools/registry/flashlogic.py` exists, containing
      `flash_hex`, `_validate_hex`, `_looks_transient`/
      `_TRANSIENT_SIGNATURES`, `_looks_locked`/`_LOCKED_SIGNATURES`,
      `_run_streamed`, and `_log` — moved verbatim (no logic change) from
      `mbtools/deploy/flash.py`. `DEFAULT_MCU` continues to be imported
      from `mbtools.registry.flash` (unchanged — sprint 1's constant,
      still owned there).
- [x] `mbtools/deploy/flash.py` re-exports `flash_hex` and `DEFAULT_MCU`
      from `mbtools.registry.flashlogic` (`from
      mbtools.registry.flashlogic import DEFAULT_MCU, flash_hex`,
      `__all__` unchanged) — every existing `from mbtools.deploy.flash
      import flash_hex` call site (`deploy.cli`) needs zero changes.
- [x] `mbtools.registry.flash.FlashOp` (the sprint-1 deliberately-minimal
      wire-protocol op) is untouched — this ticket does not merge it with
      the new module or change its behavior in any way.
- [x] Module docstrings on both the new `flashlogic.py` and the shim
      `deploy/flash.py` explain the split (the shim's docstring points to
      `flashlogic` as the real implementation; `flashlogic`'s docstring
      notes it is called from both `deploy.cli` locally and
      `registry.remote_api` remotely).

## Testing

- **Existing tests to run**: the full existing flash test suite (wherever
  today's `deploy.flash` tests live) must pass unchanged, run against
  both the new `mbtools.registry.flashlogic` module directly and through
  the `mbtools.deploy.flash` re-export, to prove the shim is behavior-
  identical.
- **New tests to write**: a smoke test importing `flash_hex`/
  `DEFAULT_MCU` from both `mbtools.deploy.flash` and
  `mbtools.registry.flashlogic` and asserting they're the same object
  (`is` identity), so a future edit to one path can't silently drift
  from the other.
- **Verification command**: `uv run pytest tests/deploy/ tests/registry/`
