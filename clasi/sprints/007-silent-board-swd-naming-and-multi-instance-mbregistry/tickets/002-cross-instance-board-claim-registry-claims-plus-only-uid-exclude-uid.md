---
id: '002'
title: 'Cross-instance board claim (registry.claims) plus --only-uid/--exclude-uid'
status: open
use-cases: [SUC-005]
depends-on: []
github-issue: ''
issue: robot-console-on-mbregistry-multi-instance-and-spawn-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Cross-instance board claim (registry.claims) plus --only-uid/--exclude-uid

## Description

Build the new `registry.claims` module (sprint.md Architecture Step 3)
and wire it into `daemon.py`'s attach path so two `mbregistry` processes
on one host never both treat the same board as their own. This lands
before (or alongside — see sprint.md's own sequencing note) ticket 003's
SWD read, since the SWD read also opens a session on the same probe and
must be guarded by the same claim.

**`registry.claims` (new module)**: `try_claim(uid) -> ClaimHandle |
None` and a release (either an explicit `release(handle)` or a
context-manager form — implementer's call, document whichever is
chosen). Unix implementation: `flock` (non-blocking) on
`<shared-runtime>/mbtools/claims/<uid>.lock`, plus `TIOCEXCL` on the
open tty once a port is actually opened for that uid (the flock covers
"is another *mbregistry* instance already claiming this uid," `TIOCEXCL`
additionally guards the specific serial port against *any* other opener,
belt-and-suspenders). Windows implementation: a no-op that always
succeeds — COM-port opens are already exclusive at the OS level, so no
new mechanism is needed there (document this explicitly in the module,
don't silently skip Windows). Add a `registry.paths` helper for the
claims-directory location (world-writable-sticky, `/tmp`-style,
created on first use if missing — see sprint.md Open Question 4 for the
permissions caveat this ticket should confirm against sprint 006's
`--user`/`--system` split if that sprint has merged by the time this
ticket starts).

**`daemon.py` wiring**: in `run_once`, before `store.upsert_attached` is
called for a newly-seen uid (the `if uid not in previously_attached:`
branch), attempt `claims.try_claim(uid)`. If the claim fails, skip this
uid entirely for this cycle — do not upsert it, so it never appears in
this instance's own `list` (per SUC-005's postcondition). Retry the
claim on a later cycle (a uid whose claim failed this cycle is simply
absent from `previously_attached` next cycle too, so the existing
"newly-seen" branch naturally retries it — no new bookkeeping needed).
Release the claim when the uid detaches (the existing
`store.mark_disconnected` branch) or, if a released-on-process-exit
design is chosen instead for simplicity, document that the daemon does
not need to explicitly release on detach at all (the OS releases the
flock when there's no reason to keep the fd open) — pick one approach
and be consistent; a `ClaimHandle`'s own lifetime tied to "this uid is
currently attached to this instance" is the natural shape either way.

**`--only-uid`/`--exclude-uid`**: new `cli.py` flags (repeatable,
comma-separated, or both — implementer's call, follow the existing
`--peer` repeatable-flag convention). Wire into the claim-check: a uid
excluded by `--exclude-uid`, or not included when `--only-uid` is given
and non-empty, is treated as never-claimable by this instance — skip the
`try_claim` call entirely and never upsert it (same effect as a failed
claim, but decided locally rather than by contention).

## Acceptance Criteria

- [ ] `registry.claims.try_claim`/release exist, with a real-filesystem
      `tmp_path` test proving two independent claim attempts on the same
      uid (from two threads/processes) never both succeed.
- [ ] A crashed/killed holder's claim becomes available to another
      claimant with no manual cleanup (verified by closing the holder's
      fd/process and re-attempting).
- [ ] Windows path is an explicit, documented no-op that always
      succeeds — not silently skipped, not raising `NotImplementedError`.
- [ ] `registry.paths` gains a claims-directory helper following the
      existing location-knowledge convention (see `system_db_path`/
      `user_db_path`/sprint 006's service-artifact helpers for the
      pattern to match).
- [ ] `daemon.run_once` never calls `store.upsert_attached` for a uid
      whose claim attempt failed this cycle — a test using two
      `Daemon`/`Store` pairs sharing one fake claims directory proves a
      uid claimed by one `Daemon` never appears in the other's
      `list_devices()`.
- [ ] A claim that fails this cycle is retried (not permanently given
      up on) on a later cycle once it becomes available.
- [ ] `--only-uid`/`--exclude-uid` are wired into the claim-check path
      and covered by CLI tests, following the existing `--remote-port`-
      style flag test pattern.
- [ ] No test in the automated suite touches a real board or a real
      probe — every claim test uses `tmp_path` and fake/simulated
      contention, matching the module's own "no real hardware in the
      automated suite" convention already established for
      `service`-style tests in sprint 006.

## Implementation Plan

**Approach**: Build `registry.claims` and its tests standalone first
(pure filesystem-primitive module, no dependency on `daemon`/`store`),
then wire it into `daemon.py`'s `run_once`, then add the CLI flags.

**Files to modify/create**:
- `src/mbtools/registry/claims.py` (new): `try_claim`/release,
  Unix/Windows dispatch.
- `src/mbtools/registry/paths.py`: new claims-directory helper.
- `src/mbtools/registry/daemon.py`: `run_once`'s newly-seen-uid branch
  gates on `claims.try_claim`; accepts an injected claims-module/
  claimer callable the same way `Daemon.__init__` already accepts
  `serial_factory`/`probe_timeout_s` as test-only escape hatches, so
  tests can fake claim outcomes without touching a real filesystem when
  that's more convenient than `tmp_path`.
- `src/mbtools/registry/cli.py`: `--only-uid`/`--exclude-uid` argparse
  additions; threaded into whatever `daemon`/`claims` construction
  `cmd_run`/`_run_registry` does.

**Testing plan**:
- `tests/registry/claims/test_claims.py` (new): real-`flock` contention
  test, crash-release test, Windows-no-op test (mocked platform check).
- `tests/registry/daemon/test_daemon.py`: extend with a claim-gated
  attach test (fake claimer that always denies uid X; assert
  `upsert_attached` is never called for X, and no exception/hang
  results).
- `tests/registry/cli/`: `--only-uid`/`--exclude-uid` flag/env
  resolution tests.
- An integration-style test (new or extended existing file) standing up
  two `Daemon`+`Store` pairs against one shared fake/real
  `tmp_path`-backed claims directory and a shared fake USB scan result,
  asserting exactly one lists the uid.
- Run: `uv run pytest tests/registry/claims/ tests/registry/daemon/
  tests/registry/cli/ -x`.

**Documentation updates**: none in this ticket — ticket 006 covers docs
for the whole sprint.
