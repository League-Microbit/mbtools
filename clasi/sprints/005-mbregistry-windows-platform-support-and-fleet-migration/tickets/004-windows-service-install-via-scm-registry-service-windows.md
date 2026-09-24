---
id: '004'
title: Windows service install via SCM (registry.service_windows)
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

# Windows service install via SCM (registry.service_windows)

## Description

New module `registry.service_windows`: the Windows counterpart to
`registry.cli`'s existing systemd-unit rendering (`render_systemd_unit`)
— renders the Windows Service Control Manager commands that register
`mbregistry` as a restart-on-failure service (brief §3.1, spec §3.8).

**Approach** (sprint architecture Decision 3: SCM via `sc.exe`, not
Task Scheduler; Decision 1: no `pywin32`)
- `render_windows_service_install(exec_path: str | None = None) -> str`:
  returns the `sc.exe create mbregistry binPath= "..." start= auto
  DisplayName= "..."` command text — the Windows analog of
  `render_systemd_unit()`'s returned unit-file text. `exec_path` mirrors
  `render_systemd_unit`'s `exec_start` parameter (defaults to invoking
  `mbregistry run`, resolved the same way that function already
  resolves the Linux `ExecStart=` — reuse the reasoning in its
  docstring about "not on the PATH systemd uses" rather than
  reinventing it).
- `render_windows_service_failure_actions(service_name: str = "mbregistry") -> str`:
  returns the `sc.exe failure mbregistry actions= restart/60000/
  restart/60000/restart/60000 reset= 86400` command text — the
  systemd-`Restart=on-failure`-equivalent continuous-respawn policy
  (Decision 3).
- `cmd_install_service`'s existing Linux/systemd+udev path is
  **unchanged**; this ticket adds a `sys.platform == "win32"` branch
  (wired in ticket 005, not here — this ticket only adds the pure
  render functions plus this module's own thin `cmd_install_service_windows`
  entry point ticket 005 will call) that **prints, never runs**, the two
  rendered command strings — mirroring the existing Linux precedent's
  "safe to exercise without root or a real systemd" property exactly:
  safe to exercise without Administrator or a real SCM.
- **No `pywin32` import** — `sc.exe` ships with every Windows install,
  the same way `systemctl`/`udevadm` ship with every systemd Linux
  install this project already assumes (module docstring should say so
  explicitly, referencing sprint.md Decision 1).

