---
id: '001'
title: 'Fix: relay in data plane misidentified by re-probe'
status: open
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

- [ ] `identity.probe(port, reset_first=True)` calls `ser.send_break(...)`
      before writing `HELLO`, using a `FakeSerial` whose `break_calls`
      list is empty until that point.
- [ ] `identity.probe(port)` (default `reset_first=False`, and every
      existing call site/test) is byte-for-byte unchanged in behavior —
      no `send_break` call, same read-window/retry logic as before this
      ticket.
- [ ] **Regression test reproducing the forwarded-announcement case**: a
      `FakeSerial` scripted with a stored `RADIOBRIDGE` role for the uid
      and a scripted `readline()` that would return a robot-dialect
      announcement even before any `HELLO` write (simulating
      radio-forwarded traffic already flowing) — the test asserts
      `send_break` is called before the first `write()`, proving the
      fix intercepts the ambiguous read.
- [ ] A second regression test proves an ordinary (non-relay) device's
      reprobe is unaffected: no `BREAK` sent when the stored role does
      not contain `RELAY`/`BRIDGE` (or the uid has no prior stored role
      at all — first-ever probe).
- [ ] `daemon._maybe_probe` passes `reset_first` based on the *uid's
      pre-probe stored role*, for both an ordinary reattach-triggered
      reprobe and a flash-pending reprobe — covered by a
      `test_daemon.py` case for each path.
- [ ] The full scoped test run (`tests/registry/identity/
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
