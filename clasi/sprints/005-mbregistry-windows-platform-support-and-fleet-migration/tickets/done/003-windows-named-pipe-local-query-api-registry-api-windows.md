---
id: '003'
title: Windows named-pipe local query API (registry.api_windows)
status: done
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

- [x] `registry.api_windows` imports cleanly on macOS/Linux (no
      import-time failure from any Windows-only symbol) — mirrors
      `usbwatch.py`'s/`identity.py`'s existing `try/except` guard around
      `pyserial`.
- [x] `WindowsPipeAPIServer` dispatches `list`/`find`/`lock`/`unlock`/
      `mark_flashed` identically to `RegistryAPIServer` (same wire JSON
      shape) against a fake pipe/connection double — proven by a test
      that runs the *same* op-level assertions `tests/registry/api/`
      already makes against `RegistryAPIServer`, adapted to this
      module's fake transport.
- [x] `_holder_for_connection` derives a `HolderRef` from an injected
      fake client-PID lookup (never a client-supplied PID), matching
      `api.py`'s `peer_pid_fn` injection pattern.
- [x] The pipe's security descriptor is explicitly set (not left at the
      Win32 default) — verified by asserting the `ctypes` call the
      module makes includes a non-null, restrictive security-attributes
      argument (exact assertion depth is an implementer call, since this
      can't be verified against a real ACL without Windows hardware —
      document what *is* and isn't proven).
- [x] No `pywin32` (or any other new third-party package) import
      appears anywhere in this module — `ctypes`/stdlib only.
- [x] Pipe name is sourced from `registry.paths.default_pipe_name()`,
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

**Implemented (this ticket):**

- New `src/mbtools/registry/api_windows.py`: `WindowsPipeAPIServer`
  (extends `BaseAPIServer`, same op dispatch as `RegistryAPIServer`
  minus `flash` — see below), `_Win32PipeAPI` (the real ctypes
  transport, only ever constructed on real Windows),
  `default_is_pid_alive_windows`, `_PipeLineReader`/`_PipeWriter`
  (newline-delimited-JSON framing over `ReadFile`/`WriteFile`, the
  named-pipe counterpart to `conn.makefile()`), and the module-level
  `_kernel32`/`_advapi32` names (bound only when `sys.platform ==
  "win32"` at import time; `None` otherwise) that every real ctypes call
  site reads through — this is what lets tests monkeypatch them with
  fakes and exercise `_Win32PipeAPI`'s own ctypes-calling code
  (including the security-descriptor construction) without ever
  touching a real Windows API.
- **`flash` deliberately not wired in.** The ticket's own Description
  lists the five ops (`list`/`find`/`lock`/`unlock`/`mark_flashed`) and
  says this module "adds no protocol/op logic of its own"; `flash` is
  `RegistryAPIServer`'s own addition (ticket 008, `FlashOp`-backed), not
  part of `BaseAPIServer`, and no `flash_op` constructor parameter is
  listed in this ticket's Approach/Files. A `flash` request over the
  named pipe gets the same `invalid_request` response as any other
  unrecognized op (tested). The four name-registry ops
  (`names_get`/`names_set`/`names_clear`/`names_list`) *are* wired in —
  they come for free from `BaseAPIServer`, cost nothing extra, and keep
  "every op this server exposes... identical to `api.RegistryAPIServer`'s"
  true for everything except the one op this ticket explicitly excludes.
- **Security descriptor**: `_Win32PipeAPI._security_attributes` builds a
  `SECURITY_ATTRIBUTES` from a fixed SDDL string,
  `"D:P(A;;GA;;;BA)(A;;GA;;;OW)"` (protected DACL, Generic-All to
  built-in Administrators and the pipe's own owner only — no
  Everyone/Authenticated-Users ACE), via
  `ConvertStringSecurityDescriptorToSecurityDescriptorW` (advapi32) —
  standard-library-only, no `pywin32`. **What is proven without Windows
  hardware**: the SDDL string's own shape (owner + administrators only,
  asserted directly), and that `_Win32PipeAPI.create_named_pipe` always
  builds and passes a non-null `SECURITY_ATTRIBUTES` pointer to
  `CreateNamedPipeW` (proven against a fake `kernel32`/`advapi32` pair
  standing in for the real DLL objects, reached via the module's own
  `_kernel32`/`_advapi32` globals). **What is not proven**: that
  `ConvertStringSecurityDescriptorToSecurityDescriptorW` actually parses
  that SDDL into the DACL a real kernel enforces, that `CreateNamedPipeW`
  honors it, or that any of this module's `ctypes` struct
  layouts/calling convention match a real `kernel32.dll`/`advapi32.dll`
  — ticket 006's `windows-latest` CI job is the first real check; there
  is no Windows hardware-acceptance target for this project.
