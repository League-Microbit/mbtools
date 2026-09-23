---
id: '007'
title: 'Minimal flash op: pyOCD flash by UID, flash-triggered re-probe'
status: done
use-cases:
- SUC-003
depends-on:
- '005'
- '006'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Minimal flash op: pyOCD flash by UID, flash-triggered re-probe

## Description

Build `mbtools.registry.flash`: the registry's own minimal flash
operation (brief §3.6 — "identify one micro:bit by serial port or by
name; hand it a hex file; it flashes"). Per sprint.md's Architecture
(module "flash"), this is deliberately minimal: it wraps a pyOCD
invocation and nothing else — no diagnostics, no retry-on-transient-
error, no erase-and-reflash recovery, no blank-board reporting. All of
that "fancy work" explicitly stays in `mbdeploy` (sprint 002), per brief
§3.6's "if you don't need to put flashing in MB Registry, don't."

Port the pyOCD invocation shape from `mbdeploy/src/mbdeploy/flash.py`:
invoking `pyocd` through the current interpreter
(`[sys.executable, "-m", "pyocd"]`, not a bare PATH lookup — see that
file's comment on why: mbdeploy/mbtools is typically installed via an
isolated venv where the console script isn't on PATH but the package
is importable), and `_validate_hex()`'s pre-flight `intelhex` parse
(never touch a board with a hex file that doesn't even parse). Do
**not** port `flash.py`'s failure-signature matching or retry/mass-erase
logic (`_TRANSIENT_SIGNATURES` and friends) — that's `mbdeploy`'s job,
not the registry's minimal op.

**Contract with `locks` and `daemon`** (per sprint.md's Design Rationale
"a `flash`-kind lock's release is the re-probe trigger"): this module
requires a `flash`-kind lock already held on the target uid before it
will flash (it does not take the lock itself — the caller, eventually
`mbdeploy` in sprint 002 or a direct API call in this sprint's tests,
takes the lock via the API/`locks` first). On completion (success or
failure), it calls `store.increment_flash_count(uid)` and releases
nothing itself — releasing the `flash`-kind lock is the caller's job,
and *that* release is what ticket 006's daemon-core hook turns into a
re-probe. This module's own job ends at "flashed, streamed the log,
recorded the attempt" — it does not wait for or trigger the re-probe.

Log lines stream back via a callback (mirroring `flash.py`'s `log`
parameter), so a caller (eventually the API, ticket 008) can relay them
to a remote client without buffering the whole flash in memory.

## Acceptance Criteria

- [x] `flash.flash_hex(uid: str, hex_path: str, log: Callable[[str],
      None]) -> FlashResult` validates the hex file with `intelhex`
      before touching pyOCD, returning a clear `FlashResult` (or raising
      a distinguishable exception) for an unreadable/malformed file
      without invoking pyOCD at all.
- [x] Requires evidence of a held `flash`-kind lock on `uid` (e.g. takes
      a `locks` reference and checks `locks.status(uid)` names a
      `flash`-kind holder) — refuses to flash otherwise, so this module
      can never be the thing that bypasses the locking contract it
      itself depends on for the re-probe hook to work.
- [x] The pyOCD subprocess invocation is behind an injectable callable
      (constructor parameter or similar) — no test in this ticket shells
      out to a real `pyocd` binary or touches a real probe.
- [x] On successful completion, `store.increment_flash_count(uid)` is
      called exactly once.
- [x] On a pyOCD failure (non-zero exit, or the injected runner raising),
      `flash_hex` returns/raises a result distinguishing failure from
      success, and still calls `increment_flash_count` (an attempted
      flash counts, per the ERD's `flash_count` semantics — "incremented
      on each *completed* flash," where completed means "pyOCD ran to
      completion," not "succeeded"; a hex-validation failure that never
      reaches pyOCD does *not* increment it).
- [x] Log lines from the injected pyOCD runner are relayed to the `log`
      callback in order, not buffered and dumped at the end.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/locks/` —
  confirm the lock-status check this ticket relies on still behaves.
- **New tests to write**: hex validation failure short-circuits before
  any pyOCD call; flashing without a `flash`-kind lock held is refused;
  successful flash increments `flash_count` and relays log lines in
  order; a failed (non-zero) pyOCD run still increments `flash_count`
  and reports failure distinctly from success.
- **Verification command**: `uv run pytest tests/registry/flash/`
