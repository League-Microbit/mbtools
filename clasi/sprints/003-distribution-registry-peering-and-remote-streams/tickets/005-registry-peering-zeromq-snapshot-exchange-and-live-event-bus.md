---
id: '005'
title: 'registry.peering: ZeroMQ snapshot exchange and live event bus'
status: open
use-cases: [SUC-001, SUC-002, SUC-005]
depends-on: ['001', '004']
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.peering: ZeroMQ snapshot exchange and live event bus

## Description

Per sprint.md's Architecture (module `registry.peering`, Decisions 3 and
5), add the ZeroMQ half of peering on top of ticket 004's mDNS discovery:
a PUB socket every registry runs (publishing its own local
attach/detach/identity-change events, from hooking into `daemon`'s
existing pipeline, plus lock-acquire/lock-release *display* events from
`locks`), a REQ/REP snapshot endpoint a newly-discovered peer queries
once before subscribing, and applying an incoming peer's snapshot/events
into `store` via ticket 001's `upsert_remote_attached`/
`mark_remote_detached`/`apply_remote_probe`/`apply_remote_lock_state`.
Also implements the peer-vanish policy (Decision 5): on a detected link
drop, `store.mark_peer_unreachable(host)` — never a device-row mutation.

This ticket makes discovery (ticket 004) actually useful — until this
ticket, a discovered peer is recorded but nothing synchronizes with it.

Add `pyzmq` to `pyproject.toml`'s `dependencies`.

## Acceptance Criteria

- [ ] `registry.peering`'s class gains a PUB socket (bound to
      `--peer-pub-port`, default `7442`) and a REP socket (bound to
      `--peer-snapshot-port`, default `7443`), both started alongside
      the mDNS lifecycle from ticket 004.
- [ ] `daemon.Daemon` gains an injectable hook (mirroring its existing
      `flash_release_callback`-on-`LockManager` pattern) fired on every
      attach, detach, and identity-change (probe result applied) — wired,
      in `registry.cli`'s assembly (ticket 009), to publish an event on
      this module's PUB socket. This ticket adds the hook and the publish
      call; ticket 009 does the actual assembly wiring.
- [ ] `LockManager` gains a similar injectable "lock display changed"
      hook (fired on every successful `acquire`/`release`, carrying
      `kind` and a rendered display string — not the raw `HolderRef`,
      since a peer only needs the display, never the ability to act on
      it) — published on the same PUB socket as a `lock_state` event
      type, distinct from `attach`/`detach`/`identity`.
- [ ] On receiving a peer's snapshot (REQ to that peer's REP socket, sent
      once right after ticket 004 records a newly-discovered peer): every
      device in the snapshot is applied via
      `store.upsert_remote_attached`/`apply_remote_probe`, tagged with
      the peer's `host`.
- [ ] After the snapshot, this registry SUBs to the peer's PUB socket and
      applies each subsequent `attach`/`detach`/`identity`/`lock_state`
      event the same way, for the life of the peering relationship.
- [ ] A dropped peer link (ZMQ socket-level disconnect, not a guessed
      timeout — `zmq.EVENT_DISCONNECTED` monitor socket, or equivalent)
      calls `store.mark_peer_unreachable(host)`. A later reconnect (new
      mDNS advertisement or `--peer` retry) re-runs the snapshot
      exchange and calls `store.mark_peer_reachable(host)`.
- [ ] The REP socket handling a snapshot request never blocks the PUB
      socket's own event publishing (separate sockets, so this is true by
      construction — stated here as an explicit acceptance check, not
      just an implementation detail).
- [ ] `pyzmq` is added to `pyproject.toml`'s `dependencies`, confirmed to
      install cleanly on this project's target platforms (full hardware
      confirmation is ticket 014's job; a `uv sync` dry run here is
      enough for this ticket).

## Testing

- **Existing tests to run**: `tests/registry/daemon/`,
  `tests/registry/locks/` — the new hooks must not change either
  module's existing behavior when no hook callback is registered
  (defaulting to `None`, same convention as `flash_release_callback`).
- **New tests to write**:
  - Two real `peering` instances on loopback with distinct ports
    (no mDNS involved — construct them with an explicit peer address,
    reusing this ticket's own REQ/REP+PUB/SUB code path directly) proving
    snapshot-then-stream convergence end to end — this is the "at least
    one two-process integration test" sprint.md's Test Strategy calls
    for.
  - Peer-vanish: kill one side's sockets mid-test, assert the other
    marks it unreachable within a bounded wait; reconnect and assert it
    flips back to reachable after a fresh snapshot.
  - Attach/detach/identity/lock_state events each individually applied
    correctly into the receiving side's `store`.
- **Verification command**: `uv run pytest tests/registry/peering/`