- **`default_is_pid_alive_windows`** (implementer addition, not
  explicitly named in the ticket's own Approach's list of ctypes calls):
  `OpenProcess`/`GetExitCodeProcess`-based liveness check for the
  periodic lock sweep. Needed because `api.default_is_pid_alive`'s POSIX
  `os.kill(pid, 0)` has no Windows equivalent — Python's `os.kill` on
  Windows doesn't support signal 0 as a liveness probe — and without
  *some* Windows-appropriate implementation, `WindowsPipeAPIServer`'s
  inherited sweep mechanics would be silently inert on the one platform
  they're meant to run on. Same "needs real-Windows confirmation"
  caveat as everything else ctypes-based in this module.
- Pipe opened in byte mode (`PIPE_TYPE_BYTE`/`PIPE_READMODE_BYTE`), not
  message mode — this wire protocol frames on its own `\n` characters
  (same as the Unix-socket transport), so the pipe should behave as a
  plain byte stream, not add a second, message-boundary framing layer
  underneath the one the protocol already has.
- `stop()`'s shutdown mechanism: since a named pipe has no
  `socket.close()`-from-another-thread equivalent to unblock a pending
  `accept()`-like call, `_accept_loop`'s blocking `connect_named_pipe`
  is unblocked by `stop()` calling `close_handle` on that specific
  pending handle from another thread (tracked via `self._pending_handle`
  under `self._accept_gate`) — a documented, standard-enough technique
  for closing a handle that's blocked in a synchronous call on another
  thread, but (like everything else ctypes-specific here) unverified
  against real Windows kernel behavior.
