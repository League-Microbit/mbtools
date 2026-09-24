---
id: '006'
title: 'CI: windows-latest test job'
status: done
use-cases:
- SUC-002
depends-on:
- '005'
github-issue: ''
issue: mbregistry-windows-platform-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CI: windows-latest test job

## Description

Add a GitHub Actions workflow (this repo currently has none —
`.github/workflows/` does not exist yet) that runs the full `mbtools`
test suite on a `windows-latest` runner. This is the sprint's one piece
of **real, non-hardware verification** of the Windows-specific code from
tickets 002-005 — it proves the code imports and runs correctly under
actual Windows/CPython, and exercises the `ctypes`/`kernel32` calls in
`registry.api_windows`/the `sc.exe`-text rendering in
`registry.service_windows` against a real Windows OS, without needing
Windows *hardware* (no USB micro:bit, no real SCM service lifecycle
required for the fake-based unit tests to pass).

**Approach**
- New workflow file (e.g. `.github/workflows/test.yml`), with at least
  two jobs/matrix entries: the existing implicit "run tests" job
  extended to a matrix including `ubuntu-latest` (or whatever the
  project already runs locally — establishing baseline CI is implicitly
  part of this ticket, since none exists) and `windows-latest`.
- Each job: checks out the repo, sets up `uv` (or Python + `uv sync`),
  runs `uv run pytest`.
- **Optional, implementer's judgment** (sprint.md Open Questions): a
  real `sc.exe create`/`sc.exe delete` round-trip against a throwaway
  service name (e.g. `mbregistry-ci-test`), proving
  `service_windows.render_windows_service_install()`'s text is not just
  well-formed but actually accepted by a real SCM — GitHub's
  `windows-latest` runner has Administrator rights by default, so this
  is genuinely possible without any special CI configuration. If
  included, the job must delete the test service afterward regardless
  of pass/fail (a cleanup step), so a flaky run never leaves a stray
  service registered on the (ephemeral, single-use) runner.

**Files to create/modify**
- `.github/workflows/test.yml` (new).

**Documentation updates**: none required — this is CI infrastructure,
not user-facing behavior. (If the README is being written in ticket 009
around the same time, a CI status badge is a nice-to-have but not an
acceptance criterion here.)

## Acceptance Criteria

- [x] A GitHub Actions workflow exists that runs `uv run pytest` on
      `windows-latest`.
- [x] The same workflow (or a sibling job) also runs the suite on at
      least one non-Windows platform (`ubuntu-latest`), since this repo
      currently has no CI at all — this ticket establishes baseline CI,
      not just the Windows leg.
- [x] The workflow triggers on push/PR to the default branch (standard
      GitHub Actions convention — matches how a reader would expect CI
      to run).
- [x] Every test in `tests/registry/api_windows/`,
      `tests/registry/service_windows/`, `tests/registry/paths/`, and
      `tests/registry/cli/`'s new platform-branch tests passes on the
      `windows-latest` job — this is the ticket's actual verification
      payload, not just "a workflow file exists."
- [x] If the optional real `sc.exe create`/`delete` round-trip is
      included, it cleans up the test service unconditionally (even on
      failure) and does not leave any persistent state on the runner
      (ephemeral runners make this low-risk, but the workflow should
      still clean up correctly as good practice). — Not included
      (implementer's judgment, per the ticket's own "Optional" note):
      the real-pipe round-trip test
      (`test_real_pipe_round_trip_server_and_client_together`) already
      gives this ticket's actual required payload — real `ctypes`
      verification against a live Windows kernel — without the added
      flakiness surface of a real service-registration round-trip; this
      AC is vacuously satisfied since nothing was included to clean up.

## Testing

- **Existing tests to run**: the full suite, on both platforms the new
  workflow covers — this ticket's own subject is "does the suite pass
  in CI," so its testing *is* the workflow itself; verify locally with
  `uv run pytest` before trusting the CI run.
- **New tests to write**: none in `tests/` — this ticket is CI
  configuration, not application code.
- **Verification command**: push the branch (or open a draft PR) and
  confirm both the `ubuntu-latest` and `windows-latest` jobs go green;
  locally, `uv run pytest` as a sanity check before pushing.

## Implementation Notes

- This ticket depends on 005 because the Windows-specific code paths it
  verifies don't exist as a runnable whole until `cmd_run`/
  `cmd_install_service` actually dispatch to them — running CI before
  005 lands would only test the standalone modules, not the integrated
  behavior tickets 002-005 together claim to provide.
- Be explicit in this sprint's hardware-acceptance document
  (`docs/acceptance/005-hardware.md`, ticket 010) that a green
  `windows-latest` CI job is **not** hardware verification — it proves
  the code runs correctly under real Windows/CPython against fakes, not
  that a real USB micro:bit attach is seen or that SCM restart-on-crash
  actually restarts a crashed process on real hardware.

