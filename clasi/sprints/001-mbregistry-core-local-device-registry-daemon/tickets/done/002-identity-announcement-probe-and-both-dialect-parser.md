---
id: '002'
title: 'Identity: announcement probe and both-dialect parser'
status: done
use-cases:
- SUC-001
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Identity: announcement probe and both-dialect parser

## Description

Build `mbtools.registry.identity`: the one module in mbregistry that
opens a serial port to read a board's announcement, and the one module
that parses that announcement. Per sprint.md's Architecture (module
"identity"), no other module in mbtools re-parses announcements — this
is why.

Port from `mbdeploy/src/mbdeploy/devices.py`:
- `probe_type(port, timeout_s=1.6)` — open with `dsrdtr=False,
  rtscts=False`, `dtr=False`, `rts=False` (DTR/RTS held low, per brief
  §3.2 step 4 / spec cross-cutting §4), send `HELLO\n` after a short
  settle delay, read lines until a match or the 1.6s window expires,
  then close. Both dialects must parse — see `devices.py`'s own
  docstring/comment block above the parsing branch (lines ~149-196) for
  the *exact* history of why both matter: the `device <role> ...`
  (robot) dialect was silently unparsed for a period and caused a real
  field failure (a reflashed board keeping a stale RADIOBRIDGE role).
  Do not regress that: both dialects need their own test case, not just
  the colon-delimited one.
- `port_serial_map()`'s VID:PID filter constant (`0x0D28, 0x0204`) —
  needed here too since `identity` validates a device is a micro:bit
  from USB data alone before it's worth probing (ticket 003 also uses
  this same constant for the watch side; put it in `mbtools.common` so
  both import the same literal instead of two copies drifting).
- `is_relay(role)` from `devices.py` — role contains `RELAY` or
  `BRIDGE`, case-insensitive.
- `short_uid` — `uid[16:24]`, per `mbrelay/inventory.py`'s
  `DeviceRecord.short_uid` property and its docstring explaining *why*
  that slice (DAPLink UID layout: `board(4) family(4) hic(8)
  unique(16) pad(8) hic(8)`; the last 8 hex chars are identical across
  every board, per brief cross-cutting concern 1).

Also port the `_port_holder`-style diagnostic from
`microbit-radio-relay/server/src/mbrelay/cli.py` (`_port_holder`,
~line 366): when opening a port fails because something else holds it,
shell out to `lsof -t <port>` then `ps -o command= -p <pid>` for each
holder PID, so the error note in SUC-001's "port fails to open" error
flow names the holding process instead of a bare `OSError`. This is a
best-effort diagnostic (swallow `lsof`/`ps` failures, fall back to
"another program") — never let it turn a probe failure into a crash.

## Acceptance Criteria

- [x] `identity.probe(port: str, timeout_s: float = 1.6) ->
      ProbeResult | None` opens with DTR/RTS low, sends `HELLO` if
      silent, and returns `None` on timeout (no exception) — mirroring
      `probe_type`'s contract.
- [x] Both announcement dialects parse into the same `ProbeResult`
      shape: `DEVICE:<role>:<common>:<name>:<serial>` and
      `device <role> <common> <name> <serial>`.
- [x] A malformed or unrecognized line does not crash the probe; it is
      treated as "no match," and the raw line is retained on the
      `ProbeResult`/caller side for diagnostics (per UC-001's "malformed
      announcement" error flow — role/name blank, raw line kept, not
      dropped).
- [x] `is_relay(role)` matches both `RADIORELAY` and `RADIOBRIDGE`
      case-insensitively; does not match a robot role (e.g. `NEZHA2`).
- [x] `short_uid(uid)` returns `uid[16:24]` for a well-formed 48-hex-char
      UID, and a documented fallback for a shorter/malformed UID (match
      `mbrelay`'s own fallback: last 8 chars).
- [x] A port-busy failure (open raises) produces a diagnostic string
      naming the holding process (best-effort via `lsof`/`ps`) or
      falls back to "another program" — never raises out of the probe
      call itself.
- [x] The VID:PID constant lives in one place (`mbtools.common`) and is
      imported here, not redefined.
- [x] Every test in this ticket runs against `mbtools.testing.fakes.
      FakeSerial` from ticket 001 — no real serial port opened.

## Testing

- **Existing tests to run**: ticket 001's fakes tests (`uv run pytest
  tests/testing/`) to confirm no regression in the fixtures this ticket
  depends on.
- **New tests to write**: table-driven parse tests for both dialects
  (including the historical robot-dialect regression case — a
  `device NEZHA2 robot <name> <serial>` line must parse, not just the
  `DEVICE:` form), a timeout-returns-None case, a malformed-line case,
  `is_relay` true/false cases, `short_uid` normal and short-UID cases,
  and a busy-port diagnostic test using a `FakeSerial` scripted to raise
  on open.
- **Verification command**: `uv run pytest tests/registry/identity/`
