---
id: '003'
title: Linux systemd user/system install/uninstall/status + plugdev/udev
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
depends-on:
- '001'
github-issue: ''
issue: mbregistry-service-install-uninstall-user-system.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Linux systemd user/system install/uninstall/status + plugdev/udev

## Description

Move `render_systemd_unit`/`render_udev_rule` from `cli.py` into
`service.py` (created in ticket 001), add a user-scope unit variant, and
build the install/uninstall/status orchestration for both Linux scopes,
including the `plugdev`/udev preflight-and-auto-add behavior the issue
specifies.

1. **Move existing renderers.** Relocate `render_systemd_unit` and
   `render_udev_rule` (currently in `cli.py`, system-scope-only) into
   `service.py` verbatim, keeping their existing behavior and signatures
   for system scope — this is a move, not a rewrite, so the existing
   golden-file assertions in `tests/registry/cli/test_cli_install_service.py`
   for their *content* keep passing once relocated (the test file itself
   is rewritten in ticket 004; for this ticket, just get the functions
   moved and re-tested in their new home).
2. **Add a user-scope unit variant** — `render_systemd_unit(scope="user")`
   (extend the existing signature rather than adding a parallel function,
   if that keeps call sites simplest) producing a systemd **user** unit:
   no `RuntimeDirectory=`/`StateDirectory=` (those are system-manager-only
   directives — a user unit relies on `paths.py`'s existing
   `user_socket_path`/`user_db_path`, which already resolve to
   `$XDG_RUNTIME_DIR`/`$XDG_STATE_HOME`-based paths that exist without
   those directives), `WantedBy=default.target` instead of
   `multi-user.target`. Same `ExecStart=` convention (current
   interpreter).
