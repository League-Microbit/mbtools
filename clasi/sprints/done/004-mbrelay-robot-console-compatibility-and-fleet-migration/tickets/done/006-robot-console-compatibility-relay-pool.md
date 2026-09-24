---
id: '006'
title: 'robot-console compatibility: relay pool'
status: done
use-cases:
- SUC-002
depends-on:
- '003'
- '004'
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# robot-console compatibility: relay pool

## Description

Host the `_mbrelay._tcp` pool-port contract inside `mbregistry` itself
(architecture Decision 1), so robot-console (unmodified) continues to
get a freshly-reset, normalized relay per TCP connection — the
highest-blast-radius contract in this sprint (brief: breaking it
silently mistunes a moved robot). Verified against robot-console's own
source, not this sprint's paraphrase of the brief (Part 1 research:
`/Volumes/Proj/proj/robot-projects/robot-console/packages/host/src/`).

**Approach**
- New module `registry.console_compat.relay_pool`: advertises
  `_mbrelay._tcp` (mDNS instance label from hostname, SRV → the live
  pool-port socket's actual bound port, TXT `registry=<names_api port>`
  from ticket 007 — read back from the live socket, not blindly from
  config, matching the old advertiser's own documented practice).
- On every new TCP connection: pick a free **local** relay only (`role`
  contains `RELAY`/`BRIDGE`, `host IS NULL` — architecture Decision 5,
  no cross-host proxying), lock it (kind `relay`), reset/normalize via
  `relay.protocol.RelayControl` (ticket 003) over a
  `relay.channel.LocalRelayChannel` (ticket 004), then pump raw bytes
  bidirectionally between the socket and the channel.
- On disconnect: reset/normalize again, release the lock — this
  reset-by-reconnect is what makes a fresh TCP connection equivalent to
  "freshly reset relay in the command plane" per
  `docs/relay-server.md`'s documented model, and is why robot-console's
  own `relayBridger.ts` resets the relay on every candidate attempt
  rather than assuming it's already reset (confirmed in Part 1 research
  — this is a real bug `relayBridger.ts` already had to work around for
  the old `mbrelay`, so the new pool port must uphold the guarantee
  robot-console is relying on).
- New default pool port distinct from legacy `mbrelay`'s 8760 (Decision
  6) — always discovered via mDNS SRV, never hardcoded by robot-console
  (confirmed in Part 1 research: no fixed-host/port CLI flag exists
  there).
- No name lookup in this module — that's ticket 007, entirely separate.

**Files to create/modify**
- `src/mbtools/registry/console_compat/__init__.py` (new package).
- `src/mbtools/registry/console_compat/relay_pool.py` (new).
- `src/mbtools/registry/cli.py` (extended — `mbregistry run` assembles
  `relay_pool` alongside `daemon`/`api`/`remote_api`/`peering`, with an
  option to disable it if no local relay hardware is expected on a host).
- `tests/registry/console_compat/test_relay_pool.py` (new).