- New `tests/registry/api_windows/test_api_windows.py` (25 tests):
  op-dispatch tests calling `_dispatch_line` directly against a
  `_RecordingWFile` fake (mirrors the ticket's own "fake pipe/connection
  double" language) for list/find/lock/unlock/mark_flashed/names_*/
  unknown-op/malformed-JSON/flash-is-unsupported; a
  `_holder_for_connection` test with an injected fake `peer_pid_fn`;
  `_PipeLineReader`/`_PipeWriter` framing tests (including lines split
  across multiple scripted `read_file` chunks); the two security-
  descriptor tests described above; an AST-based (not substring-based —
  a naive substring check false-positives on this module's own
  `_Win32PipeAPI` identifier) "no pywin32 import" test; and two
  end-to-end lifecycle tests (`start()`/scripted single connection/
  `stop()`) against `_ScriptedLifecycleWin32`, a full fake covering
  every method `WindowsPipeAPIServer` calls on its `win32` collaborator,
  proving `stop()` joins the accept/sweep/connection threads and that a
  closed connection releases the locks it acquired — mirrors
  `test_api.py`'s own `test_stop_joins_connection_handler_threads_before_returning`.
- Scoped run: `uv run pytest tests/registry/api_windows/
  tests/registry/api/ tests/registry/paths/` — 83 passed, 1 skipped
  (pre-existing platform skip, unrelated).

**Additional team-lead-assigned scope (not in this ticket's own
Acceptance Criteria, recorded here per dispatch instructions):**

- **Fixed the import-time crash ticket 002 flagged** ("Flag for ticket
  005/006" in ticket 002's own Implementation Notes):
  `registry.api.DEFAULT_SOCKET_PATH` was computed by calling
  `registry.paths.default_socket_path()` unconditionally at module
  import time; that function raises `NotImplementedError` on
  `sys.platform == "win32"`. Since `registry.client`, `registry.cli`,
  `deploy.cli`, `relay.cli`, and `serial.cli` all import
  `DEFAULT_SOCKET_PATH` from `registry.api` at their own module scope,
  every one of those modules would have raised on import on real
  Windows, before any Windows-aware code in this sprint ever ran —
  meaning ticket 006's `windows-latest` CI job would have failed at test
  *collection*, not at any real assertion. Fixed in `api.py`: `sys.platform
  == "win32"` is now checked directly at that constant's own definition
  (mirroring `default_peer_pid`'s existing platform-dispatch style in
  the same file), giving `DEFAULT_SOCKET_PATH = None` on Windows instead
  of calling through to a function that raises. `registry.paths
  .default_socket_path()` itself is unchanged (still raises when
  actually *called* on Windows — ticket 002's own decision, left for
  whichever ticket resolves it) — only the unconditional module-scope
  *call* to it was the bug.
  `registry.client.resolve_socket_path`'s `default` parameter's type
  hint was widened to `str | Path | None` to match (its docstring now
  says plainly that calling it with no override on Windows still raises
  `TypeError` from `Path(None)` — no caller in this sprint's scope
  reaches that; ticket 005's Windows branch in `cli.py` resolves the
  pipe name instead of calling this function at all).
- **New `tests/test_windows_import_safety.py`** (4 tests, top-level
  since it spans `deploy`/`registry`/`relay`/`serial`, following the
  existing `tests/test_stub.py` top-level-test precedent): discovers
  every mbtools module via `pkgutil.walk_packages` and re-imports each
  one (`importlib.reload`, after warming the normal-platform import
  cache first — see the test file's own docstring for why a *cold*
  `sys.platform = "win32"` then `import` spuriously fails on totally
  unrelated stdlib modules like `subprocess`, which is a property of
  this dev machine, not a bug) under a simulated `sys.platform ==
  "win32"`, asserting none raises. `registry.api_windows` itself is
  excluded from that reload (its own `ctypes.windll` guard is *supposed*
  to reach for a real capability that genuinely doesn't exist on
  macOS/Linux regardless of caching — that module's own off-Windows
  import-safety is covered directly in its own test file instead). Two
  more targeted tests reproduce the exact ticket-002-flagged regression
  directly (`DEFAULT_SOCKET_PATH is None` on simulated Windows;
  `paths.default_socket_path()` still raises when called, unaffected).
  Verified this test suite actually catches the bug: temporarily
  reverted the `api.py` fix, confirmed 3 of the 4 tests failed with
  exactly the `NotImplementedError` ticket 002 predicted, then restored
  the fix and confirmed all 4 pass again with no state leaking into
  later tests.
- **Client-side named-pipe reachability (`registry.client
  .RegistryClient`): left as a documented seam, not implemented.** The
  dispatch instructions asked for this "if the ticket's scope allows —
  otherwise leave a clear seam for ticket 005." Investigated: ticket
  005's own stated scope is `cmd_run`/`cmd_install_service` (the daemon
  side, in `cli.py`) — it does not touch `client.py`, so no ticket in
  this sprint currently owns wiring `RegistryClient.connect()` to speak
  to the named pipe. Chose not to implement it in this ticket because
  the one real blocker isn't dispatch-branching (that part's easy) but a
  genuine, Windows-hardware-unverifiable risk: `RegistryClient.__init__`
  takes `socket_path: str | Path` and stores `Path(socket_path)`; a pipe
  name (`registry.paths.default_pipe_name()`'s
  `r"\\.\pipe\mbregistry"`) pushed through a real `WindowsPath` risks
  silent normalization of that UNC-shaped string in a way this project
  cannot test without Windows hardware. Documented the gap and the
  reused-transport approach (`api_windows._Win32PipeAPI`'s
  `read_file`/`write_file` plus a new client-side `CreateFileW` open
  call) directly in `RegistryClient`'s own docstring, as a clear seam
  for whichever ticket picks it up next, rather than guessing blind at
  something unverifiable here.
- Full suite: `uv run pytest -q` — see this ticket's commit for the
  final count; no regressions to any existing test.
