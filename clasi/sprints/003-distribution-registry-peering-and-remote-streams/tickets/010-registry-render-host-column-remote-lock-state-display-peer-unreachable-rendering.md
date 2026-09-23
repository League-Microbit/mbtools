---
id: '010'
title: 'registry.render: HOST column, remote lock-state display, peer-unreachable
  rendering'
status: open
use-cases: [SUC-002, SUC-005]
depends-on: ['001', '009']
github-issue: ''
issue: mbregistry-peering-mdns-and-zeromq.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.render: HOST column, remote lock-state display, peer-unreachable rendering

## Description

Per sprint.md's Architecture (Decision 3, module `registry.render`),
`mbregistry list`/`mbdeploy list` need to show the combined fleet-wide
view UC-005 requires: a HOST column, a remote-owned row's lock display
read from `store`'s cached `remote_lock_kind`/`remote_lock_display`
fields (never a live cross-host call), and "peer unreachable" instead of
a stale live-looking state when `peer.reachable` is false for that row's
`host`.

This ticket only touches rendering and the `list`/`find` response shape
that feeds it — no new registry logic, since tickets 001/005 already
populate everything this ticket reads.

## Acceptance Criteria

- [ ] `render_table`/`render_json` gain a HOST column — `"local"` (or
      blank, implementer's call, documented either way) for a `host IS
      NULL` row, the peer's hostname otherwise.
- [ ] A remote-owned row's STATE/lock display reads
      `remote_lock_kind`/`remote_lock_display` instead of a live
      `LockManager.status` call (there is no live call to make for a
      device this registry doesn't own) — a local row's rendering is
      unchanged (still live `LockManager` state, via the API's existing
      `_device_dict` folding).
- [ ] A row whose `host` is non-`NULL` and whose corresponding
      `peer.reachable` is false renders as "peer unreachable" (a distinct
      STATE cell value, not reused from the existing
      `attached_unprobed`/`connected`/`connected_no_firmware`/
      `disconnected` set, since none of those describe "we don't know,
      the link to its owner is down").
- [ ] `find`/`get`'s JSON response for a remote-owned device includes an
      `endpoint` field (the owning peer's `host:remote_api_port`, from
      `store`'s `peer` table) — this is what ticket 011/012/013's client
      code needs to know where to connect for the actual remote
      operation (Decision 8).
- [ ] `mbregistry list` and `mbdeploy list`'s output stay identical for
      the same device set (this module's own existing "no
      `mbdeploy`-specific rendering code" invariant, unaffected by this
      ticket — both still call the same `render_table`/`render_json`).

## Testing

- **Existing tests to run**: `tests/registry/render/`,
  `tests/registry/api/` (the `list`/`find` response shape) — existing
  local-only rendering must be byte-for-byte unchanged when no remote/
  peer data is present (a `host IS NULL` fleet looks exactly as it did
  before this sprint).
- **New tests to write**:
  - A row with `host` set and `peer.reachable=true` renders with the
    right HOST cell and cached lock display.
  - A row with `host` set and `peer.reachable=false` renders "peer
    unreachable" regardless of its last-known state/lock cache.
  - `find()`'s JSON response for a remote row includes `endpoint`; for a
    local row it's absent/`null`.
- **Verification command**: `uv run pytest tests/registry/`
