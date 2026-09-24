---
id: 009
title: 'registry.cli: assemble peering + remote_api, --peer flag, port/auth flags'
status: done
use-cases:
- SUC-001
- SUC-003
- SUC-004
depends-on:
- '004'
- '005'
- '006'
- '007'
- 008
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.cli: assemble peering + remote_api, --peer flag, port/auth flags

## Description

Per sprint.md's Architecture (module `registry.cli`, Step 5), wire
`registry.peering` (tickets 004/005) and `registry.remote_api` (tickets
006/007/008) into `mbregistry run`, alongside the existing
`daemon`/`api` pairing `assemble_daemon_and_api` already builds. Adds the
CLI surface: `--peer HOST[:PORT]` (explicit peering, UC-014), `--remote-
port`, `--peer-pub-port`, `--peer-snapshot-port` (Decision 7's
configurable defaults), and `--auth-token`/`$MBREGISTRY_TOKEN` (Decision
6). This is the last ticket before the new subsystems are actually
reachable by a running `mbregistry run` — everything before this ticket
is built and unit-tested but not yet assembled into the real daemon
entry point.

## Acceptance Criteria

- [x] `assemble_daemon_and_api` (or a new sibling assembly function —
      implementer's choice, but the existing shared-`threading.RLock`
      pattern it establishes must extend to `remote_api` too, since it
      touches the same `store`/`locks`) is extended so `remote_api` and
      `peering` share the same lock as `daemon`/`api`.
