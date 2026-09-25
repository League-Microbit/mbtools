---
id: '003'
title: Silent-board SWD naming plus flash-triggered known-blank wiring
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ['001', '002']
github-issue: ''
issue: name-silent-boards-over-swd-and-keep-list-table-clean.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Silent-board SWD naming plus flash-triggered known-blank wiring

## Description

The core of issue 1: when a board doesn't answer `HELLO`, read its real
name over SWD instead of showing `-`. Depends on ticket 001 (the
chip-identity cache column and didn't-announce/known-blank states must
already exist) and ticket 002 (the claim must already gate probing, so
this ticket's new SWD session is never racing another instance for the
same probe).

**`identity.py`**: port `mbdeploy`'s `read_device_id`/`friendly_name`/
`read_board_name` (`/Volumes/Proj/proj/robot-projects/mbdeploy/src/
mbdeploy/devices.py`) into a new `read_chip_identity(uid, ...)` (or
split `read_device_id`/`friendly_name` as separate functions, matching
the reference implementation's own split — implementer's call). Use
pyOCD's Python API directly (`from pyocd.core.helpers import
ConnectHelper`), **not** a subprocess — see sprint.md's Design Rationale
for why this differs from `flash.py`/`flashlogic.py`'s subprocess
convention. Same semantics as the reference: `connect_mode="attach"`,
`auto_unlock=False`, `blocking=False`, try `target_override` then `None`
(auto-detect) on failure, read `FICR_DEVICEID1` (`0x10000064`), silence
pyOCD's own logging around the call (`_quiet_pyocd`-equivalent) so it
doesn't interleave with `mbregistry`'s own log/print output. Make
`pyocd` an optional import (mirroring `identity.py`'s existing optional
`serial` import) so the module stays importable without it. Add a
test-only escape hatch (a `connect_helper=`/session-factory parameter,
mirroring `probe`'s own `serial_factory=`) so unit tests never touch a
real pyOCD session.

**`daemon.py`**: in `_maybe_probe`, when `identity.probe(...)` returns
`None` (or a blank/malformed `ProbeResult`) and this uid has no cached
chip identity yet (per ticket 001's new store column), call
`identity.read_chip_identity(uid, ...)` and, on success, persist the
result via the store (ticket 001's cache-write path). A failed SWD read
(returns `None`) is not an error — leave `NAME` at `-` for this cycle,
same as today, and let a later eligible cycle retry (this uid stays
eligible for a retry exactly when the ordinary re-probe rule already
allows it — no new eligibility bookkeeping needed beyond "no cached
identity yet").

Also in this ticket: wire the flash-triggered re-probe give-up path
(`run_once`'s `timed_out` handling, and the ordinary post-flash-reappear
probe in `_maybe_probe` when `uid in self._flash_pending`) to ticket
001's known-blank store method instead of the plain didn't-announce one,
when the probe that returns `None` was triggered by a flash release.
This is the ticket that investigates and, if needed, fixes the
pre-existing `togov` finding (sprint.md Open Question 2) — trace whether
the existing `_on_flash_release` → `_flash_pending` → re-probe pipeline
already reaches `apply_probe_result(uid, None)` reliably for a
mass-erase-then-failed-reflash board, and if it does, whether simply
routing that one call site to the known-blank method (rather than
didn't-announce) is sufficient, or whether a real gap in the trigger
chain needs fixing first. Document what you find in the ticket's own
Implementation Notes (the sprint-planner did not root-cause this at the
architecture level — see sprint.md Open Question 2).

## Acceptance Criteria

- [ ] A board that never answers `HELLO` shows its real five-letter name
      in `NAME` once the SWD read succeeds, on a faked-pyOCD test (no
      real hardware in the automated suite).
- [ ] The SWD read is attempted at most once per UID — a test asserting
      no second `read_chip_identity` call across a simulated detach/
      reattach or reflash once the cache is set.
- [ ] A failed SWD read (faked to raise/return `None`) leaves `NAME` at
      `-` and does not raise out of the probe cycle.
- [ ] The friendly-name codebook is tested against at least the known
      device-id/name example from `mbdeploy`'s own reference (and any
      other fixtures ported from `mbdeploy`'s test suite, if available).
- [ ] A flash-triggered re-probe that still gets nothing lands on the
      known-blank state (not didn't-announce) — a test reproducing the
      `togov`-shaped scenario (flash lock release → re-probe → no
      `HELLO` reply) asserts the resulting `STATE`/`FIRMWARE` show
      known-blank/`no firmware`, not stale prior-firmware text.
  - [ ] Implementation Notes in this ticket document what was found
        tracing the pre-existing pipeline (per Open Question 2 above).
- [ ] An ordinary (non-flash-triggered) silent probe still lands on
      didn't-announce, not known-blank — the two call sites remain
      correctly distinguished.
- [ ] The SWD read never runs for a uid this instance doesn't hold the
      cross-instance claim for (ticket 002) — verified by a test
      asserting `read_chip_identity` is never called for a uid whose
      claim attempt failed.

## Implementation Plan

**Approach**: Port `identity.py`'s SWD function first, with its own
standalone unit tests against a faked pyOCD session, then wire it into
`daemon.py`'s `_maybe_probe`, then investigate and fix the flash-
triggered known-blank routing as a distinct, clearly-separated sub-step
(so the SWD-naming and known-blank-routing halves of this ticket can be
reviewed/tested independently even though they land in one ticket).

**Files to modify**:
- `src/mbtools/registry/identity.py`: new `read_chip_identity`/
  `friendly_name` (or equivalent split), `FICR_DEVICEID1` constant,
  optional `pyocd` import, test-only session-factory parameter.
- `src/mbtools/registry/daemon.py`: `_maybe_probe`'s no-announcement
  branch calls the new identity function when no cache exists yet;
  the flash-triggered give-up/reappear-and-fail paths route to the
  known-blank store method.
- `src/mbtools/registry/store.py`: only if ticket 001's cache-write API
  needs a small adjustment once this ticket's real call pattern is
  known (expected to be minor/none).

**Testing plan**:
- `tests/registry/identity/test_identity.py`: friendly-name codebook
  fixtures; `read_chip_identity` against a faked `ConnectHelper`/session
  (success, attach-mode-refused/locked-part failure, pyOCD-unavailable
  failure) — no real probe.
- `tests/registry/daemon/test_daemon.py`: SWD-fallback-on-silent-probe
  test; cache-hit-skips-SWD test; claim-gates-SWD test; the
  `togov`-shaped known-blank-routing test described above.
- Run: `uv run pytest tests/registry/identity/ tests/registry/daemon/
  tests/registry/store/ -x`.

**Documentation updates**: none in this ticket — ticket 006 covers docs
for the whole sprint, once the exact state-constant name and any
identity-related `--json` fields are final.
