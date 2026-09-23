---
id: '003'
title: 'USB watch: PortWatcher interface and polling implementation'
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# USB watch: PortWatcher interface and polling implementation

## Description

Build `mbtools.registry.usbwatch`: the module that detects USB
attach/detach for micro:bit-shaped devices. Per sprint.md's Architecture
(module "usbwatch") and Design Rationale ("usbwatch ships only a polling
implementation in sprint 001, behind an interface"), this ticket defines
a `PortWatcher` interface and ships exactly one implementation —
`PollingPortWatcher`, wrapping pyserial's `comports()` — so a future
udev/netlink source (out of scope this sprint) can be added later
without touching any caller.

Port the VID:PID filtering logic from `mbdeploy`'s
`port_serial_map()` (`devices.py`), using the shared constant from
`mbtools.common` (ticket 002 puts it there first; if ticket 003 lands
its tests before ticket 002's code, coordinate the constant's location
via `mbtools.common` regardless of ticket completion order — both
tickets depend only on package scaffold ticket 001, not on each other).

`PortWatcher`'s interface is intentionally narrow: `scan() ->
dict[str, PortInfo]` (uid → port info), returning the *current* set of
matching devices. It does not diff against a previous scan or emit
events itself — that's `daemon`'s job (ticket 006), which calls `scan()`
on an interval and diffs the result against what it already knows. This
keeps `usbwatch` a pure, stateless snapshot source, easy to fake
(`FakeUSBSource` from ticket 001 implements the same interface) and easy
to test without any timing assumptions.

`PollingPortWatcher` is plain `comports()` — no platform-specific code —
so it runs unmodified on macOS too, which is what lets `mbregistry run`
work as a macOS dev convenience (per sprint.md's Open Questions #3)
without this ticket doing anything macOS-specific.

## Acceptance Criteria

- [x] `PortWatcher` is defined as an interface (ABC or `Protocol`) with
      at least `scan() -> dict[str, PortInfo]`, where `PortInfo` carries
      at minimum `port` (device path), `vid`, `pid`.
- [x] `PollingPortWatcher.scan()` returns only devices matching VID:PID
      `0x0D28:0x0204`, keyed by the DAPLink UID (pyserial's
      `serial_number`), mirroring `port_serial_map`'s filtering.
- [x] A `comports()` result with no matching device returns an empty
      dict, not `None` and not a raised exception.
- [x] `mbtools.testing.fakes.FakeUSBSource` (from ticket 001) satisfies
      the same `PortWatcher` interface, confirmed by a test that runs
      the same behavioral test cases against both `PollingPortWatcher`
      (with `comports()` monkeypatched) and `FakeUSBSource`.
- [x] No test in this ticket touches real USB hardware — `comports()`
      itself is monkeypatched/injected in every `PollingPortWatcher`
      test.

## Testing

- **Existing tests to run**: ticket 001's fakes tests.
- **New tests to write**: `PollingPortWatcher.scan()` with a
  monkeypatched `comports()` returning zero, one matching, one
  non-matching, and mixed device lists; a shared parametrized test
  suite run against both `PollingPortWatcher` and `FakeUSBSource` to
  prove interface parity.
- **Verification command**: `uv run pytest tests/registry/usbwatch/`
