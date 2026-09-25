---
id: '002'
title: Cross-instance board claim (registry.claims) plus --only-uid/--exclude-uid
status: done
use-cases:
- SUC-005
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

- [x] `registry.claims.try_claim`/release exist, with a real-filesystem
      `tmp_path` test proving two independent claim attempts on the same
      uid (from two threads/processes) never both succeed.
- [x] A crashed/killed holder's claim becomes available to another
      claimant with no manual cleanup (verified by closing the holder's
      fd/process and re-attempting).
- [x] Windows path is an explicit, documented no-op that always
      succeeds — not silently skipped, not raising `NotImplementedError`.
- [x] `registry.paths` gains a claims-directory helper following the
      existing location-knowledge convention (see `system_db_path`/
      `user_db_path`/sprint 006's service-artifact helpers for the
      pattern to match).
- [x] `daemon.run_once` never calls `store.upsert_attached` for a uid
      whose claim attempt failed this cycle — a test using two
      `Daemon`/`Store` pairs sharing one fake claims directory proves a
      uid claimed by one `Daemon` never appears in the other's
      `list_devices()`.
- [x] A claim that fails this cycle is retried (not permanently given
      up on) on a later cycle once it becomes available.
- [x] `--only-uid`/`--exclude-uid` are wired into the claim-check path
      and covered by CLI tests, following the existing `--remote-port`-
      style flag test pattern.
- [x] No test in the automated suite touches a real board or a real
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

## Implementation Notes

- **New module `src/mbtools/registry/claims.py`**: `try_claim(uid, *,
  claims_dir=None) -> ClaimHandle | None`, `ClaimHandle.release()`
  (idempotent, also usable as a context manager), a free-function
  `release(handle)`, `protect_fd(fd) -> bool` (best-effort `TIOCEXCL`,
  Unix only — see below), `is_claimable(uid, *, only_uids=,
  exclude_uids=)` (pure predicate), and `build_claim_fn(*, only_uids=,
  exclude_uids=, claims_dir=)` (builds the `--only-uid`/`--exclude-uid`-
  filtering closure `registry.cli` wires into `Daemon`). Unix: real
  non-blocking `flock` on `<claims_dir>/<uid>.lock`. Windows: an explicit
  no-op — `try_claim` returns a handle with no real fd and always
  succeeds, checked before any filesystem access.
- **`registry.paths.claims_dir_path()`**: `<tempfile.gettempdir()>/
  mbtools/claims` (not `/tmp` literally, so a sandboxed run or a host
  with `$TMPDIR` set differently still gets a writable location — the
  same permission *model*, `0o1777` world-writable-sticky, sprint.md's
  Open Question 4 asked for). Deliberately **one location for every
  scope** (no `system_`/`user_` split unlike the db/socket helpers) —
  a claim's entire point is being visible to every `mbregistry` process
  on the host regardless of privilege level, so a root (`--system`) and
  a user (`--user`) daemon must contend for the *same* file. This
  resolves Open Question 4 as stated: one shared, world-writable-sticky
  directory works for both.
- **`TIOCEXCL` scoping decision**: `claims.protect_fd(fd)` exists and is
  unit-tested (against a real `pty` pair) as its own standalone
  capability, but is **not wired into any actual serial/SWD open call
  site** by this ticket — no such call site is touched by this ticket's
  own daemon wiring (only the pre-`upsert_attached` claim check is in
  scope; the actual port open happens later, in `_maybe_probe` via
  `identity.probe`, and ticket 003's SWD read opens a second session on
  the same probe). Flagged explicitly for **ticket 003**: wire
  `claims.protect_fd` onto the fd once `identity.probe`/the new SWD path
  actually open the port, for the belt-and-suspenders protection the
  ticket description calls for.
