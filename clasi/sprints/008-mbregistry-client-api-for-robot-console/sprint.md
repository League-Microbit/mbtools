---
id: 008
title: mbregistry client API for robot-console
status: roadmap
branch: sprint/008-mbregistry-client-api-for-robot-console
use-cases: []
issues:
- mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 008: mbregistry client API for robot-console

## Goals

Give robot-console everything it needs to become a native mbregistry
client — no ZeroMQ, no HTTP compatibility shims — for the parts of the
protocol that are still missing: change notifications, an operator-legible
lock display, a way to break a stale lock, and streaming over the local
socket as well as the remote TCP port.

## Problem

`docs/design/robot-console-integration.md` (§3, §5 items 2, 3, 7, 8) lays
out what robot-console needs from mbregistry to drop its direct-USB /
mbrelay-HTTP path. Today's local-socket API (`src/mbtools/registry/api.py`,
dispatch table in `_api_base.py`) has no `watch` or `stream` op — only
`remote_api.py` (the remote TCP port) has `stream`, added in ticket 007.
`locks.py`'s `HolderRef` carries `origin`/`ref`/`pid`/`host` but no
display label and no acquisition timestamp, so a student or operator
looking at `mbregistry list` or a `locked` reply has no way to tell who
holds a lock, for how long, or whether it looks stale — and no supported
way to clear a stale one short of killing the holding process.

## Solution

Extend the local-socket and remote-port JSON-op protocol (both currently
dispatched through the shared `_api_base.py` helpers) with:

- a `watch` op, on both the local socket and the remote port, that pushes
  JSON-lines events after the `ok` reply — the same event types the
  ZeroMQ PUB bus already emits (`attach`, `detach`, `identity`,
  `lock_state`, `name_set`, `name_clear`, `peer_up`, `peer_down`) — so a
  Node client gets change notifications without speaking ZeroMQ;
- a `stream` op on the local socket, mirroring the framed binary
  sub-protocol `remote_api.py` already implements for the remote port
  (`stream_frame.py`), so an in-process or same-host client can flash and
  read serial output without opening TCP;
- an optional, display-only `label` on `lock`, carried on `HolderRef`
  alongside a `since` timestamp, both surfaced in `locked.holder`, in
  `list`, and in `lock_state` events;
- `mbregistry unlock --force UID|NAME`, local-socket only, that drops a
  lock and closes the holder's connection or stream so the holder
  observes EOF rather than silently losing exclusivity — a manual,
  operator-only override with no automatic pre-emption and no equivalent
  on the remote TCP port.

`docs/design/registry-api.md` gets updated alongside the code as the new
ops and wire fields land, not as an afterthought.

## Success Criteria

- A `watch` client, local or remote, observes `attach`/`detach`/lock
  events without a ZeroMQ dependency.
- A `locked` reply and `mbregistry list` show the holder's `label` (when
  set) and `since`.
- `mbregistry unlock --force` on a board locked by a live client releases
  the lock and closes that client's connection/stream.
- `stream` over the local socket behaves identically (same framing, same
  precondition checks) to `stream` over the remote port.
- `docs/design/registry-api.md` reflects the new ops and wire fields.

## Scope

### In Scope

- `watch` op on the local socket (`api.py`) and the remote port
  (`remote_api.py`), reusing the existing PUB-bus event vocabulary.
- `label` on `lock`, added to `HolderRef` (`locks.py`) and surfaced in
  `locked.holder`, `list`, and `lock_state` events. Display only — never
  used for authorization or holder identity.
- `since` on the lock holder record, surfaced the same three places.
- `mbregistry unlock --force UID|NAME`: local-socket-only CLI op that
  drops the lock and closes the holder's connection/stream.
- `stream` op added to the local socket, matching the remote port's
  existing framed sub-protocol.
- Updating `docs/design/registry-api.md` for all of the above.

### Out of Scope

- Defaulting the HTTP/relay-pool compatibility shims off
  (`clasi/issues/default-mbregistry-compatibility-shims-off-once-robot-console-is-native.md`)
  — blocked on robot-console actually migrating, which is outside this repo.
- The robot-console-side implementation — a separate repository.
- Everything carried by sprint 007 (instance naming/`--instance`,
  multi-instance per-board claims, spawn flags such as `--ready-json`,
  `--exit-with-parent`, `--no-peering`). Sprint 008 depends on 007's
  instance naming and on the local socket that `--no-peering`-spawned
  instances expose, but does not redo that work.
- Any pre-emption between clients. `unlock --force` remains the only
  override, per the stakeholder decision in §7 of the design doc.

## Test Strategy

Unit/integration tests alongside each op, following this codebase's
existing per-op test pattern in `tests/registry/` (e.g. a `watch` test
asserting event delivery and ordering against a live `attach`/`detach`
sequence; a `label`/`since` round-trip through `lock` → `list` →
`locked`; an `unlock --force` test asserting the held connection observes
EOF; a local-socket `stream` test mirroring the existing remote-port
`stream` tests, reusing `stream_frame.py` framing helpers and the
`FakeSerial` test seam already used for ticket 007). No new
system-level/hardware test is anticipated — a spare board (per this
project's testing-against-real-hardware convention) is enough for any
manual verification. Full test-strategy detail (specific files, fixtures,
coverage targets) is deferred to detail planning.

## Architecture

(To be filled in at detail planning, once sprint 007 — instance naming
and the local socket for `--no-peering`-spawned instances — has landed.
This sprint is expected to size as *compact*: it extends the existing
local/remote API dispatch and `HolderRef` in place, with no new
cross-module dependency or dependency-direction change anticipated, but
that call is deferred to detail planning rather than made here.)

## Use Cases

(To be filled in at detail planning, once sprint 007 has landed.)

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|

Tickets execute serially in the order listed.