**Documentation updates**: `docs/wiki/` gets a short note that
`mbregistry` hosts robot-console compatibility (no machine specifics);
this ticket does not touch the internal Robot Garage wiki (no fleet
rollout yet — that's sprint 005).

## Acceptance Criteria

- [x] A raw TCP connection to the pool port receives the relay's boot
      banner first, in the command plane, already normalized (RAW250,
      frag off, echo off, P7, ch0/grp10) — verified by an integration
      test opening a real loopback socket against a fake device.
- [x] On disconnect, the relay is reset/normalized again before being
      offered to the next connection (reset-by-reconnect).
- [x] No local free relay: the pool port has nothing to offer (connection
      accepted then closed, or refused — decide and document one
      behavior; do not hang).
- [x] The mDNS advertisement's service type, SRV target, and TXT
      `registry=<port>` key exactly match what
      `discovery/mdnsDiscovery.ts`/`watchers/mdnsWatcher.ts` parse
      (`RELAY_SERVICE_TYPE = "mbrelay"` → `_mbrelay._tcp`, `registry` TXT
      key holding a decimal port string) — cross-checked directly against
      that source, not just this ticket's own assumption.
- [x] The pool port never serves a peer-owned (remote) relay.
- [x] This listener is exempt from the `--auth-token` check (architecture
      "Migration Concerns" note) — robot-console has no mechanism to
      authenticate.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/` (confirm
  `daemon`/`locks`/`store` assembly is unaffected by the new listener).
- **New tests to write**:
  `tests/registry/console_compat/test_relay_pool.py` — loopback TCP
  integration test against a fake local relay device, asserting banner
  order, normalization, and reset-by-reconnect; a separate test asserting
  the mDNS TXT/SRV shape.
- **Verification command**: `uv run pytest
  tests/registry/console_compat/test_relay_pool.py`

## Implementation Notes

Interface decisions later tickets (007 names HTTP API, 010 braeburn mDNS
fix, 011 real-hardware acceptance) depend on:

- **Fixed default ports, in this project's own 7440/7442/7443 sequence**
  (architecture Decision 6): `relay_pool.DEFAULT_POOL_PORT = 7444`,
  `relay_pool.DEFAULT_NAMES_API_PORT = 7445`. Both are only ever
  discovered live via mDNS SRV/TXT (Decision 6's own "no client hardcodes
  them"), so the exact numbers matter only for avoiding a collision with
  legacy `mbrelay`'s `8760`/`8761` during sprint 005's migration window —
  ticket 007's `names_api` module should reuse `DEFAULT_NAMES_API_PORT`
  from this module rather than re-declaring its own default, so the port
  this module advertises and the port `names_api` actually binds never
  drift apart.
- **`RelayPool` is a standalone, injectable class** (`store`/`locks`/
  `lock`/`control`/`channel_factory`/`zeroconf` all constructor
  parameters, mirroring `registry.peering.PeerDiscovery`'s own
  conventions), wired into `mbregistry run` by a *new*
  `registry.cli.assemble_relay_pool` function — **not** folded into
  `assemble_registry` itself, since that function's existing 4-tuple
  return (`daemon, api, remote_api, peering`) is unpacked by name in
  several pre-ticket-006 tests
  (`tests/registry/cli/test_cli_run_peering.py`); changing its arity
  would have broken every one of them. `assemble_registry` gained one
  new, purely-additive `lock: threading.RLock | None = None` parameter
  instead (mirroring `assemble_daemon_and_api`'s own already-established
  `lock` parameter) so `cmd_run` can build the shared
  `threading.RLock` itself, pass it into `assemble_registry`, and reuse
  the *same* lock for `assemble_relay_pool` — `registry.locks.
  LockManager` has no internal lock of its own (module docstring: every
  access must go through the one shared assembly lock), so `RelayPool`
  acquiring/releasing a `relay`-kind lock under a *different* lock object
  than `daemon`/`api`/`remote_api`/`peering` use would have been a real
  data race, not just an inconsistency.
- **`mbregistry run --no-relay-pool`** disables it (for a host with no
  local relay hardware); every other host gets it by default, matching
  `peering`'s own "always started, never opt-in" precedent. No
  `--relay-pool-port`/`--names-api-port` flags were added — out of this
  ticket's own scope (only "an option to disable it" was asked for) and
  easy to add later without touching `RelayPool` itself.
- **Holder identity for an in-daemon pool session**: `HolderRef(origin=
  "local", ref=f"console-compat-pool:{uuid4()}", pid=os.getpid())` — see
  `RelayPool._make_holder`'s own docstring for why neither existing
  origin fits verbatim (this session is neither a separate process with
  its own pid `api.py` can read via `SO_PEERCRED`, nor a peer registry's
  connection the way `remote_api.py`'s `"remote"` origin models). A
  future ticket adding liveness-sweep coverage for these holders (today
  none exists — a pool session's lock is always released deterministically
  in its own connection handler's `finally`, so no sweep is needed) would
  need to extend `LockManager.sweep`'s `is_alive` dispatch, which today
  only discriminates `"local"` vs `"remote"` at the two existing servers'
  own sweep loops; neither currently treats a pool-originated `"local"`
  holder specially (it is reported alive unconditionally, same as every
  other `"local"` holder), which is correct only because nothing sweeps
  a `relay_pool`-held lock today.
- **Three open/close cycles per TCP connection** on the local serial
  port: acquire's `reset_and_normalize(clear_stored=False)` (open ...
  close), the raw pump phase (`channel.open()` again — safe, since
  `LocalRelayChannel.open()` never resets — ... `channel.close()` when
  the client disconnects), then release's
  `reset_and_normalize(clear_stored=True)` (open ... close again).
  Deliberate, not an oversight: `relay.channel`'s own Implementation
  Notes (ticket 004) call this exact pattern "harmless" for
  `LocalRelayChannel` specifically (open/close cycles freely) — this
  module leans on that guarantee rather than porting `relay.cli`'s own
  "call `RelayControl`'s smaller building blocks directly, open once,
  close once" workaround, which exists there only because
  `RemoteRelayChannel` is single-use. `RelayPool` never constructs a
  `RemoteRelayChannel` (architecture Decision 5: local relays only), so
  that workaround's reason to exist does not apply here.
- **Reject message** (no free local relay): `# ERROR: no relay available
  ({total} devices, {busy} in use)\r\n`, then the connection is closed —
  the legacy pool's own comment-syntax answer
  (`docs/relay-server.md` §3), minus its "N being handed back" count
  (this module has no separate state for a board mid release-reset; it
  is simply still locked, and so already counted in `busy`).
- **`docs/wiki/` does not exist in this repository** (same situation
  ticket 005 already documented) — updated `docs/design/specification.md`
  §6.6/§6-open-decisions instead: marked "Where does the relay pool live"
  resolved, pointing at this module and its two default ports.
