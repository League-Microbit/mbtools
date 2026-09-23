---
id: '003'
title: 'registry: flash-count bookkeeping via mark_flashed op'
status: in-progress
use-cases:
- UC-008
depends-on:
- '001'
github-issue: ''
issue: mbdeploy-flash-by-name-via-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry: flash-count bookkeeping via mark_flashed op

## Description

Add a new wire-protocol op, `mark_flashed`, to `mbtools.registry.api`, and
a corresponding `mark_flashed(uid)` method to `mbtools.registry.client`
(ticket 001).

Per sprint.md's Design Rationale ("a new `mark_flashed` wire-protocol op,
rather than reusing `unlock` or extending `flash`"): sprint 002's
`mbdeploy` flashes locally by running pyOCD directly (ticket 007), not
through the registry's own `flash` op — so nothing calls
`store.increment_flash_count` for that path unless this ticket adds a
way to. `mark_flashed` closes that gap with the narrowest possible
change: same precondition as the existing `flash` op (a `flash`-kind
lock already held by *this* connection — checked via
`LockManager.status`, same as `flash`'s own precondition check), calls
`store.increment_flash_count(uid)`, returns `{"ok": true}`. No pyocd
invocation. No re-probe trigger of its own — that already happens on
any `flash`-kind lock release regardless of what ran before it (sprint
001's `LockManager`'s `flash_release_callback`, unconditional on the
releasing caller), so this op does not need to duplicate that logic.

Update `docs/design/registry-api.md`'s op table with the new row,
following the same documentation shape as the existing ops.

## Acceptance Criteria

- [x] `{"op": "mark_flashed", "uid": "..."}` requires a `flash`-kind lock
      already held by the calling connection; without one, responds
      `{"ok": false, "code": "not_locked", ...}` — same precondition and
      same error code as the existing `flash` op.
- [x] On success, `store.flash_count` for that uid is incremented by
      exactly one and the response is `{"ok": true}`.
- [x] Calling `mark_flashed` does **not** invoke pyocd and does not by
      itself trigger a re-probe (the existing lock-release hook is what
      does that, unaffected by this op).
- [x] `mbtools.registry.client.mark_flashed(uid)` (extending ticket 001's
      module) wraps this op the same way its other methods wrap
      `lock`/`unlock`/`find`.
- [x] `docs/design/registry-api.md` documents the new op in its existing
      table format (fields, response shape, error codes) as a new
      section alongside `flash`.
- [x] Every existing op's behavior, response shape, and error codes are
      unchanged (regression-checked by re-running sprint 001's full API
      test suite unmodified).

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/api/` (sprint
  001's full API test suite, must pass unmodified).
- **New tests to write**: precondition failure (no flash-kind lock held
  -> `not_locked`, mirroring the existing `flash` precondition test);
  success (flash-kind lock held by this connection -> `store.flash_count`
  increments by one, `{"ok": true}`); a lock held by a *different*
  connection is treated the same as no lock (still `not_locked`, per the
  existing "this connection's own lock" contract `flash` already
  enforces).
- **Verification command**: `uv run pytest tests/registry/`
