---
id: '002'
title: 'registry.paths: cross-platform state-location resolution'
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: mbregistry-windows-platform-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.paths: cross-platform state-location resolution

## Description

New module `registry.paths`: the one place that knows where
`mbregistry`'s on-disk/OS-namespace state lives, per platform. Today
`registry.store.DEFAULT_DB_PATH` and `registry.api.DEFAULT_SOCKET_PATH`
are Linux-path literals; Windows needs `ProgramData`-rooted equivalents
(brief §9.2, spec Open decision #2) and a named-pipe name (which has no
filesystem path at all — it lives in the `\\.\pipe\` namespace).

**Approach**
- `default_db_path() -> Path`: returns `Path("/var/lib/mbregistry/
  devices.db")` unchanged on Linux/macOS (`sys.platform` not
  `"win32"`); on Windows returns a path under `%ProgramData%`
  (`os.environ.get("PROGRAMDATA", r"C:\ProgramData")`, e.g.
  `<ProgramData>\mbregistry\devices.db`).
- `default_socket_path() -> Path`: unchanged Linux/macOS behavior
  (`/run/mbregistry/api.sock`); on Windows this function is not
  meaningful (no Unix socket) — either raise clearly if called on
  Windows, or simply not be called there (ticket 005 decides which,
  based on how `cli.py`'s platform branch ends up shaped). Document
  whichever choice is made.
- `default_pipe_name() -> str`: Windows-only, returns the fixed named
  pipe path (e.g. `r"\\.\pipe\mbregistry"`). Callable on any platform
  (returns the string regardless — it's just a name, not I/O), so tests
  on macOS/Linux can exercise it too.
- Flag the Windows `ProgramData` default explicitly, in this module's
  docstring and in `docs/migration.md` (ticket 008), as an assumption
  for stakeholder confirmation per the brief's own open decision #2 —
  not a ratified path.
- Update `registry.store.DEFAULT_DB_PATH` and `registry.api
  .DEFAULT_SOCKET_PATH` to call through `registry.paths.
  default_db_path()`/`default_socket_path()` respectively, preserving
  today's exact values on every already-supported platform (no behavior
  change for Linux/macOS).

**Files to create/modify**
- `src/mbtools/registry/paths.py` (new).
- `src/mbtools/registry/store.py` (`DEFAULT_DB_PATH` sourced from
  `paths.default_db_path()`).
- `src/mbtools/registry/api.py` (`DEFAULT_SOCKET_PATH` sourced from
  `paths.default_socket_path()`).
- `tests/registry/paths/test_paths.py` (new — exercise both platform
  branches by monkeypatching/injecting the platform check, mirroring how
  `registry/api.py`'s own `default_peer_pid` platform dispatch is
  tested).

**Documentation updates**: none beyond this ticket's own docstrings —
sprint.md's Architecture/Migration Concerns already document the
ProgramData assumption; `docs/migration.md` (ticket 008) restates it for
the stakeholder.

## Acceptance Criteria

- [x] `paths.default_db_path()` returns exactly today's
      `/var/lib/mbregistry/devices.db` on Linux/macOS — verified by a
      test asserting `registry.store.DEFAULT_DB_PATH` is unchanged from
      its value before this ticket.
- [x] `paths.default_socket_path()` returns exactly today's
      `/run/mbregistry/api.sock` on Linux/macOS — same
      no-behavior-change guarantee for `registry.api.DEFAULT_SOCKET_PATH`.
- [x] `paths.default_db_path()` on a simulated Windows platform returns
      a path rooted at `%ProgramData%` (or its default fallback if the
      env var is unset).
- [x] `paths.default_pipe_name()` returns a fixed, documented named-pipe
      path string, callable on any platform.
- [x] Module docstring explicitly states the Windows `ProgramData` path
      is an assumption, not a stakeholder-confirmed decision.
- [x] Every existing test in `tests/registry/store/` and
      `tests/registry/api/` still passes unmodified (no behavior change
      on existing platforms).

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/store/
  tests/registry/api/`.
- **New tests to write**: `tests/registry/paths/test_paths.py` covering
  both platform branches for each function.
- **Verification command**: `uv run pytest tests/registry/paths/
  tests/registry/store/ tests/registry/api/`

## Implementation Notes

- Mirror the existing platform-dispatch style already in this codebase
  (`registry.api.default_peer_pid`'s `sys.platform` branch) rather than
  inventing a different pattern — consistency with an already-reviewed
  precedent.
- This module has no I/O and no dependency on `pyserial`/anything
  platform-library-specific — pure path/string computation — so it needs
  no lazy-import guard the way `usbwatch`/`identity` do for `pyserial`.

**Implemented (this ticket):**
- New `src/mbtools/registry/paths.py`: `default_db_path()`,
  `default_socket_path()`, `default_pipe_name()`, each dispatching on
  `sys.platform == "win32"` exactly mirroring `registry.api
  .default_peer_pid`'s existing platform-dispatch style. No circular
  -import risk from the note on ticket 001 (`store` importing `identity`
  at module scope) — `paths.py` imports nothing from `mbtools` at all,
  only `os`/`sys`/`pathlib`, so it can be imported from `store.py`,
  `api.py`, or anywhere else without regard to import order.
- `registry.store.DEFAULT_DB_PATH` and `registry.api.DEFAULT_SOCKET_PATH`
  now call through to `paths.default_db_path()`/`default_socket_path()`
  respectively at module scope, exactly as the ticket's Approach
  specifies. Verified byte-for-byte unchanged on this dev host
  (Darwin): both still evaluate to `/var/lib/mbregistry/devices.db` and
  `/run/mbregistry/api.sock`.
- `default_socket_path()` raises `NotImplementedError` on Windows (the
  first of the two options the ticket's Approach offered, "raise
  clearly if called"), rather than the "simply not be called there"
  alternative — this ticket doesn't wire any Windows call site, so
  either choice is equally inert here; ticket 005 picks whichever shape
  fits `cli.py`'s platform branch.
- **Flag for ticket 005/006**: because `registry.api.DEFAULT_SOCKET_PATH`
  is now computed at *module import time* by calling through to
  `default_socket_path()`, simply importing `mbtools.registry.api` on a
  real Windows interpreter will raise `NotImplementedError` immediately
  — before any Windows-specific code path is reached. This is a
  consequence of following the ticket's literal instruction to "call
  through" at the existing module-scope constant site; it was not
  something this ticket's own acceptance criteria required testing
  (they only cover Linux/macOS behavior for `DEFAULT_SOCKET_PATH`, plus
  Windows behavior for `default_db_path()`/`default_pipe_name()`, which
  don't raise). Ticket 005 (wiring Windows support into
  `cli.py`/`run`/`install-service`) and ticket 006 (`windows-latest` CI
  job actually importing `mbtools.registry.api` during test collection)
  should account for this — e.g. by never importing `registry.api` on
  Windows (using `registry.api_windows` exclusively there), or by making
  `DEFAULT_SOCKET_PATH`'s computation lazy/guarded if `api.py` ends up
  needing to be import-safe on Windows too.
- New `tests/registry/paths/test_paths.py` (13 tests): both platform
  branches of all three functions via `monkeypatch.setattr(paths_module
  .sys, "platform", ...)`, mirroring `tests/registry/api/test_api.py`'s
  existing `default_peer_pid` platform-monkeypatch precedent; plus two
  cross-check tests asserting `registry.store.DEFAULT_DB_PATH` and
  `registry.api.DEFAULT_SOCKET_PATH` equal the `paths` module's own
  return values on the platform actually running the test.
- Scoped run: `uv run pytest tests/registry/paths/ tests/registry/store/
  tests/registry/api/` — 144 passed, 1 skipped (pre-existing skip,
  unrelated). Full suite: `uv run pytest -q` — 796 passed, 2 skipped
  (pre-existing skips, unrelated to this ticket).