- **`Daemon.claim_fn` default is a filesystem-free no-op, not
  `claims.try_claim`** — a deliberate deviation from `serial_factory`'s
  own precedent (whose default *does* reach for real hardware). Reasoning
  in `daemon.py`'s own "Cross-instance claim" docstring note: a missing
  `serial_factory` fails loudly (no port to open); a bare `Daemon(...)`
  silently defaulting to real `claims.try_claim` against the real, host-
  shared claims directory would instead succeed silently, and would have
  broken every one of the ~23 pre-existing `test_daemon.py` tests'
  isolation (they reuse the same fixed `UID` across many test functions
  in one pytest process, several without ever detaching, which would
  leak a real held `flock` across test functions). Real, cross-instance
  enforcement is opt-in: `registry.cli._run_registry` always builds and
  passes a real `claims.build_claim_fn(...)` through `assemble_registry`
  → `assemble_daemon_and_api` → `Daemon`, unconditionally (not only when
  `--only-uid`/`--exclude-uid` are given), so production `mbregistry run`
  always enforces the claim. Every pre-ticket-002 direct `Daemon(...)`
  construction, and every `assemble_daemon_and_api`/`assemble_registry`
  call in the existing test suite that omits `claim_fn`, needed zero
  changes as a result — confirmed by the full pre-existing
  `tests/registry/daemon/` and `tests/registry/cli/` suites passing
  unmodified.
- **Real bug found and fixed while wiring this in**: `run_once`'s final
  pass used to call `_maybe_probe(uid, info)` for *every* uid in the
  current USB scan, unconditionally. Once a claim can be denied, that is
  wrong two ways: (1) `Store.needs_probe` raises `KeyError` for a uid
  with no store record at all (exactly what a denied, never-before-seen
  uid has, since it's never upserted) — an uncaught crash of `run_once`;
  (2) even where it wouldn't crash (a uid mirrored from a peer, `host !=
  None`), probing it would mean physically opening a port for a board
  this instance does not hold the claim for — the exact hazard the claim
  exists to prevent, and the reason sprint.md's Architecture says the
  claim must land before ticket 003's SWD read shares the same probe.
  Fixed by tracking `locally_owned_now` (uids that are either already
  locally-owned and still attached, or newly claimed+upserted this
  cycle) during the locked attach/detach pass, and probing only that set
  afterward, instead of every currently-scanned uid. Covered by the new
  cross-instance integration tests (`test_daemon_claims_integration.py`)
  and the denied-claim unit tests in `test_daemon.py`, which would have
  hit the `KeyError` immediately without this fix.
- **Release policy**: explicit release-on-detach (in the same
  `store.mark_disconnected` branch), not release-on-process-exit-only —
  chosen for symmetry with the existing attach/detach bookkeeping; the
  module docstring documents both as equally valid per the ticket's own
  "implementer's call".
- **`--only-uid`/`--exclude-uid`**: repeatable `action="append"` flags
  (no comma-splitting), following `--peer`'s own convention. An excluded/
  not-included uid never reaches `try_claim` at all (decided locally,
  per the ticket's Description) — `build_claim_fn` filters before
  delegating.
- **Testing**: `tests/registry/claims/test_claims.py` (27 tests: flock
  contention including a real killed-subprocess crash-recovery test via
  `multiprocessing`, two-thread race, release/reclaim, context-manager
  form, directory creation/permissions, the Windows no-op, `protect_fd`
  against a real `pty`, `is_claimable`/`build_claim_fn`);
  `tests/registry/daemon/test_daemon.py` (8 new claim-gating tests, all
  23 pre-existing tests unmodified and passing);
  `tests/registry/daemon/test_daemon_claims_integration.py` (2 new tests,
  real `claims.try_claim` against a shared `tmp_path` directory, two
  independent `Daemon`/`Store` pairs); `tests/registry/cli/
  test_cli_claims.py` (5 new tests: flag parsing,
  `assemble_daemon_and_api`'s `claim_fn` forwarding). Full scoped run:
  `uv run pytest tests/registry/claims/ tests/registry/daemon/
  tests/registry/cli/ tests/registry/paths/ -q` → 154 passed.
- **Not tested directly**: `_run_registry`'s own one-line
  `build_claim_fn(only_uids=args.only_uid, exclude_uids=args.exclude_uid)`
  call is not exercised by a dedicated test that runs the real pipeline —
  matching this codebase's existing convention (no test in
  `tests/registry/cli/` invokes `_run_registry` directly; it has no seam
  to inject a fake `zeroconf`, so doing so would mean real mDNS
  registration in the test suite). Covered instead by: `build_parser`
  tests proving the flags parse into `args.only_uid`/`args.exclude_uid`
  correctly, `claims.build_claim_fn`'s own thorough unit tests, and
  `assemble_daemon_and_api`'s `claim_fn`-forwarding test proving the
  parameter this call passes through actually gates `Daemon`.
