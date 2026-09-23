---
id: 008
title: 'mbdeploy: list, build, and debug commands'
status: done
use-cases:
- SUC-003
- SUC-006
depends-on:
- '001'
- '002'
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

- [x] `mbdeploy list`'s table and `--json` output are identical to
      `mbregistry list`'s (same columns, same data) for the same device
      set — verified by running both against the same daemon and
      diffing output.
- [x] `mbdeploy list` against an absent registry socket reports
      `EXIT_NO_DAEMON` with the same clear message `mbregistry list`
      gives, not a stack trace.
- [x] `mbdeploy build` shells out to `build.py` in CWD by default,
      accepts `--clean`/`--build-cmd`/`-j`, and returns the build
      script's own exit code — matching today's `mbdeploy`'s `builder.py`
      behavior.
- [x] `mbdeploy debug <name> -- <pyocd args>` takes a `debug`-kind lock
      before running pyOCD, and releases it when the pyOCD subprocess
      exits, regardless of its exit code.
- [x] `mbdeploy debug` against an already-locked device fails fast
      (SUC-005's pattern: holder kind + PID), no retry.
- [x] `mbdeploy debug` on Ctrl-C (SIGINT) during the pyOCD session still
      releases the lock before the process exits — no leaked lock on an
      interrupted debug session.

## Implementation Notes

- `list`/`build`/`debug` added directly to `deploy.cli.build_parser()`
  (no second parser), per ticket 007's notes. `list` calls
  `registry.client.list()` + `registry.render.render_table`/
  `render_json` verbatim — no `mbdeploy`-specific rendering code.
  `build` stays inline in `deploy.cli` (no new `deploy.builder` module)
  since sprint.md's component diagram shows no registry edge for it —
  ported near-verbatim from today's `mbdeploy`'s `builder.py`, including
  `--verbose` for fidelity even though the ticket's Description only
  calls out `--clean`/`--build-cmd`/`-j`. `debug` is a *bare* pyOCD
  passthrough per sprint.md's own Open Questions entry — the argv after
  `--` reaches `pyocd` completely unmodified, no injected `--uid`.
  `debug`'s own CLI flags (e.g. `--socket`) must precede `<name>` on the
  command line, since everything from `<name>` onward is
  `argparse.REMAINDER`.
- `debug`'s pyOCD subprocess (`_run_pyocd`) uses `subprocess.run(cmd)`
  with inherited stdio, not the streamed/captured invocation
  `deploy.flash`/`registry.flash` use — a debug session can be genuinely
  interactive (`pyocd commander`'s REPL, `gdbserver`'s live progress)
  and needs the real terminal, not a pipe. A Ctrl-C during the session
  raises `KeyboardInterrupt` in `_run_debug`'s own `try`, which reports
  it and returns exit code 130 (conventional "killed by SIGINT"); the
  lock release lives in a `finally` around that so it fires regardless
  of how the pyOCD invocation ended.
- New test file `tests/deploy/test_deploy_cli_list_build_debug.py` (11
  tests): `list` table/JSON parity verified by running both
  `mbtools.deploy.cli.main` and `mbtools.registry.cli.main` against one
  real `RegistryAPIServer` and diffing captured stdout; `build` against
  a fake `build.py`/`--build-cmd` script written to `tmp_path` (a real
  but trivial subprocess, never the firmware toolchain); `debug` against
  the same lightweight server fixture `test_deploy_cli.py` uses, with
  `cli_mod._run_pyocd` monkeypatched (lock-held-during-run asserted from
  inside the fake itself, lock-released-after asserted for success,
  non-zero exit, and a simulated `KeyboardInterrupt`; already-locked
  fail-fast asserted for elapsed time and an untouched holder).
- Team-lead also asked this ticket to make `registry.store.Store`
  internally thread-safe (a separate, additional-scope item — see that
  work's own commit and notes below for the reasoning and verification;
  not part of this ticket's own acceptance criteria above).

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
