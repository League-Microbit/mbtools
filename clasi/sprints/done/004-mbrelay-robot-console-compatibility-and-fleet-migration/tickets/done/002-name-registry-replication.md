---
id: '002'
title: Name registry replication
status: done
use-cases:
- SUC-004
depends-on:
- '001'
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

- [x] A `name_registry` `set` on host A publishes onto the PUB bus and is
      visible in host B's `Store` within one event-bus tick, with no
      polling on B's side.
- [x] A `name_registry` `clear` on host A likewise propagates to B.
- [x] A peer joining after entries already exist receives the current
      `name_registry` rows via the existing snapshot exchange (no
      separate name-registry-specific snapshot call).
- [x] A peer-link drop degrades name-registry replication the same way
      it already degrades device-state replication (sets `peer
      .reachable = false`; existing rows are not deleted or reverted).
- [x] Two peers independently deriving the same unseen name (e.g. two
      simultaneous `resolve()` calls before either has heard from the
      other) converge on the same value with no conflict/error surfaced.

## Implementation Notes

- The REP handler's snapshot reply shape changed from a bare device list
  (ticket 005) to `{"devices": [...], "names": [...]}` (this ticket) --
  the one existing snapshot round-trip now carries both tables, per the
  Approach's "no new snapshot round-trip" requirement.
  `_PeerLink._fetch_snapshot` now distinguishes a refusal (`{"error":
  ...}`, ticket 009's auth) from a valid reply by the presence of an
  `"error"` key rather than "is the reply a dict at all", since a valid
  reply is now also a dict.
- New event types `EVENT_NAME_SET = "name_set"` / `EVENT_NAME_CLEAR =
  "name_clear"`, applied in `_apply_event` and via the new
  `_apply_snapshot_name` (snapshot path). Both apply through
  `Store.set()`/`Store.clear()` only -- the two methods ticket 001 already
  exposes -- so this ticket needed no `store.py` changes.
- Incoming rows (event or snapshot) always land as `source=SOURCE_REGISTRY`
  on the receiving side, regardless of the origin row's own tier
  (`derived` vs `registry`): `Store.set()` is the only write primitive
  used to apply a replicated value, and it always persists
  `SOURCE_REGISTRY`. This is intentional, not an oversight -- the
  ticket's own Approach explains why no conflict-resolution/tier-fidelity
  logic is needed for a derived value (`naming.name_to_radio` is
  deterministic, so any two hosts asked to derive the same unseen name
  agree independently of replication). A future ticket that needs the
  origin `source` tier preserved through replication (none currently
  does) would need a new `Store` method that accepts an explicit
  `source`, not a `peering.py`-only change.
- `PeerDiscovery.publish_name_set(entry)` / `publish_name_clear(name)` are
  new public adapter methods, mirroring `publish_daemon_event`/
  `publish_lock_event`'s existing "call this right after your own local
  store write succeeds" shape. Neither is wired to a real call site by
  this ticket (`Store` has no event-callback hook of its own, matching
  the existing device/lock pattern where `Daemon`/`LockManager` -- not
  `Store` -- own the callback and invoke it after their own write) --
  ticket 005's `mbrelay` CLI and ticket 007's `names_api` are the
  callers that will invoke `publish_name_set`/`publish_name_clear` after
  their own `Store.set()`/`Store.resolve()`/`Store.clear()` calls.
- Tests added to `tests/registry/peering/test_peering_eventbus.py` (not
  `tests/registry/test_peering.py` -- the ticket's Testing section names
  the pre-restructure path; the actual location, established by ticket
  005, is `tests/registry/peering/{test_peering.py,
  test_peering_eventbus.py}`, and this ticket's tests join the latter
  since it's the module's existing home for ZeroMQ event-bus tests):
  pure `_apply_event`/`_apply_snapshot_name` logic tests (set, clear,
  overwrite, malformed-event drop, snapshot round-trip), plus four real-
  loopback-socket integration tests covering each acceptance criterion
  directly (set+clear propagation, late-joining-peer snapshot catch-up,
  survives-peer-vanish, and independent-derive convergence with no live
  link at all).

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_peering.py` (full existing peering suite — no
  regression to device/peer replication).
- **New tests to write**: two-registry-instance test (as sprint 003's own
  peering tests already do) asserting `name_registry` set/clear
  propagation and snapshot catch-up.
- **Verification command**: `uv run pytest tests/registry/test_peering.py`
