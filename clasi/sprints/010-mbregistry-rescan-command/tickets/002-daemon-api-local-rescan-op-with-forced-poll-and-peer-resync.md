---
id: '002'
title: 'Daemon/API: local rescan op with forced poll and peer resync'
status: open
use-cases: [SUC-001, SUC-002]
depends-on: ['001']
github-issue: ''
issue: mbregistry-rescan-command.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Daemon/API: local rescan op with forced poll and peer resync

## Description

Wire ticket 001's `Store.candidates_for_purge()`/`purge()` into a new,
local-only `rescan` op: lock-aware orchestration (skip and report any
locked uid), then — unless `dry_run` — trigger an immediate USB poll
and ask every reachable peer for a fresh snapshot, and publish removal
events on the local event bus.

Per sprint.md's Architecture (Step 3, Step 5, and the two
`force_unlock`-precedent Design Rationale entries): `_op_rescan` lives
in the **shared** `_api_base.BaseAPIServer` (not directly in `api.py`
the way `force_unlock` does) so both `api.py` (Unix socket) and
`api_windows.py` (Windows pipe) get it; `remote_api.py` never adds the
dispatch line, so it stays unreachable from the remote TCP control
plane. The "force an immediate poll"/"ask peers to resync" steps are
wired through two new optional constructor callables
(`poll_callback`, `peer_resync_callback`), following the exact
precedent `event_callback`/`lock_display_callback` already set for
"assembly module wires two otherwise-decoupled modules together" — no
new import from `_api_base.py`/`api.py`/`api_windows.py` into
`daemon.py`/`peering.py`.

**Important codebase-alignment note from the architecture review**:
`WindowsPipeAPIServer` never sets `self._eventbus` (eventbus support
was deliberately scoped out of the Windows pipe transport in an
earlier sprint). `_op_rescan` must look it up defensively
(`getattr(self, "_eventbus", None)`) and skip publishing when absent
— do not assume `self._eventbus` exists just because `BaseAPIServer`
declares it as a required attribute.

See sprint.md's Architecture section (Steps 3-6) for the full design,
including the component diagram and the three Design Rationale
entries this ticket implements.

## Acceptance Criteria

- [ ] `{"op": "rescan", "dry_run": <bool, optional, default false>}`
      is dispatched by both `api.RegistryAPIServer` and
      `api_windows.WindowsPipeAPIServer`, and rejected (same
      unrecognized-op response every other unknown op gets) by
      `remote_api.RemoteAPIServer`.
- [ ] `_op_rescan` computes candidates via
      `Store.candidates_for_purge()`, then excludes any uid currently
      locked (`self._locks.status(uid) is not None`) from the delete
      set — an excluded uid is reported in the response as skipped
      (with enough detail for the CLI's "skipped 1 locked" summary
      line), never passed to `Store.purge()`.