3. **Linux install orchestration**, both scopes:
   - `--system`: write unit + udev rule (existing renderers), run
     `systemctl daemon-reload`, `systemctl enable --now
     mbregistry.service`, `udevadm control --reload-rules`, `udevadm
     trigger`, and — the behavior change from today's print-only
     `install-service` — actually run `usermod -aG plugdev
     $SUDO_USER` through the runner (use the existing
     `_resolve_operating_user` logic, moved or imported from wherever
     ticket 004 leaves it) rather than just printing it. Print the "log in
     again" note same as before.
   - `--user`: **preflight check** — before writing anything, check
     whether the operating user is already in `plugdev` and whether the
     udev rule (system-scope file) exists. If either is missing, print the
     exact `sudo usermod -aG plugdev <user>` / pointer to `service install
     --system` (which writes the udev rule) commands and **stop without
     installing** (per the issue's explicit "install --user prints the
     exact sudo commands and stops" behavior) — return a distinct
     nonzero exit code, don't silently succeed. If both are present: write
     the user unit, run `systemctl --user daemon-reload`, `systemctl
     --user enable --now mbregistry.service`, and `loginctl enable-linger
     <user>`.
4. **Uninstall orchestration**, both scopes: stop/disable
   (`systemctl [--user] disable --now`), remove the unit file (and, system
   scope, the udev rule + `daemon-reload` + `udevadm control
   --reload-rules`). Keep `devices.db` unless `purge=True`. Safe no-op
   when nothing is installed at the requested scope, naming the other
   scope if it has an install. Never touches `plugdev` membership.
5. **Status**: for each scope, does the unit file exist and is the
   service active (`systemctl [--user] is-active mbregistry` through the
   runner), reporting the resolved socket/db paths alongside.

## Acceptance Criteria

- [x] `render_systemd_unit`/`render_udev_rule` live in `service.py`; their
      existing golden-file assertions (moved to
      `tests/registry/service/test_service_linux.py`) still pass unchanged
      for system scope.
- [x] A user-scope unit renders with no `RuntimeDirectory=`/
      `StateDirectory=`, `WantedBy=default.target`, and the correct
      `ExecStart=`.
- [x] `linux_install(scope="system", ...)` against a mocked runner calls
      `daemon-reload`, `enable --now`, `udevadm control --reload-rules`,
      `udevadm trigger`, and `usermod -aG plugdev <user>` — and the mock
      shows `usermod` was actually invoked, not merely printed (this is
      the ticket's key acceptance criterion distinguishing it from the old
      `install-service`).
- [x] `linux_install(scope="user", ...)` with a mocked "plugdev missing"
      precondition refuses to install, prints the exact `sudo` remediation
      commands, and returns a distinct nonzero result — and performs no
      writes and no `systemctl` calls when it refuses.
- [x] `linux_install(scope="user", ...)` with a mocked "plugdev present"
      precondition writes the user unit and calls `systemctl --user
      daemon-reload`/`enable --now` and `loginctl enable-linger`.
- [x] `linux_uninstall` for both scopes stops/removes correctly, keeps
      `devices.db` unless `purge=True`, no-ops cleanly when nothing is
      installed (naming the other scope if applicable), and never calls
      `usermod` to remove `plugdev` membership.
- [x] `linux_status` reports not-installed / installed-not-running /
      installed-and-running correctly for both scopes.
- [x] No test invokes a real `systemctl`/`udevadm`/`usermod`/`loginctl`.

## Implementation Plan

**Approach**: Move first (mechanical relocation of the two existing
renderers, re-tested in place), then extend (user-scope variant,
orchestration functions). All new code in `service.py`; `cli.py` is
untouched in this ticket — its existing `render_systemd_unit`/
`render_udev_rule` call sites (`cmd_install_service`) will start failing
to import once the functions move, so either leave a thin
`from mbtools.registry.service import render_systemd_unit, render_udev_rule`
re-import at the top of `cli.py` for the duration of this ticket (removed
in ticket 004 when `cmd_install_service` itself is replaced), or coordinate
so this ticket temporarily keeps `cli.py` importing from the new location.
Either is fine; just don't leave `cli.py` broken between tickets 003 and
004.

**Files to create**:
- `tests/registry/service/test_service_linux.py`

**Files to modify**:
- `src/mbtools/registry/service.py` (add Linux rendering + orchestration,
  receiving the moved functions from `cli.py`)
- `src/mbtools/registry/cli.py` (update the two import lines so
  `cmd_install_service` keeps working via the relocated functions; no
  other change)
- `tests/registry/cli/test_cli_install_service.py` (update its imports of
  `render_systemd_unit`/`render_udev_rule` to the new module path so it
  keeps passing until ticket 004 rewrites it properly)

**Testing plan**: Run `uv run pytest tests/registry/service/
tests/registry/cli/test_cli_install_service.py` to confirm both the moved
golden-file tests and the new orchestration tests pass, with no real
external command invoked.

**Documentation updates**: None yet (ticket 004).

## Implementation Notes

- Extra fix (team-lead request, from the linked issue's own uninstall
  text — "removes the plist/unit, ..., the socket and log files"):
  `macos_uninstall` (ticket 006-002) and `linux_uninstall` (this ticket)
  now also remove the scope's socket file
  (`system_socket_path()`/`user_socket_path()`) unconditionally on every
  real (non-`dry_run`) uninstall, not gated on `purge` — `devices.db`
  remains the one thing kept unless `purge=True`. `macos_uninstall` also
  removes the scope's log file (`_macos_log_path`). Linux has no
  equivalent log *file* to remove — this module's systemd unit sets no
  `StandardOutput=`/`StandardError=`, so `journald` captures output by
  default and there is no file this module ever wrote to clean up; this
  is documented in `linux_uninstall`'s own docstring rather than a
  no-op removal call.
- `render_systemd_unit`'s existing signature is extended with a
  keyword-only `scope: str = "system"` parameter (default preserves
  every pre-ticket call site's behavior unchanged) rather than adding a
  parallel function, per the ticket's own suggestion.
- `_resolve_operating_user`'s logic is duplicated (not imported) into
  `registry.service` as `_resolve_linux_operating_user`, since
  `registry.cli`'s copy still lives in `cli.py` and importing it from
  `service.py` would invert sprint.md's `cli` → `service` dependency
  direction (and would be a real circular import). Ticket 006-004 is
  expected to consolidate the two copies by moving `cli.py`'s version
  into `registry.service` and having `cli.py` import it back — the same
  treatment already given to `render_systemd_unit`/`render_udev_rule`.
- The `--user` preflight's "is the operating user in `plugdev`" check
  (`_linux_user_in_plugdev`) uses `grp`/`pwd`, imported lazily inside the
  function (not at module scope) so importing `registry.service` on
  Windows — exercised by this ticket's own
  `test_linux_functions_dont_depend_on_sys_platform` — never fails just
  because this function exists.
- `linux_install(scope="user", ...)`'s refusal path raises
  `LinuxUserPreflightError` (a new, exported exception) rather than
  returning an `int`/`Path` union — ticket 006-004's CLI wiring is
  expected to catch it and turn it into the "distinct nonzero exit
  code" this ticket's acceptance criteria call for.
