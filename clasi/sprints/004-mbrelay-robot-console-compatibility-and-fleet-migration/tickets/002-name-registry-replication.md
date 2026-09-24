---
id: '002'
title: Name registry replication
status: open
use-cases: [SUC-004]
depends-on: ['001']
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Name registry replication

## Description

Extend `registry.peering`'s existing ZMQ event bus so `name_registry`
rows converge across peers the same way `device`/`peer` rows already do
(sprint architecture Decision 2). A robot tuned or looked up on one host
must resolve identically on every peered host.

**Approach**
- Publish a `name_registry` set/clear event on the existing PUB socket
  whenever ticket 001's `Store.set()`/`resolve()` (on derive) / `clear()`
  is called, alongside the existing attach/detach/identity-change events.
- Apply an incoming `name_registry` event into the local `Store` exactly
  like an incoming device event is applied today.
- Add `name_registry` rows to the existing REQ/REP snapshot payload so a
  newly-joining peer catches up in the same call that already hands it
  `device`/`peer` rows (no new snapshot round-trip).
- No conflict-resolution logic needed for concurrent derives of the same
  unseen name: `naming.name_to_radio` is a pure deterministic function,
  so two hosts deriving the same name independently always agree.

**Files to create/modify**
- `src/mbtools/registry/peering.py` (extended — event publish/apply,
  snapshot payload).
- `tests/registry/test_peering.py` (extended).

**Documentation updates**: none beyond what's already covered by the
sprint architecture doc.

## Acceptance Criteria

- [ ] A `name_registry` `set` on host A publishes onto the PUB bus and is
      visible in host B's `Store` within one event-bus tick, with no
      polling on B's side.
- [ ] A `name_registry` `clear` on host A likewise propagates to B.
- [ ] A peer joining after entries already exist receives the current
      `name_registry` rows via the existing snapshot exchange (no
      separate name-registry-specific snapshot call).
- [ ] A peer-link drop degrades name-registry replication the same way
      it already degrades device-state replication (sets `peer
      .reachable = false`; existing rows are not deleted or reverted).
- [ ] Two peers independently deriving the same unseen name (e.g. two
      simultaneous `resolve()` calls before either has heard from the
      other) converge on the same value with no conflict/error surfaced.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_peering.py` (full existing peering suite — no
  regression to device/peer replication).
- **New tests to write**: two-registry-instance test (as sprint 003's own
  peering tests already do) asserting `name_registry` set/clear
  propagation and snapshot catch-up.
- **Verification command**: `uv run pytest tests/registry/test_peering.py`
