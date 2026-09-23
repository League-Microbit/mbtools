---
id: '001'
title: 'registry.client: shared local-socket client library'
status: open
use-cases: [UC-004, UC-006, UC-007]
depends-on: []
github-issue: ''
issue:
- mbdeploy-flash-by-name-via-mbregistry.md
- mbserial-raw-serial-access-local-or-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.client: shared local-socket client library

## Description

Create `mbtools.registry.client`, a typed library for talking to the
registry's Unix-socket API, so `mbdeploy` and `mbserial` don't each grow
their own copy of socket connect/framing/JSON code. Extract the private
`_connect`/`_request` helpers already living in `mbtools.registry.cli`
(sprint 001, ticket 009) into this new module, then refactor
`registry.cli` to call it instead of its own inline code — behavior must
not change, only where the code lives.

Per sprint.md's Architecture (module `mbtools.registry.client`), the
module owns:

- Socket connect (default `/run/mbregistry/api.sock`, overridable —
  mirror `registry.cli`'s existing `--socket`/env-var resolution) and
  newline-delimited-JSON framing.
- Typed wrappers for `list`, `find`/`get`, `lock`, `unlock` (per
  `docs/design/registry-api.md`'s existing op table — this ticket does
  **not** add `mark_flashed`; that's ticket 003, layered on top of this
  module once it exists).
- Mapping `{"ok": false, "code": ...}` responses to the stable `EXIT_*`
  constants (`mbtools.common`): `not_found` -> `EXIT_NO_DEVICE`, `locked`
  -> `EXIT_LOCKED`, `invalid_request` -> `EXIT_USAGE`. A `locked` response
  must preserve the `holder` dict (`kind`, `pid`) so a caller can format
  "locked for `<kind>` by pid `<pid>`" (UC-006's error flow / SUC-005).
- A `RegistryUnavailable` exception (socket file absent, or `ConnectionRefusedError`)
  distinct from the above — this is `EXIT_NO_DAEMON`'s trigger (UC-004's
  error flow), not a protocol-level error code.

This is the sprint's foundation ticket — every other client-tool ticket
(005-009) imports this module rather than talking sockets directly.

## Acceptance Criteria

- [ ] `mbtools.registry.client` exists with `list()`, `find(uid)`,
      `lock(uid, kind)`, `unlock(uid)` methods, each returning a plain
      Python value (list of dicts / dict) on success and raising a typed
      exception on failure — no raw socket or JSON leaks past this
      module's boundary.
- [ ] Protocol error codes (`not_found`, `locked`, `invalid_request`) map
      to distinct exception types (or one exception type carrying a
      `code` attribute) that a CLI can catch and translate to the
      matching `EXIT_*` constant without re-parsing strings.
- [ ] A `locked` failure's `holder` (`kind`, `pid`) is preserved on the
      raised exception, not discarded.
- [ ] Socket-not-present / connection-refused raises `RegistryUnavailable`,
      distinct from every protocol-level error.
- [ ] `mbtools.registry.cli`'s `list`/`run` commands are refactored to
      call this module instead of their own inline `_connect`/`_request`
      code; `mbregistry list`'s behavior and output are unchanged (same
      table, same `--json`, same exit codes) — verified by re-running
      sprint 001's existing CLI tests unmodified against the refactored
      code.
- [ ] Socket path resolution (default, `--socket` flag, env var
      override) matches `registry.cli`'s existing precedent exactly, so
      no caller's invocation changes.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/` (sprint
  001's full registry suite, including `tests/registry/cli/` — must pass
  unmodified against the refactored `registry.cli`, proving the
  extraction changed no behavior).
- **New tests to write**: protocol/dispatch tests against a real
  `AF_UNIX` socket in `tmp_path`, reusing sprint 001's
  `RegistryAPIServer` as the server side (list/find/lock/unlock success
  and every documented error code); a table-driven test asserting each
  `code` -> `EXIT_*` mapping; a `RegistryUnavailable` test against an
  absent socket path.
- **Verification command**: `uv run pytest tests/registry/`
