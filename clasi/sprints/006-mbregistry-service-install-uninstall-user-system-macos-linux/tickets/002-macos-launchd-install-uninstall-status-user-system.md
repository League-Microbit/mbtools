---
id: '002'
title: macOS launchd install/uninstall/status (user + system)
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004]
depends-on: ['001']
github-issue: ''
issue:
- macos-default-socket-path-needs-a-user-writable-location.md
- mbregistry-service-install-uninstall-user-system.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# macOS launchd install/uninstall/status (user + system)

## Description

Build macOS support in `src/mbtools/registry/service.py` (created in
ticket 001): render the LaunchAgent/LaunchDaemon plist content and
orchestrate install/uninstall/status through the `CommandRunner` seam.
This is the launchd equivalent of what `render_systemd_unit`/
`cmd_install_service` do for Linux today, and is the concrete fix for
`macos-default-socket-path-needs-a-user-writable-location.md`'s "Add a
launchd plist" ask.

1. **Plist rendering** — `render_launchd_plist(scope: str, exec_path=None)`
   (or two functions, `render_launchd_agent_plist`/
   `render_launchd_daemon_plist` — your call, keep it one obvious entry
   point either way) producing the XML plist text matching
   `docs/service.md` §7.1/§7.2's hand-written examples exactly: `Label`
   `org.jointheleague.mbregistry`, `ProgramArguments` invoking the current
   interpreter (`sys.executable -m mbtools.registry.cli run`, same
   "invoke through the running interpreter" convention as
   `render_systemd_unit`), `KeepAlive`/`SuccessfulExit=false`,
   `ThrottleInterval=10`, and the log paths from ticket 001's new path
   helpers as `StandardOutPath`/`StandardErrorPath`. Use `plistlib` to
   generate this (don't hand-roll XML) unless a golden-file test needs a
   specific formatting `docs/service.md` will also show — check with the
   sprint planner's note below if a golden-file mismatch comes up.
2. **Install orchestration** — a function (e.g. `macos_install(scope,
   *, dry_run, runner)`) that: writes the plist to the path ticket 001
   added, then (real run) calls `launchctl enable
   gui/$UID|system/org.jointheleague.mbregistry` and `launchctl bootstrap
   gui/$UID|system <plist_path>` through the runner (see
   `docs/service.md` §7.1/§7.2 for the exact command shapes to mirror).
   Idempotent: bootstrap-ing an already-loaded label should not error the
   command (check `launchctl` semantics — `bootout` first if already
   loaded, then `bootstrap`, is the safe idempotent sequence; verify
   against the doc's own manage commands).
3. **Uninstall orchestration** — stops/unloads (`launchctl bootout`),
   removes the plist file; system scope has no udev/plugdev equivalent to
   clean up. Keeps `devices.db` unless `purge=True` (remove the state
   directory using ticket 001's db-path helpers). Safe no-op (not an
   error) when the plist doesn't exist at the requested scope — check the
   *other* scope's plist too and mention it in the printed message if
   found.
4. **Status** — a function reporting, for a given scope: does the plist
   file exist, and (if so) is it currently loaded/running (`launchctl
   print gui/$UID/... | system/...` — parse for a running/pid indicator,
   or treat any non-error exit as "loaded" if parsing the full output is
   too fragile — keep this simple and testable through the mocked
   runner).

## Acceptance Criteria

- [ ] `render_launchd_plist` (or its two variants) produces XML matching
      `docs/service.md` §7's `Label`/`ProgramArguments`/`KeepAlive`/
      `ThrottleInterval`/log-path directives, for both LaunchAgent and
      LaunchDaemon scope, golden-file tested.
- [ ] `macos_install(scope="user", dry_run=False, runner=<mock>)` writes
      the plist to the ticket-001 path and calls the mocked runner with
      the expected `launchctl enable`/`bootstrap` commands, in that order.
      Same for `scope="system"`.
- [ ] `dry_run=True` writes nothing, runs nothing, and the mock records no
      calls (or the runner's dry-run mode records "would run" text
      instead — match ticket 001's seam).
- [ ] Re-running install with the same mocked runner (simulating "already
      loaded") does not raise and results in the same end state (idempotent
      sequence, e.g. bootout-then-bootstrap or an equivalent safe pattern).
- [ ] `macos_uninstall` removes the plist and calls `launchctl bootout`;
      keeps `devices.db` unless `purge=True`; is a no-op (no error) when
      nothing is installed at that scope, and its message names the other
      scope if that one has an install.
- [ ] `macos_status` correctly reports "not installed", "installed but not
      running", and "installed and running" for both scopes, driven
      entirely through a mocked runner (no real `launchctl` call anywhere
      in this ticket's tests).
- [ ] No test in this ticket runs on a non-macOS assumption implicitly —
      force the platform branch via monkeypatch the same way
      `tests/registry/cli/test_cli_install_service.py` already does for
      the Linux branch, so these tests run and pass on Linux/Windows CI
      too.

## Implementation Plan

**Approach**: All new code lives in `service.py`; nothing in `cli.py`
changes in this ticket (wiring is ticket 004's job) — write and test
`service.py`'s macOS functions directly, calling them from tests, not
through `main([...])`.

**Files to create**:
- `tests/registry/service/test_service_macos.py`

**Files to modify**:
- `src/mbtools/registry/service.py` (add macOS rendering + orchestration)

**Testing plan**: Unit tests only, entirely through the ticket-001
`CommandRunner` mock/double. Run `uv run pytest tests/registry/service/`.

**Documentation updates**: None yet (ticket 004).
