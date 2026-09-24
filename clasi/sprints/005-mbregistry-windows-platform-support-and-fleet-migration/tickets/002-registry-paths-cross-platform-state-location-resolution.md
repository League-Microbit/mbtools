---
id: '002'
title: 'registry.paths: cross-platform state-location resolution'
status: open
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

- [ ] `paths.default_db_path()` returns exactly today's
      `/var/lib/mbregistry/devices.db` on Linux/macOS — verified by a
      test asserting `registry.store.DEFAULT_DB_PATH` is unchanged from
      its value before this ticket.
- [ ] `paths.default_socket_path()` returns exactly today's
      `/run/mbregistry/api.sock` on Linux/macOS — same
      no-behavior-change guarantee for `registry.api.DEFAULT_SOCKET_PATH`.
- [ ] `paths.default_db_path()` on a simulated Windows platform returns
      a path rooted at `%ProgramData%` (or its default fallback if the
      env var is unset).
- [ ] `paths.default_pipe_name()` returns a fixed, documented named-pipe
      path string, callable on any platform.
- [ ] Module docstring explicitly states the Windows `ProgramData` path
      is an assumption, not a stakeholder-confirmed decision.
- [ ] Every existing test in `tests/registry/store/` and
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
