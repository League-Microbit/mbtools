---
id: '003'
title: Silent-board SWD naming plus flash-triggered known-blank wiring
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
- '002'
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

- [x] A board that never answers `HELLO` shows its real five-letter name
      in `NAME` once the SWD read succeeds, on a faked-pyOCD test (no
      real hardware in the automated suite).
- [x] The SWD read is attempted at most once per UID — a test asserting
      no second `read_chip_identity` call across a simulated detach/
      reattach or reflash once the cache is set.
- [x] A failed SWD read (faked to raise/return `None`) leaves `NAME` at
      `-` and does not raise out of the probe cycle.
- [x] The friendly-name codebook is tested against at least the known
      device-id/name example from `mbdeploy`'s own reference (and any
      other fixtures ported from `mbdeploy`'s test suite, if available).
- [x] A flash-triggered re-probe that still gets nothing lands on the
      known-blank state (not didn't-announce) — a test reproducing the
      `togov`-shaped scenario (flash lock release → re-probe → no
      `HELLO` reply) asserts the resulting `STATE`/`FIRMWARE` show
      known-blank/`no firmware`, not stale prior-firmware text.
  - [x] Implementation Notes in this ticket document what was found
        tracing the pre-existing pipeline (per Open Question 2 above).
- [x] An ordinary (non-flash-triggered) silent probe still lands on
      didn't-announce, not known-blank — the two call sites remain
      correctly distinguished.
- [x] The SWD read never runs for a uid this instance doesn't hold the
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

## Implementation Notes

**`identity.py`**: added `FICR_DEVICEID1`, `DEFAULT_TARGET_MCU`,
`_NAME_CODEBOOK`, `friendly_name()`, `_quiet_pyocd()`, `read_device_id()`,
and `read_chip_identity()` (a thin `(name, device_id)` wrapper over
`read_device_id` + `friendly_name`), ported from `mbdeploy/src/mbdeploy
/devices.py` verbatim (same `connect_mode="attach"`/`auto_unlock=False`/
`blocking=False`, same `target_mcu`-then-`None` retry, same
`_quiet_pyocd` log-silencing). `pyocd` is now an optional import the same
way `serial` already is (`from pyocd.core.helpers import ConnectHelper as
_ConnectHelper`, guarded by `try/except`). `read_device_id`'s
`session_factory=` keyword is the test-only escape hatch, mirroring
`probe`'s own `serial_factory=`.

**`claims.protect_fd` wiring (ticket 002 handoff item)**: `identity.probe`
now best-effort applies `claims.protect_fd(ser.fileno())` right after a
successful `open()`, via a deferred `from mbtools.registry import
claims` import (avoids a module-level import cycle the same way
`reset_first`'s `mbtools.serial.connect` import already does). Wrapped in
a bare `try/except Exception: pass` — a fake/test port with no real
`fileno()` (every existing test in `test_identity.py`) is silently
skipped, never a test failure. New test:
`test_probe_applies_tiocexcl_to_the_opened_fd` proves the wiring with a
`fileno()`-supporting `FakeSerial` subclass and a monkeypatched
`claims.protect_fd`; `test_probe_without_a_real_fileno_does_not_raise`
proves the swallow path explicitly.

**`daemon.py`**: `_maybe_probe` now (1) calls `identity.read_chip_identity`
whenever `identity.probe` returns `None` **or** a non-`None`
`ProbeResult` with a blank `device_name` (the "malformed announcement"
case — the ticket's own "or a blank/malformed ProbeResult" text), and
only when the uid has no cached `chip_identity_name` yet; the SWD call
itself runs outside `_lock`, same as `identity.probe`. (2) Routes a
flash-pending uid's `None` probe result to `Store.apply_known_blank`
instead of `Store.apply_probe_result`, checked via `uid in
self._flash_pending` a second time (it isn't popped until after this
decision). `run_once`'s separate give-up path (a flash-pending uid that
never re-enumerates at all before its deadline) now also calls
`Store.apply_known_blank` instead of the placeholder
`apply_probe_result(uid, None)` ticket 001 left there.

**Open Question 2 — root-cause trace.** Read (not guessed) by tracing
`daemon.py`'s existing code, confirmed by the new
`test_flash_reprobe_reenumerates_but_stays_silent_lands_on_known_blank`
test: the flash-pending → re-probe → `apply_probe_result(uid, None)`
pipeline **did** already reach the right call site reliably for the
`togov`-shaped case (a mass-erase-then-failed-reflash board that *does*
re-enumerate but never answers `HELLO` again) — there was no missing
trigger, no dropped event, no race. The bug was purely which *method*
that reliably-reached call site used: `apply_probe_result(uid, None)`
(pre-ticket-001: `STATE_CONNECTED_NO_FIRMWARE` with the *old*, broad
meaning that also covered plain didn't-announce; post-ticket-001:
`STATE_ATTACHED_NO_ANNOUNCE`, since ticket 001 narrowed that call's
`None` branch) instead of the newly-available `apply_known_blank`
(`STATE_CONNECTED_NO_FIRMWARE`, now correctly reserved for this exact
case). So: **no gap in the trigger chain** — simply routing this one
call site (plus the separate `run_once` give-up path, which has the
identical shape) to `apply_known_blank` was sufficient. This also
resolved the original `togov` symptom (`list` showing stale
`JOYSTICK/joystick` instead of any no-firmware indication) as a
side-effect: `apply_known_blank`, like `apply_probe_result`, preserves
prior announcement fields untouched, but `render.py`'s `_firmware_cell`
(ticket 001) reads `STATE`, not the stale `device_name`/`role` fields, so
once `STATE` correctly reads known-blank the stale text is never shown
regardless of what's still sitting in those preserved columns.

**`peering.py` gap (ticket 001 handoff item #2) — real, fixed.** Traced
and confirmed a real gap, not just a hygiene nit: `_apply_snapshot_device`
and `_apply_event`'s `EVENT_IDENTITY` branch each only recognized wire
values `STATE_CONNECTED` and `STATE_CONNECTED_NO_FIRMWARE` — a peer
reporting the new `STATE_ATTACHED_NO_ANNOUNCE` matched neither branch and
was silently dropped (the existing `test_apply_event_identity_no_firmware`
test's own docstring said as much: "peering._apply_event itself is
unmodified by sprint 007"). Separately, the existing
`STATE_CONNECTED_NO_FIRMWARE` branch called `store.apply_remote_probe(uid,
None)` — which, since ticket 001 narrowed what that call's `None` branch
means, now sets `STATE_ATTACHED_NO_ANNOUNCE` instead, silently
*downgrading* a peer's genuinely-known-blank board to didn't-announce.
Fixed minimally: both functions now also recognize
`STATE_ATTACHED_NO_ANNOUNCE` (applied via `apply_remote_probe(uid, None)`,
unchanged semantics) and route `STATE_CONNECTED_NO_FIRMWARE` through
`store.apply_known_blank(uid)` instead (no new "remote" store method
needed — `apply_known_blank`, like `apply_probe_result`, is already
host-agnostic; it never reads/writes `host`). No change to the publish
side (`publish_daemon_event`) — it already forwards `record.state`
verbatim, so a peer's own local `STATE_CONNECTED_NO_FIRMWARE`/
`STATE_ATTACHED_NO_ANNOUNCE` rows were already being sent correctly; only
the *receiving* side had the gap. `_snapshot_payload`/event payloads do
**not** carry `chip_identity_name`/`chip_identity_serial` — propagating a
peer's cached chip identity across the wire is out of this ticket's scope
(not mentioned in the ticket text) and is flagged here for a future
ticket if cross-peer SWD-identity display is wanted; today a peer's own
`NAME` column simply won't show a chip-identity fallback for a
remote-owned row, only for one this host physically probed itself.

**Test-hazard found and fixed (not in the ticket text, but required by
its own "no real hardware in the automated suite" criterion): `pyocd` is
a real, installed dependency in this project's venv (`pyproject.toml`,
confirmed via `uv run python -c "import pyocd"`), not merely a declared
one absent from CI.** Without a `chip_identity_session_factory` override,
any daemon test whose probe comes back silent (or malformed) for a uid
with no cached chip identity would — after this ticket's `_maybe_probe`
change — fall through to `identity.read_device_id`'s real-pyOCD default
path and call `ConnectHelper.session_with_chosen_probe` for real, exactly
what CLAUDE.md's standing hardware rule and this ticket's own acceptance
criteria forbid. Audited every `Daemon(...)`/`assemble_daemon_and_api(...)`
construction site in the test suite (`test_daemon.py`,
`test_daemon_claims_integration.py`, `test_deploy_cli.py`,
`test_deploy_cli_remote.py`, `test_cli_claims.py`, `test_cli_run.py`,
`test_cli_run_windows.py`, `test_cli_run_peering.py`) and: added
`mbtools.testing.fakes.unavailable_chip_identity_session_factory` (always
raises, so `read_device_id` cleanly returns `None`) and
`fake_chip_identity_session_factory`/`FakeChipIdentitySession` (a
successful, scriptable fake) as new shared, importable fakes; gave
`test_daemon.py`'s own `_make_daemon` helper a protective
`kwargs.setdefault("chip_identity_session_factory",
unavailable_chip_identity_session_factory)` so every test using it is
covered by default; patched the handful of direct `Daemon(...)`/
`assemble_daemon_and_api(...)` call sites elsewhere the same way (most
were already safe by construction — every scripted announcement queue
exactly matched the number of probes that would run — but
`test_cli_claims.py`'s
`test_assemble_daemon_and_api_omitted_claim_fn_keeps_default_no_op_behavior`
genuinely was at risk: default claim grants, no `serial_factory` given,
so the real (nonexistent) port open fails, `identity.probe` returns
`None`, and pre-fix this would have gone on to open a real pyOCD
session). `assemble_daemon_and_api`/`Daemon` both gained the new
`chip_identity_session_factory` parameter (default `None`, meaning
"use pyOCD for real" — production `mbregistry run` is unaffected and
still gets real hardware access; only tests need the override).
