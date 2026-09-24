---
id: '011'
title: 'Fix: local ownership wins over stale peer sync'
status: in-progress
use-cases: []
depends-on: []
github-issue: ''
issue: peer-sync-overwrites-ownership-of-locally-attached-devices.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Fix: local ownership wins over stale peer sync

## Description

`peer-sync-overwrites-ownership-of-locally-attached-devices.md`: found in
sprint 005 ticket 007 when `mbregistry` replaced the old `mbrelay` on
`torture`. `torture`'s relay pool reported **0 devices** even though its
relays were physically attached and its own daemon had scanned them as
local.

Root cause chain:
- `hodr` tested some of the same boards in earlier sprints and still
  holds them as its own local (`host IS NULL`) rows, now disconnected.
- `hodr` keeps re-publishing those stale rows — both in the snapshot
  exchange and on its live event bus — because `Store.snapshot_local_devices`
  (`src/mbtools/registry/store.py`, `WHERE host IS NULL`) sends every
  locally-owned row regardless of connection state, and the daemon's
  event publishers (`PeerDiscovery.publish_daemon_event`, wired to
  `Daemon`'s `event_callback` in `registry/cli.py`) fire on the same
  unconditional basis.
- On the receiving host, `Store._upsert_device`'s remote path
  (`store.py`, the `existing.state == STATE_DISCONNECTED` and the final
  `else` branch, both reached via `upsert_remote_attached` /
  `apply_remote_probe` with a non-``None`` `host`) does an unconditional
  `UPDATE device SET ... host = ? WHERE uid = ?`. It has no notion of
  "this uid is currently attached to *me*" — it just overwrites whatever
  row is there, including a row this host owns and has physically
  attached. This is reached from `registry.peering._apply_snapshot_device`
  (snapshot bootstrap) and `_apply_event` (live `attach`/`identity`
  events), both of which call `store.upsert_remote_attached(...)`
  unconditionally with the sending peer's `host`.
- `console_compat.relay_pool` only offers `host IS NULL` rows for the
  relay pool, so once a row is re-tagged with a remote `host` it silently
  drops out of `torture`'s own pool.

Boards move between hosts in real fleet use. Whenever a board moves, the
host it moved to can lose it to a stale remote claim from wherever it
used to live. A production relay host then reports an empty pool and
`robot-console` finds no relays — this is why it is being pulled into
sprint 005 out of process order, ahead of tickets 008/009.

## Approach

Implement the issue's fix points 1-3 as store/peering-level ownership
rules, point 4 as tests, and point 5 as a startup-time recovery pass.
All of the store-side changes belong in `_upsert_device` (the single
choke point both `upsert_attached` and `upsert_remote_attached` already
share) and in `snapshot_local_devices`; the peering-side changes belong
in `_apply_snapshot_device`/`_apply_event` in `peering.py` and in the
event publishers that feed them.

1. **Local wins** (issue fix point 1). In `Store._upsert_device`
   (`store.py` ~495-539), when `host` (the incoming remote host argument)
   is not `None` and the existing row is currently locally-owned
   (`existing.host is None`) *and* currently connected (`existing.state`
   is not `STATE_DISCONNECTED`), do not perform the `host=?` UPDATE —
   log a warning (uid, this host's identity, the rejected remote
   `host`) and return the existing record unchanged. This is the one
   choke point both `upsert_remote_attached` (called from
   `_apply_snapshot_device`/`_apply_event`'s `attach` branch) and
   `apply_remote_probe` route through, so gating it here covers both the
   snapshot and the live-event paths without duplicating the check in
   `peering.py`.
2. **Don't advertise stale ownership** (issue fix point 2). Change
   `Store.snapshot_local_devices` (store.py ~877-891) to send only rows
   that are currently connected (`state != STATE_DISCONNECTED`) —
   `WHERE host IS NULL AND state != ?`. Apply the same filter to what
   the daemon's live event publishers send: `PeerDiscovery.publish_daemon_event`
   (`peering.py` ~1466) already receives the `DeviceRecord` that changed;
   an `EVENT_ATTACH`/`EVENT_IDENTITY` publish for a record whose `state`
   is `STATE_DISCONNECTED` must not go out (a detach already publishes
   `EVENT_DETACH` separately, so this does not lose the disconnect
   notification — it only stops a disconnected row from being
   re-asserted as an active claim of ownership).
3. **Hand-over** (issue fix point 3). When a remote `attach`
   event/snapshot entry arrives for a uid this host has as local but
   *disconnected* (`existing.host is None and existing.state ==
   STATE_DISCONNECTED`), ownership moves to the remote host — let the
   existing unconditional `host=?` UPDATE apply in that case (this is
   the `existing.state == STATE_DISCONNECTED` branch already in
   `_upsert_device`; it already resets `state`/`last_probe`, which is
   correct for a genuine hand-over). When this host's own `usbwatch`
   later reports the same uid attached again locally
   (`Store.upsert_attached`, `host=None`), the existing "new record or
   `state == STATE_DISCONNECTED` → full reattach" branch already
   restores local ownership (`host` goes back to `NULL`) — verify this
   is in fact reached for a row whose `host` is currently a remote
   peer's name (i.e. `upsert_attached`'s call into `_upsert_device` must
   not skip the reattach branch just because `host` is being set back to
   `None` from a non-`None` value; read the existing/new-value diff
   carefully before assuming no change is needed here).
4. **Tests** (issue fix point 4). Add unit tests in
   `tests/registry/store/test_store_peer_host.py` and/or
   `tests/registry/peering/test_peering.py` /
   `tests/registry/peering/test_peering_eventbus.py` (match whichever
   file already covers `_upsert_device`/`_apply_snapshot_device`/
   `_apply_event`) covering:
   - the `torture`/`hodr` scenario: a locally-owned, connected row must
     survive a remote snapshot/event claiming the same uid (fix point 1);
   - a locally-owned, *disconnected* row must accept a remote claim
     (hand-over, fix point 3);
   - `snapshot_local_devices` and `publish_daemon_event` must not surface
     a disconnected local row as claimable (fix point 2);
   - the reverse move (a remote-owned row moving back to local when this
     host's own `usbwatch` reattaches it) and a ping-pong (local →
     remote → local) round-trip end with the uid owned by whichever side
     currently has it attached.
5. **Recovery** (issue fix point 5). Existing databases (`torture`,
   `hodr`, and any other host that picked up a bad `host` tag before this
   fix) must self-correct after a daemon restart with the fix applied,
   without anyone hand-editing or deleting DB files. Since `usbwatch`
   re-scans all currently-attached ports on daemon startup and calls
   `Store.upsert_attached` (`host=None`) for each, and point 3's "reattach
   restores local ownership" path already handles a row whose stored
   `host` is stale, no separate migration/backfill step should be
   needed — confirm this is actually true by tracing the startup scan
   path (`registry.daemon`/`registry.cli`'s assembly) end to end, and if
   it is not (e.g. a currently-attached-but-never-reattaching edge case),
   add the minimal explicit recovery pass needed and say so in this
   ticket's own notes when done.

**Files likely touched**: `src/mbtools/registry/store.py`
(`_upsert_device`, `snapshot_local_devices`), `src/mbtools/registry/peering.py`
(`_apply_snapshot_device`, `_apply_event`, `PeerDiscovery.publish_daemon_event`),
plus their existing test files. No schema change is expected — this is
ownership *policy* around the existing `host`/`state` columns, not a new
column.

## Acceptance Criteria

- [x] A remote upsert (snapshot or event) never overwrites a row that is
      currently locally-owned (`host IS NULL`) and connected — it is
      logged and dropped instead (fix point 1).
- [x] Snapshots (`snapshot_local_devices`) and live attach/identity
      events (`publish_daemon_event`) never advertise a disconnected
      local row as an active claim of ownership (fix point 2).
- [x] A remote `attach` for a uid this host holds as local-but-disconnected
      transfers ownership to the remote host; when this host's own
      `usbwatch` subsequently reattaches the same uid locally, local
      ownership is restored (fix point 3).
- [x] New tests cover the `torture`/`hodr` scenario (local-connected
      resists a stale remote claim), the reverse move (remote-owned row
      returns to local on local reattach), and a ping-pong (local →
      remote → local) (fix point 4).
- [x] Existing databases (e.g. `torture`, `hodr`) end up with correct
      ownership after a daemon restart running the fix, with no manual
      DB editing or deletion (fix point 5) — traced and confirmed (or a
      minimal explicit recovery step added if tracing shows it's
      actually needed).
- [ ] **Hardware verification on `torture`**: after deploying the fix,
      `torture`'s relay pool (`console_compat.relay_pool`) offers its own
      physically attached relays, and every host's `mbregistry list`
      shows those boards as `host=torture` (not `host=hodr` or any other
      stale owner). `torture` is an authorized test host for this
      ticket's hardware verification (see this sprint's scope
      reconciliation) — use its current relay pool (3 relays, after the
      stakeholder removed `getez`) as the concrete devices to check.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/store/
  tests/registry/peering/` — in particular
  `tests/registry/store/test_store_peer_host.py` (already covers
  `upsert_remote_attached`/`snapshot_local_devices`/remote-row semantics,
  including `test_snapshot_local_devices_excludes_remote_rows`) and
  `tests/registry/peering/test_peering.py` /
  `tests/registry/peering/test_peering_eventbus.py` (`_apply_snapshot_device`/
  `_apply_event`/publishing) — scoped run per this project's ticket-level
  testing rule; the full suite runs once at `close_sprint`.
- **New tests to write**: the four scenarios listed under Acceptance
  Criteria / Approach point 4 above (local-connected resists stale
  remote claim; local-disconnected accepts hand-over; reverse move;
  ping-pong), plus a test that `snapshot_local_devices`/
  `publish_daemon_event` exclude a disconnected local row — placed
  alongside the existing peer-host/ownership tests in
  `tests/registry/store/test_store_peer_host.py` and
  `tests/registry/peering/test_peering.py`.
- **Verification command**: `uv run pytest tests/registry/store/
  tests/registry/peering/`, followed by the hardware check on `torture`
  described in Acceptance Criteria (relay pool lists its attached
  relays; `mbregistry list` on every host shows `host=torture` for
  them).

## Implementation Notes

**Fix point 1** (`Store._upsert_device`, `src/mbtools/registry/store.py`):
added a new `elif` branch ahead of the existing `state == STATE_DISCONNECTED`
branch — `host is not None and existing.host is None and existing.state !=
STATE_DISCONNECTED` — that logs a warning and returns the existing record
unchanged, rejecting the remote claim. Ordered before the `DISCONNECTED`
branch so a genuine hand-over (existing row locally-owned but disconnected)
still falls through to the unconditional update, unaffected.

**Fix point 2**: `Store.snapshot_local_devices` now filters `WHERE host IS
NULL AND state != 'disconnected'`. `PeerDiscovery.publish_daemon_event`
(`src/mbtools/registry/peering.py`) now returns early (no publish) for an
`EVENT_ATTACH`/`EVENT_IDENTITY` call whose `record.state ==
STATE_DISCONNECTED`; `EVENT_DETACH` is never suppressed.

**Fix point 3**: verified, not changed — already correct. The `existing.state
== STATE_DISCONNECTED` branch in `_upsert_device` is unconditional and
untouched by point 1's new branch (which explicitly excludes it), so a
remote `attach` for a local-but-disconnected uid still hands over cleanly.
The reverse move (local `usbwatch` reattach while a row is remote-owned)
already worked before this ticket too: `upsert_attached` always passes
`host=None`, so point 1's rejection branch (which only fires for `host is
not None`) never applies to it, and the existing final `else` branch's
unconditional `host = ?` update already restores `host = NULL` — confirmed
with `test_upsert_attached_restores_local_ownership_after_remote_takeover`
and the ping-pong test, both passing without further code changes.

**Beyond the ticket's original fix-point list — a downstream gap found while
implementing point 1**: rejecting the `host` reassignment in
`_upsert_device` alone was not sufficient. `_apply_snapshot_device` and
`_apply_event`'s `detach`/`identity`/`lock_state` branches call
`mark_remote_detached`/`apply_remote_probe`/`apply_remote_lock_state`
*unconditionally by uid*, with no ownership check of their own — so even
after point 1 correctly refused to reassign `host`, a stale remote payload
(e.g. `hodr`'s own snapshot reporting `state: "disconnected"` for a row it
no longer owns, which is exactly the production shape: `hodr`'s local copy
of the board is itself disconnected) would still reach through the second
call and flip *our* locally-owned row's `state` to `disconnected` —
reproducing the reported "pool reports 0 devices" symptom by a different
path than the one point 1 closes. Added `peering._attributed_to(store, uid,
host)` (used by `_apply_event`'s `detach`/`identity`/`lock_state` branches)
and an equivalent post-upsert `record.host != host` check in
`_apply_snapshot_device`, both dropping (logged) a payload for a uid this
store doesn't currently attribute to the sending host — covering both
"locally owned" and, as a natural extension of the same principle, "owned
by some other peer". Covered by
`test_apply_snapshot_device_does_not_overwrite_local_connected_row`,
`test_apply_event_detach_does_not_disconnect_local_connected_row`,
`test_apply_event_identity_does_not_overwrite_local_connected_row`, and the
positive-case `test_apply_event_detach_applies_when_attributed_to_sender`.

**Fix point 5** (recovery, traced not implemented): confirmed no separate
migration/backfill step is needed. `Daemon.run_once` (`src/mbtools/
registry/daemon.py`) always calls `store.upsert_attached(uid, ...)` for any
uid `usbwatch.scan()` reports that isn't already in its `previously_attached`
set (`state != STATE_DISCONNECTED`). Tracing the actual `torture`/`hodr`
production shape: `hodr`'s own stale row is itself `disconnected`, so its
(pre-fix) unfiltered snapshot carries `state: "disconnected"` for it; on
`torture`, `_apply_snapshot_device` applied that via the (pre-fix)
unconditional `mark_remote_detached`, leaving `torture`'s hijacked row as
`host="hodr", state="disconnected"`. That state means the uid is *not* in
`previously_attached` on `torture`'s next daemon cycle, so `usbwatch`'s
rescan calls `upsert_attached(uid, ..., host=None)` →
`_upsert_device`'s existing `state == STATE_DISCONNECTED` branch (unchanged
by this fix) does the unconditional reattach, restoring `host = NULL` and
resetting `state`. No explicit recovery pass was added — a plain daemon
restart on `torture`/`hodr` running this fix is sufficient.

**Separate pre-existing defect found, out of this ticket's scope — flagged
for a follow-up ticket, not fixed here**: `Daemon.run_once`'s
`previously_attached` set (`store.list_devices()` filtered only by `state
!= STATE_DISCONNECTED`) does not filter by `host`, so it includes
*remote*-owned rows too. Any remote-owned uid not physically attached to
*this* host (i.e. essentially all of them) falls into `previously_attached
- current.keys()` every cycle, so the daemon calls `store.mark_disconnected`
on it and fires a bogus `EVENT_DETACH` claiming `host=<this host>` for a
uid it doesn't own — `_apply_event`'s `EVENT_DETACH` handling had no
ownership check before this ticket either, so every peer (including the
uid's actual owner) applied it. This ticket's new `_attributed_to` guard in
`_apply_event` now stops that bogus detach from being *applied* by
receivers, and the ownership-holding daemon's own next `run_once` cycle
self-heals (reattach branch, same as point 5's trace) — so it is a source
of continuous background churn (repeated spurious disconnect/reattach
cycles logged, and brief incorrect `state` flapping on receivers' mirrored
copies of foreign devices) rather than a lasting ownership bug, and it does
not affect this ticket's `host=`-column acceptance criteria. Not fixed here:
`daemon.py` is not in this ticket's listed files, and the ticket's
root-cause chain doesn't implicate it — recommend a new ticket to scope
`previously_attached` to `record.host is None`.

**Doc touch outside the listed files**:
`src/mbtools/registry/console_compat/relay_pool.py`'s
`_pick_free_local_relay` docstring said `snapshot_local_devices()` "includes
every local row regardless of state" — corrected to describe the new
disconnected-row exclusion, since it directly describes behavior of a
method this ticket changed. No logic change in that file; its existing
`state != STATE_CONNECTED` filter was already correct and unaffected.
