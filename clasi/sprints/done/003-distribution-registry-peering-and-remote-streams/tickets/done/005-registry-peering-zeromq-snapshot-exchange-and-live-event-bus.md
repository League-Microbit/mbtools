---
id: '005'
title: 'registry.peering: ZeroMQ snapshot exchange and live event bus'
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-005
depends-on:
- '001'
- '004'
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

- [x] `registry.peering`'s class gains a PUB socket (bound to
      `--peer-pub-port`, default `7442`) and a REP socket (bound to
      `--peer-snapshot-port`, default `7443`), both started alongside
      the mDNS lifecycle from ticket 004.
- [x] `daemon.Daemon` gains an injectable hook (mirroring its existing
      `flash_release_callback`-on-`LockManager` pattern) fired on every
      attach, detach, and identity-change (probe result applied) — wired,
      in `registry.cli`'s assembly (ticket 009), to publish an event on
      this module's PUB socket. This ticket adds the hook and the publish
      call; ticket 009 does the actual assembly wiring.
- [x] `LockManager` gains a similar injectable "lock display changed"
      hook (fired on every successful `acquire`/`release`, carrying
      `kind` and a rendered display string — not the raw `HolderRef`,
      since a peer only needs the display, never the ability to act on
      it) — published on the same PUB socket as a `lock_state` event
      type, distinct from `attach`/`detach`/`identity`.
- [x] On receiving a peer's snapshot (REQ to that peer's REP socket, sent
      once right after ticket 004 records a newly-discovered peer): every
      device in the snapshot is applied via
      `store.upsert_remote_attached`/`apply_remote_probe`, tagged with
      the peer's `host`.
- [x] After the snapshot, this registry SUBs to the peer's PUB socket and
      applies each subsequent `attach`/`detach`/`identity`/`lock_state`
      event the same way, for the life of the peering relationship.
- [x] A dropped peer link (ZMQ socket-level disconnect, not a guessed
      timeout — `zmq.EVENT_DISCONNECTED` monitor socket, or equivalent)
      calls `store.mark_peer_unreachable(host)`. A later reconnect (new
      mDNS advertisement or `--peer` retry) re-runs the snapshot
      exchange and calls `store.mark_peer_reachable(host)`.
- [x] The REP socket handling a snapshot request never blocks the PUB
      socket's own event publishing (separate sockets, so this is true by
      construction — stated here as an explicit acceptance check, not
      just an implementation detail).
- [x] `pyzmq` is added to `pyproject.toml`'s `dependencies`, confirmed to
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

## Implementation Notes

