---
id: 009
title: pyOCD permission fail-fast
status: open
use-cases: [SUC-005]
depends-on: []
github-issue: ''
issue: non-root-usb-access-and-pyocd-permission-hang.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# pyOCD permission fail-fast

## Description

Found during sprint 002 hardware acceptance: without USB permission,
pyOCD retries indefinitely, and `_run_streamed` (in both
`registry/flash.py`'s `FlashOp` and `registry/flashlogic.py`'s shared
implementation, confirmed by this sprint's own code research — neither
has a timeout on the `subprocess.Popen`/`proc.stdout` read loop) hangs
`mbdeploy deploy`/`debug` forever instead of failing. Fix with a
permission pre-check plus a bounded no-progress timeout (architecture
Decision 8 — not a total-runtime cap, since mass-erase recovery
legitimately takes a while).

**Approach**
- Before invoking pyOCD, check the target device node's access (e.g.
  `os.access(path, os.R_OK | os.W_OK)` on the relevant USB/tty path) and
  fail immediately with a clear "permission denied — see udev rule
  install (ticket 008)" message if it's not accessible — this is the
  primary fix; catches the common case before pyOCD is even invoked.
- In `_run_streamed`'s output loop, track wall-clock time since the last
  line of output; if no output arrives for N seconds (configurable,
  default chosen conservatively longer than any observed real flash's
  longest legitimate silent stretch), kill the subprocess and raise a
  clear timeout error — this is the fallback for whatever the pre-check
  doesn't catch.
- Treat an early "no permission"/"unable to open" line in pyOCD's own
  output as immediately fatal, not retried — don't wait out the
  no-progress timeout when pyOCD has already told us what's wrong.
- Apply the fix once, shared by both `registry/flash.py`'s `FlashOp` and
  `registry/flashlogic.py`'s `_run_streamed`-equivalent (or, if that
  invites duplication, factor the subprocess-running helper itself into
  one shared function both call — implementer's judgment, documented in
  the ticket's actual diff).

**Files to create/modify**
- `src/mbtools/registry/flashlogic.py` (extended — permission pre-check,
  no-progress timeout).
- `src/mbtools/registry/flash.py` (extended — same fix, or delegates to
  the now-shared helper).
- `tests/registry/test_flashlogic.py` (extended).

**Documentation updates**: none required — internal robustness fix, no
observable interface change for a device that *does* have permission.

## Acceptance Criteria

- [ ] Flashing a device with no USB permission fails within a few
      seconds with a clear, actionable error message — not a hang.
- [ ] Flashing a device that *does* have permission is unaffected (same
      retry/mass-erase/blank-board behavior as before this ticket).
- [ ] A pyOCD invocation that goes silent for longer than the
      no-progress threshold (simulated in a test, not requiring real
      hardware) is killed and reported, not left running.
- [ ] The permission pre-check runs before pyOCD is invoked at all (test
      asserts pyOCD's subprocess is never spawned when the pre-check
      fails).
- [ ] No regression to the existing transient-retry or mass-erase-recovery
      signatures `flashlogic.flash_hex` already detects.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_flashlogic.py tests/registry/test_flash.py`
  (confirm no regression to retry/mass-erase behavior).
- **New tests to write**: a fake `Popen`-like process that never produces
  output (asserts the no-progress timeout fires within a bounded test
  wall-clock time, not by actually waiting out a real long timeout in the
  test suite — use a short configurable threshold in the test); a fake
  process whose device node has no read/write access (asserts immediate
  failure, no subprocess spawned); a fake process that emits an early
  "unable to open"-style line (asserts immediate fatal, not retried).
- **Verification command**: `uv run pytest
  tests/registry/test_flashlogic.py tests/registry/test_flash.py`
