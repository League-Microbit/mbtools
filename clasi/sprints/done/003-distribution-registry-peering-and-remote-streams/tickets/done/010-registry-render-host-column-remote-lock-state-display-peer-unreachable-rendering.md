---
id: '010'
title: 'registry.render: HOST column, remote lock-state display, peer-unreachable
  rendering'
status: done
use-cases:
- SUC-002
- SUC-005
depends-on:
- '001'
- 009
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

- [x] `render_table`/`render_json` gain a HOST column — `"local"` (or
      blank, implementer's call, documented either way) for a `host IS
      NULL` row, the peer's hostname otherwise.
- [x] A remote-owned row's STATE/lock display reads
      `remote_lock_kind`/`remote_lock_display` instead of a live
      `LockManager.status` call (there is no live call to make for a
      device this registry doesn't own) — a local row's rendering is
      unchanged (still live `LockManager` state, via the API's existing
      `_device_dict` folding).
- [x] A row whose `host` is non-`NULL` and whose corresponding
      `peer.reachable` is false renders as "peer unreachable" (a distinct
      STATE cell value, not reused from the existing
      `attached_unprobed`/`connected`/`connected_no_firmware`/
      `disconnected` set, since none of those describe "we don't know,
      the link to its owner is down").
- [x] `find`/`get`'s JSON response for a remote-owned device includes an
      `endpoint` field (the owning peer's `host:remote_api_port`, from
      `store`'s `peer` table) — this is what ticket 011/012/013's client
      code needs to know where to connect for the actual remote
      operation (Decision 8).
- [x] `mbregistry list` and `mbdeploy list`'s output stay identical for
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

## Implementation Notes

- **`_api_base.BaseAPIServer._device_dict`** (shared by both `api.py` and
  `remote_api.py`) is where the live-vs-cache branch actually lives, not
  `render.py`: for `record.host is None` it behaves exactly as before
  (live `LockManager.status` call folded into `lock_kind`/`lock_pid`);
  for `record.host is not None` it skips that call entirely (`lock_kind`/
  `lock_pid` stay `None`) and instead folds in two new fields looked up
  from `store.get_peer(record.host)`: `endpoint` (`peer.endpoint`, or
  `None` if `host` names a peer with no matching `peer` row at all —
  shouldn't happen in steady state, but treated conservatively) and
  `peer_reachable` (`peer.reachable`, defaulting to `False` in that same
  missing-peer-row case, and to `True` unconditionally for a local row,
  since there's no peer link to lose). `remote_lock_kind`/
  `remote_lock_display` needed no new code — they were already carried
  through by the existing `asdict(record)` call, just never read by
  anything until now.
- **`render.py`**: `_state_cell` branches on `device.get("host")`. A
  remote row checks `peer_reachable` first — `False` renders "peer
  unreachable" unconditionally, before even looking at `state` or the
  lock cache (per the ticket's "regardless of its last-known state/lock
  cache"). Otherwise a remote row's lock line reads
  `remote_lock_kind`/`remote_lock_display` (`f"locked by {kind}
  {display}"` — reads naturally as "locked by serial pid 4821" when
  `remote_lock_display` happens to be `"pid 4821"`, i.e. the owning
  host's own holder was local; extends gracefully to `locks.py`'s other
  `_describe_holder` shape, `"session <ref> on <host>"`, for a
  holder that is itself remote to the owning host). A local row's
  `_state_cell` branch is untouched byte-for-byte from before this
  ticket. New `_host_cell` returns `device.get("host") or "local"`.
- **Column placement (the ticket's "implementer's call")**: HOST was
  inserted just *before* PORT, not appended at the end. Appending at the
  end first, then re-running the existing suite, surfaced a real
  conflict: `tests/registry/render/test_render.py`'s pre-existing `uses
  port and dash when absent` test asserts a rendered row *ends with* its
  PORT cell, which stops being true the moment anything is appended after
  it. Moving HOST to just before PORT keeps PORT the last column and
  that test (and any other "PORT is last" assumption) passing unmodified
  — chosen over editing that test, since the ticket's own Testing section
  asks for it to stay byte-for-byte unchanged for a local-only fleet.
- **Testing**: `uv run pytest tests/registry/render/ tests/registry/api/`
  — 56 passed, 1 skipped (pre-existing platform skip, unrelated). New
  tests: 8 in `tests/registry/render/test_render.py` (HOST header/cell,
  remote-row lock display formatting including a "never falls back to
  local lock_kind/lock_pid" check, remote unlocked/free, peer-unreachable
  wins over a stale `connected` state, peer-unreachable wins over a
  stale `disconnected`/"gone" state) and 6 in `tests/registry/api/
  test_api.py` (local row's `host`/`endpoint`/`peer_reachable` defaults,
  `find`'s `endpoint` absent-for-local vs. present-for-remote, list never
  makes a live lock call for a remote row, a `host` with no matching
  `peer` row defaults to unreachable, an explicitly `mark_peer_unreachable`d
  peer's row reports unreachable with its endpoint still shown). Full
  `tests/registry/` suite also run: 381 passed, 1 skipped, 1 unrelated
  pre-existing failure (`tests/registry/remote_api/test_remote_flash.py::
  test_flash_rejects_a_hex_path_not_staged_by_this_connection`, a known
  flake — tracked and fixed separately, not part of this ticket's scope).
  `tests/deploy/test_deploy_cli_list_build_debug.py` (the other
  `render_table`/`render_json` consumer) also run directly: 11 passed.
