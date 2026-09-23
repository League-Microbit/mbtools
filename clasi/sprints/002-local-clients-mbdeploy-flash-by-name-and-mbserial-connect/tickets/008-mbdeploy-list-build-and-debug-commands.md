---
id: 008
title: 'mbdeploy: list, build, and debug commands'
status: open
use-cases: [SUC-003, SUC-006]
depends-on: ['001', '002']
github-issue: ''
issue: mbdeploy-flash-by-name-via-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbdeploy: list, build, and debug commands

## Description

Add three more subcommands to `mbtools.deploy.cli` (alongside ticket
007's `deploy`): `list`, `build`, `debug`. All three are thin wrappers —
no business logic of their own beyond argument handling and composing
modules built elsewhere.

Per sprint.md's Use Cases SUC-003 and SUC-006:

- **`mbdeploy list`**: calls `registry.client.list()` (ticket 001) and
  `registry.render` (ticket 002) — the identical table/JSON rendering
  `mbregistry list` uses, per spec §4.3. No `mbdeploy`-specific rendering
  code. Registry-unavailable reports the same `EXIT_NO_DAEMON` error
  `mbregistry list` already does.
- **`mbdeploy build [--clean] [--build-cmd ...] [-j N]`**: port today's
  `mbdeploy`'s `builder.py`
  (`/Volumes/Proj/proj/robot-projects/mbdeploy/src/mbdeploy/builder.py`)
  essentially unchanged — shells out to the firmware build script, no
  registry interaction at all (it never touches a board).
- **`mbdeploy debug <name> -- <pyocd args>`**: resolve and lock the named
  device (`kind=debug`) via `registry.client`, then run the given pyOCD
  invocation directly against it (same `[sys.executable, "-m", "pyocd"]`
  invocation shape as tickets 003/005), releasing the lock when the
  pyOCD process exits (success, failure, or Ctrl-C). Per sprint.md's Open
  Questions, this is intentionally a minimal passthrough — no richer
  debug UX is in scope until a stakeholder need names one.

## Acceptance Criteria

- [ ] `mbdeploy list`'s table and `--json` output are identical to
      `mbregistry list`'s (same columns, same data) for the same device
      set — verified by running both against the same daemon and
      diffing output.
- [ ] `mbdeploy list` against an absent registry socket reports
      `EXIT_NO_DAEMON` with the same clear message `mbregistry list`
      gives, not a stack trace.
- [ ] `mbdeploy build` shells out to `build.py` in CWD by default,
      accepts `--clean`/`--build-cmd`/`-j`, and returns the build
      script's own exit code — matching today's `mbdeploy`'s `builder.py`
      behavior.
- [ ] `mbdeploy debug <name> -- <pyocd args>` takes a `debug`-kind lock
      before running pyOCD, and releases it when the pyOCD subprocess
      exits, regardless of its exit code.
- [ ] `mbdeploy debug` against an already-locked device fails fast
      (SUC-005's pattern: holder kind + PID), no retry.
- [ ] `mbdeploy debug` on Ctrl-C (SIGINT) during the pyOCD session still
      releases the lock before the process exits — no leaked lock on an
      interrupted debug session.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/
  tests/deploy/` (must still pass; ticket 007's `deploy` command tests
  are unaffected by this ticket's additions).
- **New tests to write**: `list` output-parity test against
  `registry.render` directly; `build` tests against a fake `build.py`/
  injected `build_cmd` (no real subprocess); `debug` tests against an
  in-process daemon+API and an injected pyOCD runner — lock-taken-before-
  run, lock-released-after-run (including after a simulated non-zero
  exit and a simulated interrupt), and the already-locked fail-fast case.
- **Verification command**: `uv run pytest tests/deploy/`
