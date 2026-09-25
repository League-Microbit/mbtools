---
id: '001'
title: watch op and event bus (local socket + remote port)
status: done
use-cases:
- SUC-001
- SUC-005
depends-on: []
github-issue: ''
issue: mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# watch op and event bus (local socket + remote port)

## Description

Add a new `registry.eventbus` module (per sprint.md's Architecture Step 3)
that fans one produced event out to every currently-connected `watch`
client, decoupled from whether that same event also goes out over
ZeroMQ. Wire it in as the always-present second consumer of the four
callback hooks that today only ever reach `PeerDiscovery`:

- `Daemon.event_callback` (`attach`/`detach`/`identity`)
- `LockManager.lock_display_callback` (`lock_state`)
- `_api_base.BaseAPIServer._name_set_callback`/`_name_clear_callback`
  (`name_set`/`name_clear`)
- **New**: `peering.py`'s `_on_peer_reachable`/`_on_peer_unreachable`
  also publish `peer_up`/`peer_down` (new `EVENT_PEER_UP`/
  `EVENT_PEER_DOWN` constants) — these have never gone out on the PUB
  bus before; this is new production logic, not a rewire.

Add a shared `_op_watch`/`_handle_watch` pair to `_api_base.py`
(subscribe to `self._eventbus`, write each event as one JSON line until
the client disconnects), dispatched from both `api.py` (local socket)
and `remote_api.py` (remote TCP port). `registry.cli`'s assembly
constructs one `EventBus` unconditionally — including under
`--no-peering` — and wires the four hooks above via small adapter
closures that always publish to the bus and, only when a
`PeerDiscovery` was actually constructed, also call the existing
`publish_daemon_event`/`publish_lock_event`/`publish_name_set`/
`publish_name_clear`. This fan-out logic lives entirely in these
closures — `Daemon`/`LockManager`/`_api_base` keep their existing
single-callback constructor shape; see sprint.md's Design Rationale
(Decision 3) for why this avoids both a `Daemon`/`LockManager`
signature change and a `peering.py` <-> `eventbus.py` import cycle.

This is the foundational ticket for the sprint: label/since (ticket
002) extends what `lock_display_callback` carries through this same
pipeline, and `unlock --force` (ticket 003) also flows a release event
through it.

Update `docs/design/registry-api.md` for the new `watch` op and its
JSON-lines event shape (in the same ticket, per sprint.md's Solution
paragraph — not a separate docs pass).

## Acceptance Criteria

- [x] `registry.eventbus.EventBus` exists: thread-safe `subscribe()` /
      `unsubscribe()` / `publish(event: dict)`; no import of
      `registry.peering` or any socket/ZeroMQ module.
- [x] `{"op": "watch"}` on the local socket returns `{"ok": true}`
      followed by one JSON line per subsequent event
      (`attach`/`detach`/`identity`/`lock_state`/`name_set`/
      `name_clear`/`peer_up`/`peer_down`), for as long as the
      connection stays open.
- [x] The same `watch` op works identically on the remote TCP port
      (`remote_api.py`), dispatched through the same shared
      `_api_base._op_watch`/`_handle_watch`.
- [x] A `watch` client on a `--no-peering` instance still receives
      `attach`/`detach`/`lock_state`/`name_set`/`name_clear` (sourced
      from the daemon's own event hooks via the always-constructed
      `EventBus`, not a ZeroMQ subscription) and never receives
      `peer_up`/`peer_down` (an unpeered instance has no peers).
- [x] When peering *is* on, an existing peer still receives
      `attach`/`detach`/`identity`/`lock_state`/`name_set`/`name_clear`
      over its PUB socket exactly as before this ticket (the new bus is
      additive, not a replacement for `PeerDiscovery.publish_*`).
- [x] `peering.py`'s `_on_peer_reachable`/`_on_peer_unreachable`
      publish `peer_up`/`peer_down` to the event bus only (never onward
      over PUB to a third host — see sprint.md Step 3's "purely local
      notification" note).
- [x] A `watch` connection that disconnects is cleanly unsubscribed
      (no leaked queue, no exception logged on the next `publish`).
- [x] `docs/design/registry-api.md` documents the `watch` op and its
      event vocabulary.

## Implementation Notes

- `EventBus`'s subscriber set needs its own lock (this was flagged in
  architecture review as an implementation-level detail deliberately
  left out of sprint.md, which stays at module level).
- Mirror `_op_list`/`_op_find`'s existing `_api_base.py` placement and
  docstring style for `_op_watch`/`_handle_watch`.
- The adapter closures in `cli.py`'s assembly are the right place for
  unit tests proving "peering still gets called when on" and "the bus
  still gets called when peering is off" — see sprint.md Design
  Rationale, Decision 3.

**Implementation notes added during this ticket:**

- The per-event wire-payload shaping that `PeerDiscovery.publish_daemon_event`/
  `publish_lock_event`/`publish_name_set`/`publish_name_clear` already did
  inline was pulled out into four module-level pure functions in
  `peering.py` (`daemon_event_payload`/`lock_event_payload`/
  `name_set_payload`/`name_clear_payload`), so `cli.py`'s always-on
  event-bus closures build the *identical* dict those methods send over
  PUB without needing a live `PeerDiscovery`/PUB socket to ask. This
  keeps the two publish paths from drifting apart on field names; it's
  an internal refactor, not a wire or behavior change (existing
  `publish_daemon_event` suppression-of-disconnected-attach test still
  passes unchanged).
- `assemble_registry` gained a `chip_identity_session_factory` passthrough
  to `assemble_daemon_and_api` (which already had one). This was a
  pre-existing gap: no test built via `assemble_registry` had ever
  called `daemon.run_once()` before this ticket's own end-to-end `watch`
  test needed to (every prior `assemble_registry`-based test drove
  `daemon.locks`/callbacks directly). Without it, `run_once()`'s SWD
  fallback probe reaches real `pyocd` in tests, per this project's
  standing "no micro:bit-touching pyocd calls in tests" rule — fixed by
  forwarding the parameter and passing
  `mbtools.testing.fakes.unavailable_chip_identity_session_factory` from
  the new tests.
- `EventBus` events omit a `"host"` field only for nothing — every event
  the `cli.py` closures publish (including `peer_up`/`peer_down`, sourced
  directly from `peering.py`) carries `"host"`, matching the ZeroMQ PUB
  bus's own `publish_event` shape, so a watch client sees the same event
  shape regardless of transport or peering state.
- Windows (`api_windows.WindowsPipeAPIServer`) is unchanged by this
  ticket, matching sprint.md's own module list (6 modules: `locks`,
  `_api_base`, `api`, `remote_api`, `peering`, `cli` — no
  `api_windows`); `watch` is local-socket + remote-port only per the
  ticket title.
- Tickets 002-004 build on this foundation directly, per plan: no
  deviations found that would affect their scope. One thing to flag for
  002 (label/since on `LockStatus`): this ticket's `lock_event_payload`
  helper takes exactly `(uid, kind, display)`, matching
  `lock_display_callback`'s current signature — ticket 002 should extend
  that signature (and this helper) together if `label`/`since` need to
  ride along on the wire event too, rather than adding a second payload
  builder.

## Testing

- **Existing tests to run**: `tests/registry/` (full module — this
  ticket touches shared dispatch and every callback hook's wiring).
- **New tests to write**: `registry.eventbus` unit tests (subscribe,
  publish, unsubscribe, multiple subscribers); a `watch` test per
  transport asserting event delivery and ordering against a live
  `attach`/`detach`/`lock`/`unlock` sequence; a `--no-peering` test
  proving `watch` still works with no `PeerDiscovery` constructed; a
  `peer_up`/`peer_down` test against `peering.py`'s existing
  `on_reachable`/`on_unreachable` test seams.
- **Verification command**: `uv run pytest tests/registry/`