- [ ] On a non-`dry_run` call: `Store.purge()` is called with the
      locked-filtered uid/host sets, actually-removed rows are
      published on `self._eventbus` (when present — see the Windows
      note above) as new `device_removed`/`peer_removed` event types
      (payload shape: `uid`/`host` for a device, `host` for a peer,
      following `daemon_event_payload`'s existing "uid + whatever
      changed" shape), then `poll_callback()` and
      `peer_resync_callback()` are each called once if not `None`
      (both optional/no-op by default, matching the
      `event_callback`-style convention).
- [ ] On a `dry_run` call: no `Store.purge()` call, no event
      published, no `poll_callback`/`peer_resync_callback` call — the
      response reports the same candidate/skip sets a non-dry-run call
      would act on.
- [ ] Response shape carries enough structure for both the CLI's
      human summary and `--json` (e.g. `{"ok": true, "removed":
      {"devices": [...], "peers": [...]}, "skipped_locked": [...],
      "dry_run": bool}`).
- [ ] `api.RegistryAPIServer.__init__`/`api_windows.WindowsPipeAPIServer.__init__`
      both gain `poll_callback: Callable[[], None] | None = None` and
      `peer_resync_callback: Callable[[], None] | None = None`,
      forwarded into the shared base exactly like every other
      optional callback parameter these constructors already accept.
- [ ] `peering.PeerDiscovery` gains `resync_reachable_peers()` — for
      every currently-connected `_PeerLink` (i.e. every entry in
      `self._peer_links`, connected or not — a link's own
      `force_resync` no-ops if the link isn't started), triggers a
      fresh snapshot fetch on its own thread, mirroring
      `start_resync()`'s existing off-thread convention. Add the
      sibling `_PeerLink.force_resync()` (unconditional — does not
      require `self._link_dropped.is_set()` the way `resync()` does)
      that runs `_fetch_snapshot()` on its own thread, guarded by the
      same `_resync_guard` non-blocking acquire `resync()` already
      uses (so a `force_resync()` racing an in-flight drop-triggered
      `resync()` for the same link is a safe no-op, not a double
      fetch).
- [ ] `cli.assemble_daemon_and_api` gains `poll_callback`/
      `peer_resync_callback` parameters forwarded to whichever API
      server it constructs; `cli.assemble_registry` passes
      `poll_callback=daemon.run_once` and
      `peer_resync_callback=peer_discovery.resync_reachable_peers if
      peer_discovery is not None else None` through to it (so
      `--no-peering` leaves `peer_resync_callback` `None`, a pre-existing
      safe default, not a new failure mode).
- [ ] `daemon.py`'s `run_once` attach/detach diff is confirmed (by a
      new regression test, not just by reading the code) to be
      unaffected by a row `rescan` deletes out from under it: purging
      a row that is either locally-`disconnected` or not
      locally-owned can never remove a member of `previously_attached`
      (locally-owned, non-disconnected), so no `daemon.py` code change
      is needed — see sprint.md's Migration Concerns for the full
      argument this test is verifying.

## Implementation Plan

**Approach**: Implement `_op_rescan` in `_api_base.py` next to the
other shared ops (`_op_lock`/`_op_unlock`/`_op_mark_flashed`), using
`self._store`/`self._locks`/`self._lock` exactly as those do. Add the
two `BaseAPIServer` constructor-time attributes
(`self._poll_callback`, `self._peer_resync_callback`) — since
`BaseAPIServer` is a mixin with no `__init__` of its own, each
concrete subclass (`api.py`, `api_windows.py`) sets them in its own
`__init__`, same as every other optional-callback attribute today.
Add the two dispatch lines (`api.py`'s and `api_windows.py`'s own
`_dispatch_line`) — do **not** touch `remote_api.py`. Add
`PeerDiscovery.resync_reachable_peers()`/`_PeerLink.force_resync()` in
`peering.py`, then the two new `assemble_daemon_and_api`/
`assemble_registry` parameters and closures in `cli.py`.

**Files to modify**:
- `src/mbtools/registry/_api_base.py` — `_op_rescan`, new event
  payload builders (or inline dicts) for `device_removed`/
  `peer_removed`.
- `src/mbtools/registry/api.py` — dispatch line, constructor
  parameters.
- `src/mbtools/registry/api_windows.py` — dispatch line, constructor
  parameters.
- `src/mbtools/registry/peering.py` — `PeerDiscovery.resync_reachable_peers`,
  `_PeerLink.force_resync`.
- `src/mbtools/registry/cli.py` — `assemble_daemon_and_api`/
  `assemble_registry` parameter + wiring additions.

**Testing plan**:
- `tests/registry/api/` (or wherever `_op_lock`/`_op_unlock` are
  tested today — follow that file's fixture conventions): rescan
  purges gone/unreachable rows and leaves everything else; a locked
  uid is skipped and reported, never deleted; `dry_run` performs no
  mutation and triggers no callback; `poll_callback`/
  `peer_resync_callback` are each called exactly once on a real
  (non-dry-run, non-empty) rescan and never called on `dry_run`;
  `remote_api.RemoteAPIServer` rejects `{"op": "rescan"}` the same way
  it rejects any unknown op.
- `tests/registry/api_windows/`: the same rescan behavior over the
  Windows pipe transport, including the eventbus-absent case
  (`_op_rescan` must not raise `AttributeError` when
  `WindowsPipeAPIServer` has no `self._eventbus`).
- `tests/registry/peering/`: `resync_reachable_peers()`/
  `force_resync()` re-fetch a connected link's snapshot without
  requiring `link_dropped`; a call on a link racing an in-flight
  drop-triggered `resync()` doesn't double-fetch (reuses
  `_resync_guard`).
- `tests/registry/daemon/` (or `tests/registry/cli/` for the assembly
  wiring): the new regression test proving a rescan-purged row can
  never desync `previously_attached` (construct a `Daemon`, purge a
  disconnected/remote row via the store primitives directly, run
  `run_once()`, assert no spurious detach/attach).
- **Existing tests to run**: `uv run pytest tests/registry/api/
  tests/registry/api_windows/ tests/registry/peering/
  tests/registry/daemon/ tests/registry/cli/`.
- **Verification command**: same as above.
