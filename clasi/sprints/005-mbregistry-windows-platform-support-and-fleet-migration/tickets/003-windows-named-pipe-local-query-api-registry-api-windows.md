---
id: '003'
title: Windows named-pipe local query API (registry.api_windows)
status: open
use-cases:
- SUC-002
depends-on:
- '002'
github-issue: ''
issue: mbregistry-windows-platform-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Windows named-pipe local query API (registry.api_windows)

## Description

New module `registry.api_windows`: the Windows counterpart to
`registry.api.RegistryAPIServer`, serving the same local device-query
API (`list`/`find`/`lock`/`unlock`/`mark_flashed`) over a Win32 named
pipe instead of a Unix socket (spec §3.5 / brief §9.2).

**Approach** (sprint architecture Decision 1: standard library only, no
`pywin32`)
- `WindowsPipeAPIServer(BaseAPIServer)`: reuses
  `mbtools.registry._api_base.BaseAPIServer` for all op dispatch exactly
  as `registry.api.RegistryAPIServer` does — this module adds no
  protocol/op logic of its own.
- Transport: a Win32 named pipe created via `ctypes.windll.kernel32`
  (`CreateNamedPipeW`, `ConnectNamedPipe`, `ReadFile`, `WriteFile`,
  `DisconnectNamedPipe`, `CloseHandle`), guarded behind
  `sys.platform == "win32"` and lazily imported the same way
  `usbwatch.py`/`identity.py` guard their `pyserial` import — this
  module must remain importable (module-level `import` succeeds) on
  macOS/Linux so the test suite can exercise its pure-logic parts and so
  `registry.cli` can import it unconditionally.
- `_holder_for_connection`: reads the connecting client's PID via
  `GetNamedPipeClientProcessId` (a single `kernel32` call taking the
  pipe handle and an output PID pointer) — the named-pipe counterpart to
  `api.py`'s `SO_PEERCRED`/`LOCAL_PEERPID`-based `default_peer_pid`, same
  "kernel-verified, never a client-supplied PID" property.
- **Security**: `CreateNamedPipeW`'s default security descriptor is
  broader than the Unix socket's filesystem-permission-based access
  control (sprint.md Open Questions). Set an explicit, restrictive
  security descriptor (owner + local administrators, or equivalent) when
  creating the pipe — do not ship the OS default unexamined. If a fully
  correct DACL can't be verified without real Windows hardware, document
  the specific ctypes/DACL choice made and flag it as needing real-
  Windows confirmation in this ticket's Implementation Notes, the same
  way the Windows path defaults are flagged in ticket 002.
- Pipe name comes from `registry.paths.default_pipe_name()` (ticket
  002) — no hardcoded literal in this module.
- Every op this server exposes and its wire JSON shape are identical to
  `api.RegistryAPIServer`'s — a client (`mbdeploy`/`mbserial` on a
  future Windows build) sees no protocol difference, only a different
  transport.

**Files to create/modify**
- `src/mbtools/registry/api_windows.py` (new).
- `tests/registry/api_windows/test_api_windows.py` (new — fakes for the
  named-pipe transport: a scripted fake pipe handle/connection object
  standing in for the `ctypes` calls, mirroring how `FakeSerial` fakes
  pyserial, plus an injectable `peer_pid_fn`-equivalent exactly like
  `api.py`'s own `peer_pid_fn` parameter, so every op-dispatch test runs
  on macOS/Linux CI without any real Windows API call).

**Documentation updates**: `docs/design/registry-api.md` gains a section
describing the named-pipe transport alongside the existing Unix-socket
section (same ops, different transport) — see that doc's existing
structure for where this fits.

## Acceptance Criteria

- [ ] `registry.api_windows` imports cleanly on macOS/Linux (no
      import-time failure from any Windows-only symbol) — mirrors
      `usbwatch.py`'s/`identity.py`'s existing `try/except` guard around
      `pyserial`.
- [ ] `WindowsPipeAPIServer` dispatches `list`/`find`/`lock`/`unlock`/
      `mark_flashed` identically to `RegistryAPIServer` (same wire JSON
      shape) against a fake pipe/connection double — proven by a test
      that runs the *same* op-level assertions `tests/registry/api/`
      already makes against `RegistryAPIServer`, adapted to this
      module's fake transport.
- [ ] `_holder_for_connection` derives a `HolderRef` from an injected
      fake client-PID lookup (never a client-supplied PID), matching
      `api.py`'s `peer_pid_fn` injection pattern.
- [ ] The pipe's security descriptor is explicitly set (not left at the
      Win32 default) — verified by asserting the `ctypes` call the
      module makes includes a non-null, restrictive security-attributes
      argument (exact assertion depth is an implementer call, since this
      can't be verified against a real ACL without Windows hardware —
      document what *is* and isn't proven).
- [ ] No `pywin32` (or any other new third-party package) import
      appears anywhere in this module — `ctypes`/stdlib only.
- [ ] Pipe name is sourced from `registry.paths.default_pipe_name()`,
      not a literal in this module.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/api/
  tests/registry/paths/` (no regression to the existing Unix-socket
  server or ticket 002's path module).
- **New tests to write**: `tests/registry/api_windows/
  test_api_windows.py`, fully fake-transport-based, runnable on
  macOS/Linux CI.
- **Verification command**: `uv run pytest tests/registry/api_windows/
  tests/registry/api/`

## Implementation Notes

- This module's op-dispatch correctness is fully verifiable without
  Windows hardware (it's the same `BaseAPIServer` logic already
  hardware-independent on Linux/macOS). What is **not** verifiable
  without real Windows is: the exact `ctypes` signatures/calling
  convention against a real `kernel32.dll`, real named-pipe security
  semantics, and real client-PID retrieval. The `windows-latest` CI job
  (ticket 006) is the first *real* verification this code gets; hardware
  acceptance (ticket 010) explicitly does not cover it (no Windows
  hardware exists for this project).
- Keep the `ctypes` function signatures (`argtypes`/`restype`) declared
  explicitly rather than relying on default `ctypes` argument marshaling
  — this is exactly the kind of code where a wrong signature fails
  silently or corrupts memory on real Windows while looking fine in a
  fake-transport unit test.
