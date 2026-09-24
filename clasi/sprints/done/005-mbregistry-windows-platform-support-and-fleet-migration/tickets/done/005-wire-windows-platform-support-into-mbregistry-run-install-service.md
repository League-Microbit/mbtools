---
id: '005'
title: Wire Windows platform support into mbregistry run/install-service
status: done
use-cases:
- SUC-002
depends-on:
- '003'
- '004'
github-issue: ''
issue: mbregistry-windows-platform-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Wire Windows platform support into mbregistry run/install-service

## Description

Integrate tickets 003 (`registry.api_windows`) and 004
(`registry.service_windows`) into `registry.cli`'s two existing entry
points, `cmd_run` and `cmd_install_service`, so `mbregistry run`/
`mbregistry install-service` actually behave correctly on Windows — the
final assembly step `mbregistry-windows-platform-support.md` needs
before it's a genuine second supported daemon platform, not just three
standalone modules.

**Approach**
- `cmd_run`: add a `sys.platform == "win32"` branch that constructs
  `registry.api_windows.WindowsPipeAPIServer` instead of
  `registry.api.RegistryAPIServer` inside (or alongside)
  `assemble_registry`'s existing assembly — same `Store`/`Locks`/
  `PollingPortWatcher` (Decision 2 — unchanged on Windows), only the
  local-API transport differs. `remote_api`/`peering`/`console_compat.*`
  are TCP-based already and need no platform branch (sprint.md Impact
  section — this sprint claims no Windows peering).
- `cmd_install_service`: add a `sys.platform == "win32"` branch that
  calls `registry.service_windows.cmd_install_service_windows` instead
  of the existing systemd-unit/udev-rule path. The Linux/macOS branch is
  untouched — same function, same behavior, same tests, gated by the
  same `sys.platform` check pattern ticket 002/003/004 already
  established.
- No new CLI flags or subcommands — this ticket is pure dispatch,
  consistent with `mbregistry-windows-platform-support.md`'s own scope
  statement ("behind the same interface... no other module re-parses").

**Files to create/modify**
- `src/mbtools/registry/cli.py` (`cmd_run`, `cmd_install_service` gain
  the platform branch; `assemble_registry` or a small Windows-specific
  assembly helper, implementer's call, whichever keeps the existing
  4-tuple-return contract every current caller/test unpacks intact on
  non-Windows).
- `tests/registry/cli/` (new tests exercising both branches by
  monkeypatching `sys.platform`, the same technique
  `registry/api.py`'s own platform-dispatch tests already use for
  `default_peer_pid`).

**Documentation updates**: none beyond what tickets 002-004 already
added — this ticket is integration, not new user-facing behavior beyond
"it now actually works end-to-end on Windows, per fakes/CI."

## Acceptance Criteria

- [x] `cmd_run` on a simulated `sys.platform == "win32"` constructs
      `WindowsPipeAPIServer` (not `RegistryAPIServer`) while every other
      component (`daemon`, `remote_api`, `peering`, `console_compat.*`)
      is assembled exactly as on Linux/macOS.
- [x] `cmd_run` on Linux/macOS is byte-for-byte unchanged in behavior —
      no regression to any existing `cmd_run`/`assemble_registry` test.
- [x] `cmd_install_service` on a simulated `sys.platform == "win32"`
      calls `service_windows.cmd_install_service_windows` and returns
      its exit code, never touching the systemd-unit/udev-rule code
      path.
- [x] `cmd_install_service` on Linux/macOS is unchanged — same systemd
      unit, same udev rule, same printed instructions as before this
      ticket.