**Files to create/modify**
- `src/mbtools/registry/service_windows.py` (new — the two `render_*`
  functions plus `cmd_install_service_windows` per this ticket's own
  scope; wiring it into `cli.py`'s dispatch is ticket 005).
- `tests/registry/service_windows/test_service_windows.py` (new).

**Documentation updates**: none beyond this module's own docstrings;
`docs/migration.md` (ticket 008) is about the Linux fleet, not Windows,
so it does not need to reference this module.

## Acceptance Criteria

- [x] `render_windows_service_install()` returns deterministic text
      containing a valid `sc.exe create` invocation naming the service
      `mbregistry`, a `binPath=` pointing at `mbregistry run` (or the
      given `exec_path`), and `start= auto`.
- [x] `render_windows_service_failure_actions()` returns a valid
      `sc.exe failure` invocation with a restart action and a non-zero
      `reset=` window — a real systemd-`Restart=`-equivalent policy, not
      a placeholder.
- [x] Calling either render function performs **no** actual SCM
      operation, no subprocess execution, and requires no Administrator
      privilege or real Windows APIs — pure string construction,
      testable on macOS/Linux CI exactly like `render_systemd_unit()`
      already is.
- [x] `cmd_install_service_windows` prints both rendered command blocks
      (create + failure-actions) to the operator, exactly mirroring
      `cmd_install_service`'s existing "print, don't execute" shape for
      systemd/udev — never itself invokes `sc.exe`.
- [x] No `pywin32` (or any other new third-party package) import
      anywhere in this module.
- [x] Module docstring states the Decision-3 rationale (SCM over Task
      Scheduler) in one or two sentences, so a future reader doesn't
      re-litigate it without cause.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/cli/` (no
  regression to the existing Linux `install-service` path).
- **New tests to write**: `tests/registry/service_windows/
  test_service_windows.py` — string-content assertions on both render
  functions, plus a test that `cmd_install_service_windows` prints
  (captured stdout/stderr) both blocks and performs no `subprocess`
  call.
- **Verification command**: `uv run pytest tests/registry/
  service_windows/ tests/registry/cli/`

## Implementation Notes

- This ticket deliberately produces *text*, not an executed SCM
  registration — the CI ticket (006) is where a real `sc.exe create`/
  `delete` round-trip (against a throwaway service name) can optionally
  prove the rendered text is actually valid on real Windows, if the
  implementer of that ticket judges it worth the added CI complexity
  (sprint.md Open Questions — either choice satisfies this sprint's
  verification goal).
- Ticket 005 is where `registry.cli.cmd_install_service` actually gains
  its `sys.platform == "win32"` branch calling into this module — keep
  this ticket's own `cmd_install_service_windows` a plain, directly
  testable function so ticket 005's wiring is a thin dispatch, not new
  logic.

**Implementation additions beyond this ticket's own text (team-lead
dispatch item, mirroring ticket 003's own precedent of carrying an
"additional item" beyond a ticket's literal scope — see
`tests/test_windows_import_safety.py`'s module docstring):**

- **The dispatcher problem, and why it matters even though this ticket
  only renders text.** A plain console program (`python -m
  mbtools.registry.cli run`, exactly what `render_windows_service_install`'s
  default `exec_path` invokes) registered via `sc.exe create` is *not*
  a real Windows service: the SCM expects the started process to call
  `StartServiceCtrlDispatcherW` within ~30s and report `SERVICE_RUNNING`
  via `RegisterServiceCtrlHandlerExW`/`SetServiceStatus`; a process that
  never does this is killed with error 1053. Unlike the systemd case
  (any foreground process is a valid `ExecStart=`), this means the
  rendered SCM commands alone would be internally inconsistent — a
  `start= auto` + restart-on-failure registration for a process the SCM
  always kills at ~30s regardless of the restart policy.
- **Choice made**: `service_windows.py` also provides
  `run_as_windows_service(main, *, service_name=SERVICE_NAME, win32=None)`,
  a minimal ctypes-only implementation of the
  `StartServiceCtrlDispatcherW`/`RegisterServiceCtrlHandlerExW`/
  `SetServiceStatus` sequence, built the same way
  `registry.api_windows._Win32PipeAPI` wraps its own Win32 calls: real
  ctypes bindings live in `_Win32ServiceAPI`, only ever constructed on
  real Windows, behind an injectable `win32=` seam. `main(stop_event)`
  is called once `SERVICE_RUNNING` has been reported; the stop-event is
  set (and `SERVICE_STOP_PENDING` reported) when the SCM delivers
  `SERVICE_CONTROL_STOP`. Ticket 005 wires this into `cmd_run`'s Windows
  branch — this ticket only builds and tests the primitive.
- **Alternative rejected**: documenting an external service-wrapper
  binary (NSSM/WinSW) instead — rejected because it would add a new
  fleet-wide *binary* dependency, exactly the concern Decision 1 exists
  to avoid, whereas `ctypes.windll.advapi32` is the same category of
  call `api_windows.py` already makes.
- **Import-safety**: unlike `api_windows.py`'s module-scope `if
  sys.platform == "win32": _kernel32 = ctypes.windll.kernel32` guard
  (which required excluding that module from
  `tests/test_windows_import_safety.py`'s generic reimport sweep, since
  the guard's own condition is what the sweep flips), every
  `ctypes.windll`/`ctypes.WINFUNCTYPE` lookup in `service_windows.py`
  is lazy — inside `_Win32ServiceAPI`'s methods, never at module scope.
  Confirmed: `service_windows` needed **no** exclusion from that sweep
  test (`uv run pytest tests/test_windows_import_safety.py` passes
  unmodified with the new module in place).
- **Tested with fakes**: `tests/registry/service_windows/
  test_service_windows.py` drives `run_as_windows_service` against a
  `_FakeWin32ServiceAPI` double (mirrors `api_windows.py`'s own
  fake-transport tests) — proves the status-transition sequence
  (`SERVICE_RUNNING` → optional `SERVICE_STOP_PENDING` →
  `SERVICE_STOPPED`, including on `main` raising) and the
  `SERVICE_CONTROL_STOP` → stop-event wiring, without any real Windows
  API. **Not proven** without hardware: that `_Win32ServiceAPI`'s real
  ctypes bindings actually match `advapi32.dll`'s calling convention —
  flagged "not hardware-verified" pending ticket 006's `windows-latest`
  CI job, mirroring `api_windows.py`'s own caveat.