### CI workflow (`.github/workflows/ci.yml`, not `test.yml`)

- Matrix over `ubuntu-latest`/`macos-latest`/`windows-latest` (added
  macOS too, beyond the AC's minimum, since it's this project's own
  primary dev platform and was free to include), Python 3.13 via
  `astral-sh/setup-uv@v5`. Triggers on `push` (no branch filter, so a
  sprint branch under active development triggers a run directly — used
  throughout this ticket's own iteration) and `pull_request` targeting
  `main`.
- `uv run pytest` alone was not enough to make a real hang diagnosable:
  added `pytest-timeout` (dev dependency group; `[tool.pytest
  .ini_options] timeout = 60` in `pyproject.toml`) and run with
  `-v --tb=short -ra --timeout=60` (not `-q -v` together — pytest's
  verbosity is additive/subtractive across `-v`/`-q` and the two cancel
  back out to the quiet grouped-dots default, which is what hid every
  failing test's own name behind an unlabeled `F`/`E` during this
  ticket's own first real Windows run). `timeout-minutes: 20` on the job
  itself is a backstop, not the primary mechanism.
- `scripts/run_tests_ci.py`: a thin `pytest.main()` + `os._exit()`
  wrapper, used only by the CI step (see its own docstring). Needed
  because pytest itself finished the suite in ~132s on windows-latest,
  but the *job* then sat idle for another ~18 minutes before
  `timeout-minutes` killed it — Python's normal interpreter shutdown
  waiting on something (most likely a non-daemon thread some dependency
  left running) after pytest had already printed its full summary. Not
  chased further (would need a real Windows debugging session this
  project doesn't have); `os._exit()` immediately after
  `pytest.main()` returns sidesteps it the same way `pytest-timeout`'s
  own thread method already sidesteps a single hung test.
- Skipped the optional real `sc.exe create`/`delete` round-trip — see
  the AC above for why.

### Real Windows-only bug found and fixed: `WindowsPipeAPIServer.stop()` deadlock

- `test_real_pipe_round_trip_server_and_client_together`
  (`tests/registry/client/test_client_windows.py`, `skipif` off
  Windows, first ever run for real here) deadlocked: the accept thread
  blocked inside a synchronous `ConnectNamedPipe`, while the
  `stop()`-calling thread blocked *inside its own* `CloseHandle` call on
  that same handle. Ticket 003/005's original design (`stop()` closes
  the pending pipe handle from another thread to unblock the accept
  loop) assumed Windows honors that the way POSIX `socket.close()`
  wakes a blocked `accept()`/`recv()` from another thread — it does not,
  for a *synchronous* named-pipe handle; `CloseHandle` itself can block
  until the pending I/O completes, which nothing will ever satisfy once
  the daemon is shutting down.
- Fixed with `CancelSynchronousIo` (kernel32, `ctypes`-reachable, no
  `pywin32` — sprint.md Decision 1 intact): added
  `_Win32PipeAPI.cancel_pending_connect` (`OpenThread` +
  `CancelSynchronousIo`, targeting the accept *thread*, not the pipe
  *handle*) and `WindowsPipeAPIServer._cancel_accept_thread_pending_io`
  (`stop()`'s new unblock path — polls briefly to close the narrow race
  against `_accept_loop`'s own create-then-connect window). The accept
  thread still closes its own handle, on its own thread, in its
  existing exception path — never a cross-thread `CloseHandle` again.
  `tests/registry/api_windows/test_api_windows.py`'s
  `_ScriptedLifecycleWin32` fake updated to model
  `cancel_pending_connect` as the unblocking call instead of
  `close_handle`.

### Real Windows test-suite gaps found and fixed (not app-code bugs)

Two categories, both anticipated by this ticket's own "skip only tests
that genuinely need POSIX-only facilities" framing (team-lead's
dispatch instructions):

1. **No `socket.AF_UNIX` at all** on this project's `windows-latest`
   runner's own Python build (`AttributeError`, not a connect/bind
   failure) — ~115 tests across 12 files, all in fixtures that
   construct a real `AF_UNIX` socket or `RegistryAPIServer` directly
   (written before Windows was a supported platform). Added
   `tests/conftest.py`: a shared `requires_af_unix` marker plus a
   `pytest_collection_modifyitems` hook, one documented reason, instead
   of repeating the same `skipif` ~115 times. Production code already
   avoids `AF_UNIX` on `win32` — only these tests' own fixtures needed
   it.
2. **Tests that assumed "this suite never runs on real Windows"**
   instead of forcing `sys.platform` explicitly — broke the instant
   that assumption became false (this ticket's whole point). Fixed two
   different ways depending on *when* the platform-dependent value is
   decided:
   - A **live** `sys.platform` check (`assemble_daemon_and_api`,
     `resolve_local_api_address`, `_Win32ServiceAPI`,
     `run_as_windows_service`, `cmd_install_service`'s own dispatch):
     forced a non-`"win32"` value via `monkeypatch` instead of relying
     on the ambient host — these stay real, exercised regression tests
     on every CI leg, including `windows-latest`, not skips.
   - An **import-time-frozen** value (`DEFAULT_SOCKET_PATH`,
     `DEFAULT_DB_PATH`, `api_windows._kernel32`/`_advapi32`,
     `DEFAULT_UDEV_RULE_PATH`'s `str()` via `WindowsPath`): a
     post-import `monkeypatch.setattr(..., "platform", ...)` cannot
     retroactively change an already-bound default parameter or
     module-level constant, so these got a targeted
     `skipif(sys.platform == "win32", reason=...)` instead, each
     explaining specifically why forcing platform wouldn't have worked
     (so a future reader doesn't "fix" it by copying the monkeypatch
     pattern from elsewhere in the same file).
   - `test_sweep_releases_lock_of_real_dead_subprocess` (locks) needs
     the POSIX `sleep` binary and `os.kill(pid, 0)` liveness semantics
     together — `skipif`, per the dispatch instructions' own named
     example.

### Additional team-lead scope: shared `resolve_local_api_address` helper

Ticket 005 flagged that `mbdeploy`/`mbserial`/`mbrelay` still resolved
the local registry address via `registry.client.resolve_socket_path(
args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH)` directly at every
`RegistryClient`-construction call site (3 in `deploy/cli.py`, 1 in
`serial/cli.py`, 2 in `relay/cli.py`) — `DEFAULT_SOCKET_PATH` is `None`
on `sys.platform == "win32"`, so with no `--socket`/
`$MBREGISTRY_SOCKET` override this raised `TypeError` from `Path(None)`
before ever reaching `RegistryClient`, i.e. every local-registry
command in all three CLIs was unconditionally broken on Windows.

- Moved `registry.cli`'s private `_resolve_local_api_address` platform-
  dispatch logic to a new public
  `mbtools.registry.client.resolve_local_api_address(flag_value,
  env_var)`. No circular-import risk: `registry.client` already imports
  `registry.api`/`registry.api_windows`, and `registry.paths` (source
  of `default_pipe_name`) has zero internal dependencies.
  `registry.cli` keeps `_resolve_local_api_address` as a thin alias
  (`_resolve_local_api_address = resolve_local_api_address`) — same
  object, so its existing tests
  (`tests/registry/cli/test_cli_run_windows.py`) needed no changes.
- Wired all 6 affected call sites (`deploy/cli.py` ×3, `serial/cli.py`
  ×1, `relay/cli.py` ×2) through `resolve_local_api_address` instead.
- Tests: 5 new tests in `tests/registry/client/test_client.py` mirroring
  `test_cli_run_windows.py`'s own `_resolve_local_api_address` coverage
  (flag wins, env wins, win32 default, off-Windows default, off-Windows
  unaffected — the latter two needed the same live-check-vs-frozen-
  value fixes described above once actually run on real Windows), plus
  one regression test per CLI (`test_deploy_cli.py`,
  `test_mbserial_cli.py`, `test_cli.py` for relay) simulating `win32`
  and asserting `cli_mod.resolve_local_api_address(None,
  cli_mod._SOCKET_ENV_VAR)` returns the pipe name instead of raising.

### Verification

- Local: full suite `uv run pytest -q` (and via
  `scripts/run_tests_ci.py`) — 870 passed, 3 skipped, unchanged from
  before this ticket's own test-suite fixes (no `skipif` here fires off
  real Windows).
- CI: green on all three legs —
  <https://github.com/League-Microbit/mbtools/actions/runs/35999415493>
  (`ubuntu-latest` 8m23s, `windows-latest` 2m14s, `macos-latest` 1m57s).
  Iteration history for anyone reading the Actions history: run
  35991547688 (initial push) hung 21+ min on the pipe-close deadlock
  above with no diagnosis possible (`-q` gave no test names) → cancelled;
  35993849258 added `pytest-timeout` and named the deadlock, but `-q -v`
  together still hid every *other* failure's name → 35995334473 fixed
  the deadlock and the verbosity flags, which surfaced the full
  AF_UNIX/platform-assumption list above and *also* the post-summary
  shutdown hang → 35999156812/35999415493 fixed both; 35999415493 is
  the first fully green run.