- [x] `mbregistry run` gains `--peer HOST[:PORT]` (repeatable, for more
      than one explicit peer): on startup, in addition to mDNS
      advertise/browse, directly connects to each given `HOST[:PORT]`'s
      peering endpoint (UC-014's flow — snapshot then stream, identical
      mechanism to an mDNS-discovered peer per sprint.md's Use Cases).
- [x] `mbregistry run` gains `--remote-port` (default `7440`),
      `--peer-pub-port` (default `7442`), `--peer-snapshot-port` (default
      `7443`), each following the existing `--socket`/`--db` precedent
      (flag > env var > default; env vars `MBREGISTRY_REMOTE_PORT`,
      `MBREGISTRY_PEER_PUB_PORT`, `MBREGISTRY_PEER_SNAPSHOT_PORT`).
- [x] `mbregistry run` gains `--auth-token` / `$MBREGISTRY_TOKEN`
      (Decision 6), forwarded to both `remote_api` and `peering`'s
      handshake; unset by default.
- [x] `mbregistry run`'s startup log line (today: "listening on
      {socket_path}, store at {db_path}") is extended to also report the
      remote-API port and whether peering is active, so an operator
      running it in the foreground (macOS dev, per this module's existing
      "foreground dev use" doc note) can see at a glance whether peering
      came up.
- [x] `mbregistry run`'s shutdown path (`finally: api.stop(); store.close()`)
      is extended to also stop `remote_api` and `peering` cleanly, in an
      order that never leaves a socket bound after the process should
      have exited (matches the existing `api.stop()`'s own "join every
      handler thread before returning" discipline).

## Testing

- **Existing tests to run**: `tests/registry/cli/` — the existing
  `assemble_daemon_and_api`/`cmd_run` tests must keep passing with no
  peering/remote flags given (defaults: no `--peer`, mDNS still runs
  per ticket 004's always-on advertise/browse — confirm this is the
  intended default, not opt-in, per sprint.md's architecture; flag as an
  Open Question in the ticket if the implementer finds this ambiguous
  when they get here).
- **New tests to write**:
  - `mbregistry run --peer HOST:PORT` against a second real
    `mbregistry run` process (or an equivalent in-process pair) converges
    to a shared device view — this is the ticket that finally proves the
    whole assembled stack, not just each module in isolation.
  - Every new flag's precedence (flag > env var > default) tested the
    same way `--socket`/`--db` already are.
- **Verification command**: `uv run pytest tests/registry/`

## Implementation Notes

- **`assemble_registry`, a new sibling of `assemble_daemon_and_api`.**
  `assemble_daemon_and_api` (tickets 006-008) gained three new *optional*
  keyword args — `lock`, `event_callback`, `lock_display_callback` — all
  defaulting to `None`/a fresh `RLock`, so every pre-ticket-009
  caller/test (including its own `tests/registry/cli/test_cli_run.py`
  suite) is byte-for-byte unaffected. A new `assemble_registry` builds
  one shared `threading.RLock`, constructs `PeerDiscovery` first (with
  that lock, unstarted), then calls `assemble_daemon_and_api(lock=...,
  event_callback=peering.publish_daemon_event,
  lock_display_callback=peering.publish_lock_event)`, then builds
  `RemoteAPIServer` with the same lock — so all four objects
  (`Daemon`, `RegistryAPIServer`, `RemoteAPIServer`, `PeerDiscovery`)
  share one lock, per the acceptance criterion. `cmd_run` calls
  `assemble_registry` (not `assemble_daemon_and_api` directly).
- **`Daemon` gained a `lock_display_callback` constructor param**,
  forwarded straight into its own internal `LockManager(...)`
  construction (`daemon.py` previously only wired `flash_release_callback`
  there). This is what lets a lock acquired via `daemon.locks` — the
  exact `LockManager` every server in the assembly shares — publish onto
  `peering`'s event bus. Covered by
  `test_assemble_registry_lock_display_callback_reaches_peering`
  (real loopback ZMQ SUB, asserts the published message).
- **The known ticket-005 pitfall (`connect_peer` never called
  `store.record_peer_seen`) is fixed structurally, not just worked
  around at the call site.** `PeerDiscovery.connect_peer` gained an
  optional keyword-only `remote_port` argument: when given, it calls
  `store.record_peer_seen(host, f"{address}:{remote_port}")` itself,
  before establishing the link, so a caller with no prior recording step
  (`--peer`, which has no `_BrowseListener` doing this for it) is
  correct by construction. Omitting it (the default) reproduces ticket
  005's exact original behavior, so mDNS's existing tests are unchanged
  (`_BrowseListener` continues to call `record_peer_seen` itself before
  invoking `connect_peer` as `on_peer_ready`, passing no `remote_port`).
  `cmd_run`'s `--peer` loop always passes `remote_port`. New coverage:
  `test_connect_peer_with_remote_port_records_peer_before_connecting`
  (proves the fix) and
  `test_connect_peer_without_remote_port_reproduces_ticket_005_behavior`
  (documents/pins the unchanged default) in
  `tests/registry/peering/test_peering_eventbus.py`.
- **`--peer HOST[:PORT]`'s pub/snapshot ports**: sprint.md left "how a
  bare `HOST[:PORT]` learns them" as an implementer choice ("defaults, or
  query the peer's remote API/TXT-equivalent over TCP"). Chose defaults:
  a `--peer` target is assumed to be listening on *this* host's own
  resolved `--peer-pub-port`/`--peer-snapshot-port` values (not the
  hardcoded module constants) — i.e. a uniformly-configured fleet (every
  host left at the defaults, or every host given the same
  `--peer-pub-port`/`--peer-snapshot-port`) "just works" with a bare
  `--peer HOST`. A peer running non-matching ports cannot be reached via
  `--peer` and needs mDNS instead (which learns the real ports from that
  peer's own TXT record). `PORT` in `HOST[:PORT]` itself is the peer's
  *remote-api* port, defaulting to the fixed project default
  (`DEFAULT_REMOTE_PORT`, not this host's own `--remote-port`) when
  omitted — deliberately not coupled to this host's own remote-port
  configuration. Documented as a limitation, not solved further, per the
  ticket's own "implementer's choice" framing.
- **Peering is always-on, not opt-in** — resolving the Testing section's
  own flagged ambiguity. sprint.md's Solution/Architecture text describes
  mDNS advertise/browse as inherent ("each `mbregistry` advertises and
  browses"), not a flag-gated extra, and ticket 004 already built it that
  way with no on/off switch. `cmd_run` always starts `peering` (mDNS +
  ZMQ event bus); `--peer` only adds explicit peers *on top of* whatever
  mDNS finds, it never replaces it. No new CLI flag disables peering.
- **Peering handshake auth added** (`PeerDiscovery` had no `auth_token`
  support before this ticket, despite `remote_api`'s own auth already
  being fully implemented in ticket 006). New optional `auth_token`
  constructor param, threaded to `_PeerLink`/the REP loop: when set, the
  snapshot REQ/REP exchange carries `{"token": ...}` instead of ticket
  005's bare `b"snapshot"`, and a missing/mismatched token gets
  `{"error": "unauthorized"}` back (treated like a timeout — no
  `on_reachable`, no exception). When unset (default), the wire is
  unchanged from ticket 005, so every existing peering test still
  passes. `remote_api`'s own auth needed no code changes, only wiring
  the CLI flag's resolved value into `RemoteAPIServer(auth_token=...)`.
  New coverage: `test_snapshot_handshake_with_matching_auth_token_succeeds`
  / `test_snapshot_handshake_with_wrong_auth_token_is_rejected`. Also
  documented in `docs/design/registry-api.md`'s new "Peering snapshot
  handshake auth" section.
- **Shared lock, also extended to peering's own store writes.**
  `PeerDiscovery`/`_PeerLink` gained a `lock` constructor param (defaults
  to a private `RLock`, matching every other module here); the snapshot-
  apply loop, the live-event-apply call, and the REP handler's own
  snapshot read are now wrapped in `with self._lock:`. This is defense
  in depth/consistency with the rest of the assembly, not a correctness
  requirement peering has on its own (`Store` is already independently
  thread-safe — see `store.py`'s "Thread safety" note) — documented as
  such in `peering.py`'s constructor docstring so a future reader doesn't
  mistake it for load-bearing.
- **Systemd unit rendering (`render_systemd_unit`) left unchanged.**
  Checked whether new ports/flags needed a template change:
  `RuntimeDirectory=mbregistry`/`StateDirectory=mbregistry` already
  create `/run/mbregistry`/`/var/lib/mbregistry`, matching
  `DEFAULT_SOCKET_PATH`/`DEFAULT_DB_PATH` exactly, and every new port
  (7440/7442/7443) is a plain unprivileged TCP port needing no directory
  or capability the unit doesn't already grant. An operator wanting
  non-default ports/`--peer`/`--auth-token` edits `ExecStart=`/adds
  `Environment=` lines themselves, the same way any other flag not baked
  into the template already works today.
- **Testing**: `uv run pytest tests/registry/` — 370 passed, 1 skipped
  (pre-existing skip, unrelated to this ticket). New test files/additions:
  `tests/registry/cli/test_cli_run_peering.py` (15 tests: `--peer`
  spec parsing, flag/env/default precedence for all four new flags,
  `build_parser` argparse wiring, `assemble_registry`'s shared-lock and
  lock-display-callback wiring, and an in-process two-pipeline
  convergence test exercising the actual `cmd_run`-equivalent `--peer`
  path end to end including a `mbregistry list --json` check that both
  hosts' devices appear, host-tagged); four new tests appended to
  `tests/registry/peering/test_peering_eventbus.py` for the
  `connect_peer(remote_port=...)` fix and the peering handshake auth.
  Full project suite also run once: `uv run pytest` — 495 passed, 2
  skipped, no regressions.
- **Docs**: `docs/design/registry-api.md` gained two new sections
  ("Peering snapshot handshake auth", "`--peer`'s record-before-connect
  fix") plus an addendum to its existing "Cross-module concurrency ...
  resolved in ticket 009" bullet noting the lock now also spans
  `remote_api`/`peering`.
