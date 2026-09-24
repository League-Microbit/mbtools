---
id: '006'
title: 'robot-console compatibility: relay pool'
status: open
use-cases: [SUC-002]
depends-on: ['003', '004']
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

- [ ] A raw TCP connection to the pool port receives the relay's boot
      banner first, in the command plane, already normalized (RAW250,
      frag off, echo off, P7, ch0/grp10) — verified by an integration
      test opening a real loopback socket against a fake device.
- [ ] On disconnect, the relay is reset/normalized again before being
      offered to the next connection (reset-by-reconnect).
- [ ] No local free relay: the pool port has nothing to offer (connection
      accepted then closed, or refused — decide and document one
      behavior; do not hang).
- [ ] The mDNS advertisement's service type, SRV target, and TXT
      `registry=<port>` key exactly match what
      `discovery/mdnsDiscovery.ts`/`watchers/mdnsWatcher.ts` parse
      (`RELAY_SERVICE_TYPE = "mbrelay"` → `_mbrelay._tcp`, `registry` TXT
      key holding a decimal port string) — cross-checked directly against
      that source, not just this ticket's own assumption.
- [ ] The pool port never serves a peer-owned (remote) relay.
- [ ] This listener is exempt from the `--auth-token` check (architecture
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
