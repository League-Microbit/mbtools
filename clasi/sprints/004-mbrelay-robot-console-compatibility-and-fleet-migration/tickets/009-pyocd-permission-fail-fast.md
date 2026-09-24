---
id: 009
title: pyOCD permission fail-fast
status: in-progress
use-cases:
- SUC-005
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

- [x] Flashing a device with no USB permission fails within a few
      seconds with a clear, actionable error message — not a hang.
- [x] Flashing a device that *does* have permission is unaffected (same
      retry/mass-erase/blank-board behavior as before this ticket).
- [x] A pyOCD invocation that goes silent for longer than the
      no-progress threshold (simulated in a test, not requiring real
      hardware) is killed and reported, not left running.
- [x] The permission pre-check runs before pyOCD is invoked at all (test
      asserts pyOCD's subprocess is never spawned when the pre-check
      fails).
- [x] No regression to the existing transient-retry or mass-erase-recovery
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

## Implementation Notes

- **File path deviation** (same convention already established by ticket
  008's own notes): the real test files are
  `tests/registry/flash/test_flash.py` and
  `tests/registry/flash/test_flashlogic.py` (the latter unchanged —
  it's only the re-export identity smoke test; the real behavioral
  coverage lives in `tests/deploy/test_deploy_flash.py`, which exercises
  `flashlogic.flash_hex` via its `deploy.flash` re-export, per that
  file's own docstring). The scoped verification command that actually
  matches this repo's layout is `uv run pytest
  tests/registry/flash/test_flash.py tests/registry/flash/test_flashlogic.py
  tests/deploy/test_deploy_flash.py`.

- **Real pyOCD failure wording, confirmed by reading this project's own
  installed pyocd** (`.venv/lib/python3.13/site-packages/pyocd`), not
  guessed at:
  - `core/helpers.py::get_all_connected_probes` treats an inaccessible
    (permission-denied) CMSIS-DAP device as simply *not enumerated* —
    it prints `"Waiting for a debug probe matching unique ID '<uid>' to
    be connected..."` exactly **once**, then loops silently
    (`sleep(0.01)`) forever. This is the literal mechanism behind the
    "no timeout, ever" bug the issue describes.
  - `probe/pydapaccess/interface/{pyusb_backend,pyusb_v2_backend}.py`
    build the message `"<error> while trying to interrogate a USB
    device (VID=... PID=...). This can probably be remedied with a udev
    rule."` on `errno.EACCES`, where `<error>` is `str(usb.core.USBError)`
    — libusb1's own wording for `EACCES` is `"[Errno 13] Access denied
    (insufficient permissions)"`.
  - `pyocd flash`/`erase`/`reset` (`subcommands/load_cmd.py`,
    `erase_cmd.py`, `reset_cmd.py`) all default to
    `logging.WARNING`, and the message above is logged via
    `LOG.warning`, so it reaches a plain (non-verbose) invocation's
    output by default — the signature list this ticket adds
    (`flash.py`'s `_PERMISSION_SIGNATURES`) is not gated on `-v`.

- **Design: the shared-helper option, not "apply the fix twice."** Per
  the ticket's own "implementer's judgment" note, the fix is factored
  once into three new public functions in `flash.py` (imported into
  `flashlogic.py` exactly like `DEFAULT_MCU` already is, keeping the
  same one-directional `flashlogic` → `flash` dependency):
  - `check_device_permission(port)` — the proactive `os.access` pre-check.
    Returns `None` (skip, not a failure) for an unknown (`None`/empty) or
    *nonexistent* `port` — a genuinely-absent device is a different
    problem ("no such device"), and treating a missing path as a
    permission failure would misreport a disconnected/renumbering board
    as a udev-rule problem.
  - `looks_permission_denied(output)` — the signature-matching function,
    shared by the per-line early-exit below and `flashlogic.flash_hex`'s
    own "don't retry a permission failure" decision.
  - `run_streamed_with_watchdog(cmd, log, no_progress_timeout)` — the
    no-progress-timeout-and-early-permission-exit-aware replacement for
    the old bare `subprocess.Popen` + `for line in proc.stdout` loop,
    used by both `flash._default_runner` (discarding the accumulated
    output text — `FlashOp` does no signature matching) and
    `flashlogic._run_streamed` (a thin `log`-may-be-`None`-safe wrapper,
    since `run_streamed_with_watchdog` itself requires a real callable,
    mirroring `Runner`'s own contract).

- **`FlashOp.flash_hex` resolves its own port, via the `store` it
  already holds** (`self._store.find(uid).port`) — no new constructor
  parameter or call-site plumbing needed, unlike `flashlogic.flash_hex`
  (a free function with no store access), which gains a new optional
  `port` parameter that `deploy.cli._flash` (local branch, from the
  already-resolved `device["port"]`) and `remote_api._op_flash` (from
  its own resolved `record.port`) now pass through.

- **Deviation: a permission pre-check failure in `FlashOp.flash_hex`
  returns a failed `FlashResult`, not a new raised exception type** —
  unlike `HexValidationError`/`FlashLockNotHeldError`, even though the
  same "pyocd never ran, so `increment_flash_count` must not fire"
  reasoning applies. Reason: `api.py`'s per-connection request loop
  (`_handle_connection`) catches only `(ConnectionError, OSError)`
  around its dispatch loop — a new, uncaught exception type reaching it
  from `_op_flash` would crash that connection's handling *and leak the
  flash-kind lock*, which is worse than the hang this ticket fixes. Both
  of `FlashOp.flash_hex`'s direct callers (`api.py`/`remote_api.py`)
  already handle a failed `FlashResult`/`rc` as their generic "flash
  didn't work" path, so this needed no new `except` clause at either
  call site. Documented in `FlashOp.flash_hex`'s own docstring, step 3.

- **`no_progress_timeout` default: 60.0s**, chosen conservatively longer
  than any silent stretch observed in this project's own hardware
  acceptance logs (`docs/acceptance/*.md`) — flagged in `flash.py`'s own
  comment as an ASSUMPTION worth revisiting against ticket 011's
  real-hardware acceptance run if a legitimately slow, silent pyocd
  phase (e.g. a very large mass erase) is ever observed to false-positive.

- **`deploy/cli.py`'s `debug` command (`_run_pyocd`) is deliberately
  left out of this fix**, per the dispatch brief's own "decide/document"
  note — documented in `_run_pyocd`'s own docstring. Two independent
  reasons: (1) the no-progress *watchdog* must never apply to a
  genuinely interactive session (`pyocd commander`'s REPL, or a human
  reading `gdbserver` output before typing a command) — Decision 8's
  "don't cut off genuinely-in-progress work" concern, here for
  open-ended human interaction rather than a slow mass erase; (2) the
  permission *pre-check* has no natural attachment point, since `debug`
  is a bare passthrough of whatever `pyocd` subcommand/args the operator
  typed after `--` (`args.pyocd_args`), not a single reliably
  `--uid`-shaped invocation to resolve a device port against.

- **No new test-fake signature breaks**: `tests/deploy/test_deploy_cli.py`'s
  `_FakeFlashHex.__call__` gained a `port=None` parameter (and now
  records it) to match `flash_hex`'s new keyword-only argument — every
  other call site already passed only keyword arguments this fake
  already accepted.
