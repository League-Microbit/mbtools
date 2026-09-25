---
id: '001'
title: Service artifact paths + command-runner foundation
status: done
use-cases:
- SUC-001
- SUC-002
depends-on: []
github-issue: ''
issue: mbregistry-service-install-uninstall-user-system.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Service artifact paths + command-runner foundation

## Description

Foundation ticket for the whole sprint: add the location-knowledge and
command-execution primitives that tickets 002-004 build on. No CLI-visible
behavior changes yet.

1. **Confirm and document the macOS decision.** Read
   `src/mbtools/registry/paths.py` (`system_socket_path`,
   `user_socket_path`, `system_db_path`, `user_db_path`) — these already
   return correct macOS values (`/var/run/mbregistry/api.sock` /
   `~/Library/Application Support/mbregistry/api.sock`;
   `/Library/Application Support/mbregistry/devices.db` /
   `~/Library/Application Support/mbregistry/devices.db`). Do not change
   these functions. Add a short module-docstring note (or a comment near
   the darwin branches) recording that sprint 006 confirmed these as the
   answer to the "is macOS a supported daemon platform?" decision (yes),
   closing that part of
   `clasi/issues/macos-default-socket-path-needs-a-user-writable-location.md`.

2. **Add service-artifact location helpers to `paths.py`**, parameterized
   by scope (`"user"` / `"system"`), covering what tickets 002-004 need
   and today's `install-service` hardcodes inline in `cli.py`:
   - macOS: LaunchAgent plist path
     (`~/Library/LaunchAgents/org.jointheleague.mbregistry.plist`),
     LaunchDaemon plist path
     (`/Library/LaunchDaemons/org.jointheleague.mbregistry.plist`), and
     the corresponding log paths (`~/Library/Logs/mbregistry.log` /
     `/Library/Logs/mbregistry.log`), matching `docs/service.md` §7
     exactly.
   - Linux: system unit path (reuse the existing
     `DEFAULT_UNIT_PATH = /etc/systemd/system/mbregistry.service` value —
     move the constant here, or have `cli.py`/`service.py` import it from
     here, your call, but there must be exactly one definition), a new
     user-unit path (`~/.config/systemd/user/mbregistry.service`), and the
     existing udev rule path
     (`/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules`, also reused, not
     redefined).
   - Every helper must be overridable-free (no env var/flag precedence
     here — that stays a `registry.service`/`registry.cli` concern, same
     separation as the existing db/socket helpers).

3. **Create `src/mbtools/registry/service.py`** with the command-runner
   seam: a small class or protocol (e.g. `CommandRunner`) with a method
   like `run(argv: list[str]) -> None` (or returning a
   `subprocess.CompletedProcess`-shaped result if a caller needs output,
   e.g. `launchctl print`/`systemctl --user is-active`). Provide:
   - A real implementation that shells out via `subprocess.run` (check,
     capture output as needed).
   - A `dry_run=True` mode (or a separate `DryRunCommandRunner`) that
     prints `would run: <command>` to stderr instead of executing, sharing
     the exact same call sites installer/uninstaller code in tickets 002/
     003 will use — this is what `--dry-run` is built on.
   - Nothing platform-specific yet; this ticket only builds the seam and
     its test double, it does not call it from anywhere real yet (tickets
     002/003 are the first real callers).

## Acceptance Criteria

- [x] `paths.py`'s darwin branches for db/socket paths are unchanged and
      covered by an added comment/docstring note closing the
      "is macOS supported" decision.
- [x] New scope-parameterized helpers exist for: macOS LaunchAgent path,
      macOS LaunchDaemon path, macOS user log path, macOS system log path,
      Linux user unit path. (System unit path and udev rule path are
      reused from their existing constants, not redefined.)
- [x] Every new helper has a unit test asserting its exact returned path
      on each relevant platform (mocking `sys.platform`/`os.path.expanduser`
      the same way existing `paths.py` tests do — check
      `tests/registry/` for the existing pattern first).
- [x] `registry/service.py` exists with a `CommandRunner`-shaped
      abstraction, a real implementation, and a dry-run mode/double, each
      with its own unit test — no test in this ticket invokes a real
      external command.
- [x] No existing test in the repo changes behavior (this ticket adds new
      code, it does not yet wire anything into `cli.py`).

## Implementation Plan

**Approach**: Pure additive change. `paths.py` gains new functions
alongside its existing ones, same style (no I/O beyond `Path`
construction, same `sys.platform`-branching shape as
`system_socket_path`/`user_socket_path`). `service.py` is a brand-new,
otherwise-empty-of-orchestration module — just the runner seam — so this
ticket has no interaction with `cli.py` at all.

**Files to create**:
- `src/mbtools/registry/service.py`
- `tests/registry/service/test_service_runner.py` (or similar — match
  the existing `tests/registry/<module>/` layout)

**Files to modify**:
- `src/mbtools/registry/paths.py` (add helpers + docstring note)
- `tests/registry/test_paths.py` (or wherever existing path tests live —
  check first) — add tests for the new helpers

**Testing plan**: New unit tests only, no regression risk to existing
`paths.py` callers since no existing function signature or return value
changes. Run `uv run pytest tests/registry/` scoped to this ticket's
files.

**Documentation updates**: None yet — `docs/service.md`/`README.md`
updates land in ticket 004, once the CLI surface they document exists.

## Implementation Notes (as built)

`cli.py` was **not** touched, confirming the plan's "no interaction
with `cli.py` at all". For "exactly one definition" of the Linux system
unit path / udev rule path: `paths.py` gained `LINUX_SYSTEM_UNIT_PATH`/
`LINUX_UDEV_RULE_PATH` module constants holding the *same* values as
`cli.py`'s existing `DEFAULT_UNIT_PATH`/`DEFAULT_UDEV_RULE_PATH` (not a
second formula for them — moving/importing them into `cli.py` was
considered and rejected: `cli.py` already imports `api.py`/`store.py`,
both of which import `paths.py`, so `paths.py` importing *from* `cli.py`
would be a real circular import, and would also invert the dependency
direction sprint.md's Design Rationale establishes). Two new tests in
`test_paths.py` (`test_linux_system_unit_path_matches_cli_default_unit_path`,
`test_linux_udev_rule_path_matches_cli_default_udev_rule_path`)
cross-check the values against `cli.py`'s constants so they cannot
silently drift before ticket 006-004 does the actual unification (make
`cli.py` import these two constants from `paths.py` instead of defining
its own copies) as part of its cli.py rewiring work.