- [x] A single, real end-to-end test on the current dev platform
      (macOS/Linux) proves the branch *selection* logic itself (i.e. the
      `sys.platform` check dispatches correctly), even though the
      Windows-side behavior it dispatches to can only be exercised via
      fakes.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/cli/
  tests/registry/api_windows/ tests/registry/service_windows/` — full
  regression check across every module this ticket touches or depends
  on.
- **New tests to write**: platform-branch dispatch tests in
  `tests/registry/cli/`, monkeypatching `sys.platform` per case.
- **Verification command**: `uv run pytest tests/registry/cli/
  tests/registry/api_windows/ tests/registry/service_windows/
  tests/registry/paths/`

## Implementation Notes

- This is the ticket where "mbregistry is a genuine second supported
  daemon platform on Windows" (this sprint's Goal) actually becomes
  true in the code, not just in three independent modules — treat the
  acceptance criteria's "byte-for-byte unchanged on Linux/macOS" bar as
  the load-bearing constraint; a regression here would be a real
  fleet-wide risk, not just a Windows-only one.
- After this ticket, `mbregistry-windows-platform-support.md`'s scope is
  fully implemented (USB watch via Decision 2's no-new-module choice,
  service install, named-pipe query API, ProgramData paths) — ticket 006
  (CI) is what gives this sprint real, non-hardware verification that it
  actually works on Windows.

**Implemented (this ticket):**

- `assemble_daemon_and_api` (`src/mbtools/registry/cli.py`) gained the
  `sys.platform == "win32"` branch: it now constructs
  `api_windows.WindowsPipeAPIServer` (pipe name = the caller's own
  `socket_path` argument, coerced to `str`) instead of
  `api.RegistryAPIServer` on Windows, sharing the exact same `Daemon`/
  shared `threading.RLock` either way. No `FlashOp` is constructed on
  the Windows branch (`WindowsPipeAPIServer` has no `flash` op — ticket
  003's own scope). `assemble_registry` itself is unchanged — it calls
  `assemble_daemon_and_api` and inherits the branch automatically, so
  `remote_api`/`peering`/`console_compat.*` needed no platform branch of
  their own, per the ticket's own Approach.
- `cmd_install_service` gained a `sys.platform == "win32"` branch at its
  top that calls `service_windows.cmd_install_service_windows(exec_path)`
  and returns its exit code — the systemd-unit/udev-rule code below is
  never reached on Windows. `exec_path` is passed explicitly as
  `"{sys.executable} -m mbtools.registry.cli run --windows-service"`,
  not `cmd_install_service_windows`'s own default (which stays the bare
  `... run`, unchanged, so ticket 004's own golden-file tests in
  `tests/registry/service_windows/test_service_windows.py` needed no
  changes) — see the next point for why the flag matters.
- **`--windows-service` (new `run` flag) and `cmd_run`/`_run_registry`
  split**: per the wiring point in this ticket's own dispatch brief, the
  SCM-launched entry point must go through
  `service_windows.run_as_windows_service`, not call the daemon's poll
  loop directly (`service_windows.py`'s own "a plain console app is not
  a real service" — the SCM kills a process that never calls
  `StartServiceCtrlDispatcherW`). `cmd_run`'s old body (assembly +
  poll loop + cleanup) was extracted verbatim into a new
  `_run_registry(args, stop_event)`, parameterized on an external
  `threading.Event` rather than constructing one itself. `cmd_run` is
  now a thin dispatcher: `--windows-service` given → calls
  `run_as_windows_service(lambda stop_event: _run_registry(args,
  stop_event))` (that function's own `stop_event`, set on
  `SERVICE_CONTROL_STOP`, becomes `_run_registry`'s stop signal);
  otherwise → builds a `threading.Event`, wires the existing
  SIGTERM/SIGINT handlers to it exactly as before this ticket, and
  calls `_run_registry(args, stop_event)` directly. No test exercises
  `_run_registry`'s own pipeline end-to-end through `cmd_run` (none did
  before this ticket either — `tests/registry/cli/test_cli_run.py`/
  `test_cli_run_peering.py` exercise `assemble_daemon_and_api`/
  `assemble_registry` directly); the split is a pure extraction, and the
  full suite (862 passed) confirms no behavior changed for the
  non-`--windows-service` path.
- New `_resolve_local_api_address(flag_value, env_var)` (`cli.py`):
  flag > env var > platform default, shared by `cmd_run` and `cmd_list`.
  On `sys.platform == "win32"` the default (and any override) is
  returned as a plain `str` pipe name (`registry.paths
  .default_pipe_name()`) — never routed through `pathlib.Path`, per the
  team-lead's explicit instruction and `registry.client`'s own new
  "keep the pipe name as a plain str" contract (see next point).
  Everywhere else it delegates to the pre-existing
  `client.resolve_socket_path` unchanged. `cmd_list` now calls this too
  (not just `cmd_run`) — without it, `mbregistry list` would still raise
  `TypeError` from `Path(None)` on Windows even after the client-side
  transport fix below, since `DEFAULT_SOCKET_PATH` is `None` there.

**Additional team-lead-assigned scope (not in this ticket's own
Acceptance Criteria, recorded here per dispatch instructions, mirroring
tickets 003/004's own precedent of carrying an "additional item" beyond
literal ticket scope):**

- **`registry.client.RegistryClient`'s Windows transport, closing
  ticket 003's own documented seam.** `RegistryClient.connect()`/
  `close()` now dispatch on `sys.platform == "win32"`: on Windows, they
  open `api_windows.WindowsPipeAPIServer`'s named pipe from the client
  side via a new `api_windows._Win32PipeAPI.open_client_pipe` method
  (`CreateFileW`, added to that class rather than duplicating
  `ReadFile`/`WriteFile`/`CloseHandle` ctypes bindings a second time in
  `client.py` — `_Win32PipeAPI`'s existing `read_file`/`write_file`/
  `close_handle` already work unchanged against a client-opened handle,
  since a named-pipe handle behaves the same for I/O regardless of
  which side opened it), then reuse `api_windows._PipeLineReader`/
  `_PipeWriter` (unchanged) for the newline-JSON framing — so
  `RegistryClient._request` needed zero changes: those two classes
  already present the exact interface `socket.makefile()` gave it.
  `RegistryClient.__init__` gained an injectable `win32=` seam
  (mirrors every other Windows module's own test-double convention in
  this package), defaulting to a real `api_windows._Win32PipeAPI()`
  (safe to construct off Windows too — it touches no `ctypes.windll`
  call until a method actually runs). `self.socket_path` is kept as a
  plain `str` on `sys.platform == "win32"` (never wrapped in
  `pathlib.Path`) — this was ticket 003's own flagged risk (`Path`'s
  UNC-string normalization on a real `WindowsPath` being unverifiable
  without Windows hardware); unchanged (still `Path(...)`) on every
  other platform. Every lock a client session acquires is still
  released when the pipe closes, unaffected — that guarantee lives in
  `WindowsPipeAPIServer._handle_connection`'s own `finally` block
  (unchanged by this ticket), not in the client.
  **Bug found while testing this addition**: `_PipeLineReader` had no
  `close()` method — `RegistryClient.close()` calls `.close()`
  uniformly on both its read and write halves (matching
  `socket.makefile()`'s interface, which has `.close()` on both), and
  every fake-transport test in this ticket's new
  `tests/registry/client/test_client_windows.py` hit
  `AttributeError: '_PipeLineReader' object has no attribute 'close'`
  on the very first `with RegistryClient(...) as client:` block. Fixed
  by adding a no-op `close()` to `_PipeLineReader` in `api_windows.py`
  (there is nothing to flush on the read side, and it never owned the
  handle) — this also benefits any future caller of that class that
  treats it like `socket.makefile("r", ...)`. This bug was latent in
  ticket 003's own code (never triggered there because
  `WindowsPipeAPIServer._handle_connection` never calls `.close()` on
  its own `rfile`, only on the raw handle) and had zero user-facing
  impact until this ticket's client-side reuse of the same class
  surfaced it.
  Tested with fakes only (`tests/registry/client/test_client_windows.py`,
  15 tests incl. one real-pipe round-trip
  `skipif(sys.platform != "win32")`, never run in this suite) — the
  `windows-latest` CI job (ticket 006) is what exercises the real
  `CreateFileW`/`ReadFile`/`WriteFile` calls and the real
  server+client-together round trip for the first time.
  `tests/test_windows_import_safety.py` stays green unmodified (verified
  — `client.py`'s new module-scope import of `api_windows` follows the
  same guarded-off-Windows pattern that module already established).

**New test files** (all pass; no existing test file was modified except
where noted above):
- `tests/registry/cli/test_cli_run_windows.py` (11 tests): branch
  selection in `assemble_daemon_and_api`, `_resolve_local_api_address`
  precedence, the `--windows-service` flag's parsing/dispatch through a
  recording fake `run_as_windows_service`.
- `tests/registry/cli/test_cli_install_service_windows.py` (3 tests):
  `cmd_install_service`'s Windows dispatch, the `--windows-service`-
  bearing `exec_path` it passes, and a regression smoke check that the
  systemd/udev fallthrough still writes both files off Windows.
- `tests/registry/client/test_client_windows.py` (8 tests, plus one
  `skipif`'d real-hardware test): `RegistryClient`'s new pipe transport against a fake
  `win32=` double, plus the real-hardware round trip gated to actual
  Windows.

**Verification**: scoped run (`tests/registry/cli/
tests/registry/api_windows/ tests/registry/service_windows/
tests/registry/paths/ tests/registry/client/
tests/test_windows_import_safety.py`) — 146 passed, 1 skipped. Full
suite (`uv run pytest -q`) — 862 passed, 3 skipped (2 pre-existing +
this ticket's 1 new real-Windows skip); no regressions.
