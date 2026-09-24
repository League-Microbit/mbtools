---
id: '001'
title: 'Fix: relay in data plane misidentified by re-probe'
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: relay-in-data-plane-can-be-misidentified-by-reprobe.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix: relay in data plane misidentified by re-probe

## Description

Fix the shared re-probe path (sprint architecture Impact/Design
Rationale Decision) so a relay board left transparently forwarding
radio traffic can never have its registry row overwritten by a
radio-forwarded fragment of another device's announcement — the bug
found by accident in sprint 004's hardware acceptance
(`docs/acceptance/004-hardware.md`, ticket 011: `togov`'s row briefly
showed as `gitev`/`NEZHA2`/robot while `togov` was mid-data-plane).

**This must land first in the sprint**, before any other ticket touches
`registry.daemon`/`registry.identity`, per team-lead scoping.

**Approach**
- `registry.identity.probe()` gains an optional keyword parameter (e.g.
  `reset_first: bool = False`). When true, assert a serial `BREAK`
  (`ser.send_break(...)`, mirroring `relay.protocol`'s and
  `serial.connect`'s existing BREAK usage — reuse
  `mbtools.serial.connect.BREAK_DURATION` for the duration constant
  rather than inventing a new one) immediately after `ser.open()`/before
  the existing settle delay and `reset_input_buffer()` call, so a board
  that's parked in the data plane is forced back into its own command
  plane before `HELLO` is ever written. This must not change behavior
  when `reset_first` is `False` (the default) — every existing caller
  and test is unaffected.
- `registry.daemon.Daemon._maybe_probe` passes `reset_first=identity
  .is_relay(record.role)` — using the uid's **pre-probe stored role**
  (read from the `Store` before calling `identity.probe`, the record
  already available via the same lookup `_maybe_probe`/`needs_probe`
  uses) — on **every** eligible probe, whether triggered by an ordinary
  reattach or by the flash-pending path. A `BREAK` is a safe superset in
  both cases: it is exactly the recovery a genuinely reattached relay
  needs, and harmless to a board that turns out, post-`BREAK`, to have
  actually been reflashed to a robot (BREAK is a DAPLink-bridge-level
  reset either way; a robot in early boot tolerates it the same as any
  other reset).
