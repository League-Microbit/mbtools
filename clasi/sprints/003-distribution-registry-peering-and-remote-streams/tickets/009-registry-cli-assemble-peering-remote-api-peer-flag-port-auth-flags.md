---
id: 009
title: 'registry.cli: assemble peering + remote_api, --peer flag, port/auth flags'
status: open
use-cases: [SUC-001, SUC-003, SUC-004]
depends-on: ['004', '005', '006', '007', '008']
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

- [ ] `assemble_daemon_and_api` (or a new sibling assembly function —
      implementer's choice, but the existing shared-`threading.RLock`
      pattern it establishes must extend to `remote_api` too, since it
      touches the same `store`/`locks`) is extended so `remote_api` and
      `peering` share the same lock as `daemon`/`api`.
- [ ] `mbregistry run` gains `--peer HOST[:PORT]` (repeatable, for more
      than one explicit peer): on startup, in addition to mDNS
      advertise/browse, directly connects to each given `HOST[:PORT]`'s
      peering endpoint (UC-014's flow — snapshot then stream, identical
      mechanism to an mDNS-discovered peer per sprint.md's Use Cases).
- [ ] `mbregistry run` gains `--remote-port` (default `7440`),
      `--peer-pub-port` (default `7442`), `--peer-snapshot-port` (default
      `7443`), each following the existing `--socket`/`--db` precedent
      (flag > env var > default; env vars `MBREGISTRY_REMOTE_PORT`,
      `MBREGISTRY_PEER_PUB_PORT`, `MBREGISTRY_PEER_SNAPSHOT_PORT`).
- [ ] `mbregistry run` gains `--auth-token` / `$MBREGISTRY_TOKEN`
      (Decision 6), forwarded to both `remote_api` and `peering`'s
      handshake; unset by default.
- [ ] `mbregistry run`'s startup log line (today: "listening on
      {socket_path}, store at {db_path}") is extended to also report the
      remote-API port and whether peering is active, so an operator
      running it in the foreground (macOS dev, per this module's existing
      "foreground dev use" doc note) can see at a glance whether peering
      came up.
- [ ] `mbregistry run`'s shutdown path (`finally: api.stop(); store.close()`)
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
