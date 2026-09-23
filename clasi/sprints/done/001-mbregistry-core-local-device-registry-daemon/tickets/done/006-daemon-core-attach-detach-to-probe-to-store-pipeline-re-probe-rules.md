---
id: '006'
title: 'Daemon core: attach/detach to probe to store pipeline, re-probe rules'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
depends-on:
- '002'
- '003'
- '004'
- '005'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon core: attach/detach to probe to store pipeline, re-probe rules

## Description

Build `mbtools.registry.daemon`: the orchestrator that wires
`usbwatch`, `identity`, `store`, and `locks` into the running service.
Per sprint.md's Architecture (module "daemon"), this module holds no
persistent state of its own — everything it touches lives in `store` or
`locks` — its job is the *pipeline* and the *re-probe policy*.

**Attach/detach pipeline** (SUC-001, SUC-002): on an interval, call
`usbwatch.scan()`, diff the result against what `store` currently shows
as attached, and for each newly-seen uid: `store.upsert_attached(...)`,
then (if `store.needs_probe(uid)` — ticket 004) call
`identity.probe(port)` and feed the result to
`store.apply_probe_result(...)`. For each uid that dropped out of the
scan: `locks.release` any lock held on it (a detach is not a graceful
release — see UC-002's postcondition "any lock is released"), then
`store.mark_disconnected(uid)`.

**Re-probe rule** (SUC-001/UC-001's core invariant, reinforced by
UC-002's error flow): a device already probed and still attached is
**never** reopened. This is why the pipeline above only probes when
`store.needs_probe(uid)` says so — `needs_probe` is true exactly when
the device is newly attached (never probed) or has just been marked
flash-pending (below). An already-`connected` device staying attached
across scans is never touched again.

**Flash-triggered re-probe** (SUC-003, sprint.md's Design Rationale "a
`flash`-kind lock's release is the re-probe trigger"): register a
callback with `locks` (ticket 005's flash-release notification) that,
on a `flash`-kind lock's release for a given uid, marks that uid as
flash-pending (a way to make `store.needs_probe(uid)` return true one
more time even though it was already probed) and waits for the device
to re-enumerate in the next scan or two before probing it — bounded by
a timeout, after which the device is marked `no-firmware`/blank per
UC-003's error flow, rather than waiting forever.

**Detach-vs-flash disambiguation** (SUC-002's note): a detach seen while
a `flash`-kind lock is (or was, within the flash-pending window) held on
that uid is treated as the expected flash-driven re-enumeration, not an
ordinary user unplug — this is exactly what "flash-pending" state exists
to distinguish. An ordinary unplug never has a flash-kind lock in play.

## Acceptance Criteria

- [x] A single `run()` (or equivalent) loop performs one scan-diff-probe
      cycle per interval; the interval is configurable (tests use a
      very short one or drive cycles manually rather than sleeping).
- [x] A newly-attached matching device gets exactly one probe (subject
      to `needs_probe`), never re-probed on a later cycle while it stays
      attached and unflashed — verified by a test that runs multiple
      cycles with the device present throughout and asserts the fake
      serial port's probe was invoked exactly once.
- [x] A detach releases any held lock and marks the record
      `disconnected`; a subsequent reattach (same uid reappears in a
      scan) is probed again (re-probe rule's "reattached" exception).
- [x] A `flash`-kind lock's release triggers exactly one re-probe of
      that uid, once it reappears in a scan — verified with
      `FakeUSBSource` scripted to drop and re-add the uid to simulate
      the flash-induced reboot.
- [x] A flash-triggered re-probe that never sees the device
      re-enumerate within a bounded timeout ends in a `no-firmware`/
      blank-equivalent state rather than hanging the daemon loop
      indefinitely.
- [x] The pipeline never opens a port for a device currently locked by
      someone else (a locked device is, by construction, either being
      actively used or mid-flash — either way not something the
      passive attach/detach pipeline should touch).
- [x] All tests run against `FakeUSBSource` and `FakeSerial` (ticket
      001) plus real `store`/`locks` instances (tickets 004/005) —
      no real USB, no real serial port, no real subprocess beyond the
      one already covered by ticket 005's liveness integration test.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/identity/
  tests/registry/usbwatch/ tests/registry/store/ tests/registry/locks/`
  — confirm the four modules this ticket composes still pass on their
  own before wiring them together.
- **New tests to write**: full attach→probe→connected cycle; no-op
  re-scan (no re-probe) while attached; detach releases lock and marks
  disconnected; reattach re-probes; flash-lock-release triggers exactly
  one re-probe on re-enumeration; flash-triggered re-probe timeout path.
- **Verification command**: `uv run pytest tests/registry/daemon/`