- Do **not** duplicate the "never probe a locked device" rule — it
  already exists (`daemon._maybe_probe`'s `self.locks.status(uid) is not
  None` check, sprint 001). This ticket only adds the BREAK-before-HELLO
  half of the issue's recommended fix.

**Files to create/modify**
- `src/mbtools/registry/identity.py` (`probe()` gains `reset_first`).
- `src/mbtools/registry/daemon.py` (`_maybe_probe` passes it, reading
  the pre-probe stored role).
- `tests/registry/identity/` (new/updated tests for `probe(reset_first=
  True)`).
- `tests/registry/daemon/test_daemon.py` (new regression test).

**Documentation updates**: none required beyond what sprint.md's
Architecture section already documents — this ticket implements that
design as written.

## Acceptance Criteria

- [x] `identity.probe(port, reset_first=True)` calls `ser.send_break(...)`
      before writing `HELLO`, using a `FakeSerial` whose `break_calls`
      list is empty until that point.
- [x] `identity.probe(port)` (default `reset_first=False`, and every
      existing call site/test) is byte-for-byte unchanged in behavior —
      no `send_break` call, same read-window/retry logic as before this
      ticket.
- [x] **Regression test reproducing the forwarded-announcement case**: a
      `FakeSerial` scripted with a stored `RADIOBRIDGE` role for the uid
      and a scripted `readline()` that would return a robot-dialect
      announcement even before any `HELLO` write (simulating
      radio-forwarded traffic already flowing) — the test asserts
      `send_break` is called before the first `write()`, proving the
      fix intercepts the ambiguous read.
- [x] A second regression test proves an ordinary (non-relay) device's
      reprobe is unaffected: no `BREAK` sent when the stored role does
      not contain `RELAY`/`BRIDGE` (or the uid has no prior stored role
      at all — first-ever probe).
- [x] `daemon._maybe_probe` passes `reset_first` based on the *uid's
      pre-probe stored role*, for both an ordinary reattach-triggered
      reprobe and a flash-pending reprobe — covered by a
      `test_daemon.py` case for each path.
- [x] The full scoped test run (`tests/registry/identity/
      tests/registry/daemon/`) passes with no regressions to any
      existing re-probe/reattach/flash-reprobe test.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/identity/
  tests/registry/daemon/` — every existing re-probe, reattach, and
  flash-triggered-reprobe test must still pass unchanged.
- **New tests to write**: the forwarded-announcement regression test and
  its non-relay counterpart (above), plus a direct `identity.probe`
  unit test for `reset_first=True`/`False`.
- **Verification command**: `uv run pytest tests/registry/identity/
  tests/registry/daemon/`

## Implementation Notes

- `identity.is_relay()` already exists and is exactly what
  `docs/acceptance/004-hardware.md`'s own "Recommend a follow-up ticket"
  note calls for using — this ticket is that follow-up ticket.
- Do not read the store's role *after* the probe to decide whether to
  reset — that would be circular (the whole bug is that the post-probe
  role can be wrong). The `reset_first` decision must be made from the
  record as it stood *before* this probe call.
- This ticket touches the daemon's shared, heavily-tested attach/reprobe
  pipeline (used identically for every device, robot and relay alike) —
  per `docs/acceptance/004-hardware.md`'s own caution, run the full
  scoped test suite, not just the new tests, before considering this
  done.

**Implemented (this ticket):**
- `identity.probe()` gained `reset_first: bool = False`. When true, it
  calls `ser.send_break(BREAK_DURATION)` immediately after `ser.open()`,
  before the settle delay and `reset_input_buffer()` — exactly the
  ticket's specified placement.
- **Deviation (forced by a real circular import, not a design choice)**:
  a top-level `from mbtools.serial.connect import BREAK_DURATION` in
  `identity.py` is circular — `serial.connect` imports
  `registry.client` → `registry.api` → `registry._api_base` →
  `registry.store`, and `store.py` imports `ProbeResult`/`short_uid`
  from `identity.py` at module scope. Resolved with a deferred
  (function-local) import of `BREAK_DURATION` inside `probe()`'s
  `reset_first` branch, rather than inventing a new duplicate constant.
  Verified both import orders (`import identity` first and `import
  serial.connect` first) succeed. Any later ticket adding more
  `registry` ⇄ `serial` cross-imports should be aware this cycle exists.
- `daemon._maybe_probe` reads `self._store.get(uid)` inside the existing
  first `with self._lock:` eligibility block (same lock scope as
  `needs_probe`/`locks.status`, no new lock taken), then computes
  `reset_first = identity.is_relay(pre_probe_record.role if
  pre_probe_record is not None else None)` outside the lock, before
  calling `identity.probe(..., reset_first=reset_first)`. This covers
  both the `needs_probe`-eligible (reattach) and `flash_pending`-eligible
  paths identically, and handles "no prior record at all" (first-ever
  probe) via `is_relay(None)` → `False`.
- New tests: `tests/registry/identity/test_identity.py` gained a
  `_OrderedFakeSerial` fake (records `send_break`/`write` calls in
  order, so a test can prove BREAK happened *before* the first write,
  not just that both happened) and three tests covering `reset_first=
  True`, the default-`False` no-op case, and the forwarded-announcement
  regression. `tests/registry/daemon/test_daemon.py`'s `_ProbeScript`
  gained a `fakes` list (every `FakeSerial` it hands out, so a test can
  inspect a specific probe call's `break_calls`) and four new tests:
  relay reattach, relay flash-pending, non-relay reattach, non-relay
  flash-pending — each proving BREAK is/isn't sent based on the uid's
  pre-probe stored role.
- Scoped run: `uv run pytest tests/registry/identity/
  tests/registry/daemon/` — 51 passed. Full suite: `uv run pytest -q` —
  783 passed, 2 skipped (pre-existing skips, unrelated to this ticket).
