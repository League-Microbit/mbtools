---
id: '005'
title: Wire Windows platform support into mbregistry run/install-service
status: open
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

- [ ] `cmd_run` on a simulated `sys.platform == "win32"` constructs
      `WindowsPipeAPIServer` (not `RegistryAPIServer`) while every other
      component (`daemon`, `remote_api`, `peering`, `console_compat.*`)
      is assembled exactly as on Linux/macOS.
- [ ] `cmd_run` on Linux/macOS is byte-for-byte unchanged in behavior —
      no regression to any existing `cmd_run`/`assemble_registry` test.
- [ ] `cmd_install_service` on a simulated `sys.platform == "win32"`
      calls `service_windows.cmd_install_service_windows` and returns
      its exit code, never touching the systemd-unit/udev-rule code
      path.
- [ ] `cmd_install_service` on Linux/macOS is unchanged — same systemd
      unit, same udev rule, same printed instructions as before this
      ticket.
- [ ] A single, real end-to-end test on the current dev platform
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