- **Module**: all of this ticket's ZeroMQ code lives in the existing
  `mbtools/registry/peering.py` (single file, per ticket 004's own
  precedent of staying well short of needing a `peering/` package) — no
  new module. New pieces: module-level helpers `_snapshot_payload`/
  `_probe_result_from_dict`/`_apply_snapshot_device`/`_apply_event`
  (pure, socket-free, unit-tested directly); a new private `_PeerLink`
  class (one peer's SUB+REQ+monitor lifecycle, owned exclusively by
  `PeerDiscovery`); `PeerDiscovery` itself gains a `zmq` injectable
  constructor parameter (mirrors `zeroconf`'s own), PUB/REP sockets, a
  REP handler thread, `connect_peer()`, `publish_event()`, and the two
  adapter methods `publish_daemon_event()`/`publish_lock_event()` shaped
  to be passed directly as `Daemon`'s `event_callback` and
  `LockManager`'s `lock_display_callback` in ticket 009's assembly, with
  no glue code needed at that call site.
- **`_BrowseListener` (ticket 004) gained one addition**: an optional
  `on_peer_ready` callback, invoked after `record_peer_seen` succeeds
  when the discovered TXT record's `pub_port`/`snapshot_port` both parse
  — wired by `PeerDiscovery.start()` as `on_peer_ready=self.connect_peer`.
  `None` (the default) reproduces ticket 004's discovery-only behavior
  exactly, which is what every ticket-004 test still exercises unchanged
  (confirmed: `tests/registry/peering/test_peering.py`'s 28 tests all
  still pass with no edits).
- **Snapshot vs. event dict shapes are deliberately different** (see the
  new "ticket 005" comment block in `peering.py` right before
  `_snapshot_payload`): a snapshot device dict carries the full identity
  including `port`/`vid_pid` (needed to bootstrap a row from nothing); a
  live `identity` event never repeats `port`/`vid_pid`. Applying an
  `identity` event is *not* routed through the same "upsert with port"
  helper the snapshot path uses — it only calls
  `apply_remote_probe`/nothing, never `upsert_remote_attached` — because
  doing otherwise would silently null out an already-known `port`/
  `vid_pid` on every identity-only update. Caught while writing
  `test_apply_event_identity_connected_sets_announcement_fields`, which
  asserts the pre-existing `port` value survives an identity event with
  no `port` key at all.
- **Subscribe-before-snapshot ordering**: `_PeerLink.start()` connects
  and subscribes its SUB socket *before* sending the REQ snapshot
  request (the ZeroMQ "Clone pattern" ordering) — an event published in
  the gap between subscribing and the snapshot reply arriving is queued
  by the SUB socket's own buffer instead of lost. Verified by hand with a
  throwaway script before committing to the design (SUB connect+subscribe,
  REQ snapshot round-trip, PUB send, SUB recv, then a hard `Context.term()`
  on the publisher's side while polling the subscriber's monitor socket
  for `EVENT_DISCONNECTED` — all four steps behaved as expected on the
  dev Mac, confirming both the ordering and the vanish-detection
  mechanism before any test code was written against them).
- **Peer-vanish detection**: each `_PeerLink`'s SUB socket carries a
  `get_monitor_socket()` PAIR socket, polled on its own thread
  (`_LOOP_POLL_TIMEOUT_MS` = 200ms `RCVTIMEO`, not a busy loop) via
  `zmq.utils.monitor.recv_monitor_message`, for `zmq.EVENT_DISCONNECTED`
  — a real socket-level event, not a guessed timeout, per the
  acceptance criterion's explicit wording. Best-effort heartbeat options
  (`ZMQ_HEARTBEAT_IVL`/`TIMEOUT`/`TTL`, 2s/5s/6s) are also set on the SUB
  socket so a hard network drop (not just a peer's graceful `stop()`)
  still surfaces as `EVENT_DISCONNECTED` within a bounded time — every
  test in this ticket only exercises the graceful-close path (a real
  `EVENT_DISCONNECTED` fires immediately on `Context.term()`/socket
  `close()` regardless of heartbeats), so the heartbeat option's own
  behavior against a genuinely wedged/unplugged link is unverified by
  this ticket's test suite; flagged for whoever eventually does real
  hardware peering acceptance testing (ticket 014) as a "does this still
  detect a yanked ethernet cable within ~5s" check worth doing once real
  hardware is available.
- **`connect_peer(host, address, pub_port, snapshot_port)`** is the one
  method every path into a live peer link funnels through — the mDNS
  discovery callback, a future `--peer HOST:PORT` CLI flag (ticket 009),
  and this ticket's own direct-construction tests. It does *not* call
  `store.record_peer_seen` itself — that remains whichever caller's job
  it already was (ticket 004's `_BrowseListener` for the mDNS path; a
  future `--peer` flag's own resolution step for the manual path) per
  this ticket's own acceptance-criterion wording ("sent once right after
  ticket 004 records a newly-discovered peer"). A caller that skips
  `record_peer_seen` before calling `connect_peer` gets a live ZMQ link
  whose `mark_peer_unreachable`/`mark_peer_reachable` calls silently
  no-op (caught `KeyError`, logged) rather than crash, but the peer's
  reachability will never show up in `list_peers()` — this project's
  test file calls `store.record_peer_seen` explicitly before
  `connect_peer` in both socket-based tests to reproduce the real
  sequencing, and this note exists so ticket 009's `--peer` wiring
  doesn't skip the same step.
- **Reconnect after vanish** is exercised in
  `test_peer_vanish_marks_unreachable_and_reconnect_marks_reachable_again`
  using a *second* `PeerDiscovery` instance bound to fresh, distinct
  ports rather than restarting the original one on the same ports — this
  sidesteps any OS-level port-reuse/TIME_WAIT timing flakiness in the
  test itself while still exercising exactly what `connect_peer`'s own
  reconnect logic does (stop any existing link for that host, start a
  fresh one, re-run the snapshot exchange, mark reachable again).
- **`pyproject.toml`**: `pyzmq>=27.2` added to `dependencies`. Checked
  against PyPI's file listing for `pyzmq` 27.2.0 before pinning: it ships
  `cp312-abi3` wheels (stable-ABI, so they cover 3.12 through at least
  3.15) for `macosx_10_15_universal2` (covers macOS x86_64, this
  project's braeburn target) and
  `manylinux_2_26_aarch64.manylinux_2_28_aarch64` (covers Debian 13
  aarch64, this project's Nolanet targets) — no compiled-from-source
  install needed on either platform. Confirmed locally: `uv sync` on the
  dev Mac (macOS, Python 3.13.7) installed `pyzmq==27.2.0` from a
  pre-built wheel (no compile step), `zmq.zmq_version()` reports libzmq
  `4.3.5`. Full hardware confirmation on the Nolanet aarch64 hosts
  remains ticket 014's job, unchanged from ticket 004's own note about
  `zeroconf`.
- **Tests**: `tests/registry/peering/test_peering_eventbus.py` is new
  (basename checked unique across `tests/` before creating it, per the
  project's "no `__init__.py` packages" constraint) — 12 tests: 9 pure
  application-logic tests against a bare `Store` (no socket), plus 3
  socket-based tests using the real `pyzmq` package on loopback with a
  fake `zeroconf` module (so no real mDNS socket opens): the
  snapshot-then-stream convergence end-to-end test (covers attach,
  identity, lock_state, and detach live events plus the initial
  snapshot, all in one two-instance scenario), the explicit
  REP-never-blocks-PUB check (a deliberately slowed snapshot handler
  proven not to delay a concurrently published PUB event), and the
  peer-vanish/reconnect test. `tests/registry/daemon/test_daemon.py`
  gained 4 new tests for the `event_callback` hook (attach-then-identity
  ordering within one cycle, detach, the flash-reprobe-timeout give-up
  path, and an explicit "no callback registered is a no-op" case).
  `tests/registry/locks/test_locks.py` gained 10 new tests for
  `lock_display_callback` (acquire with a local holder's display, a
  remote holder's display, never receiving the raw `HolderRef`, release,
  every kind not just flash, not fired on a failed/no-op
  acquire/release, fired via `sweep()`, both callbacks firing
  independently for a flash release, and the no-callback-registered
  case).
- **Test run**: `uv run pytest tests/registry/peering/ tests/registry/daemon/
  tests/registry/locks/ -q` → 85 passed (28 existing peering + 12 new
  peering + 16 daemon (12 existing + 4 new) + 29 locks (19 existing + 10
  new)). Full suite `uv run pytest -q` → 412 passed, 2 skipped (up from
  ticket 004's 386 passed/2 skipped baseline by exactly the 26 new tests
  above) — no regressions.
- **No deviations** from the ticket's acceptance criteria. The
  `zmq`-module injectable constructor parameter and the
  `EVENT_ATTACH`/`EVENT_DETACH`/`EVENT_IDENTITY`/`EVENT_LOCK_STATE`
  string constants were added beyond what the acceptance criteria named
  explicitly — the former for consistency with `zeroconf`'s own
  injectable-dependency convention (even though every test in this
  ticket exercises it against real loopback sockets rather than a fake,
  per the Testing section's own guidance), the latter so the wire
  protocol's `"type"` string values have one place they're defined
  rather than being repeated as string literals at each call/dispatch
  site.
