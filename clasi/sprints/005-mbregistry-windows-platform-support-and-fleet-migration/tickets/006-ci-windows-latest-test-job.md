---
id: '006'
title: 'CI: windows-latest test job'
status: open
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

- [ ] A GitHub Actions workflow exists that runs `uv run pytest` on
      `windows-latest`.
- [ ] The same workflow (or a sibling job) also runs the suite on at
      least one non-Windows platform (`ubuntu-latest`), since this repo
      currently has no CI at all — this ticket establishes baseline CI,
      not just the Windows leg.
- [ ] The workflow triggers on push/PR to the default branch (standard
      GitHub Actions convention — matches how a reader would expect CI
      to run).
- [ ] Every test in `tests/registry/api_windows/`,
      `tests/registry/service_windows/`, `tests/registry/paths/`, and
      `tests/registry/cli/`'s new platform-branch tests passes on the
      `windows-latest` job — this is the ticket's actual verification
      payload, not just "a workflow file exists."
- [ ] If the optional real `sc.exe create`/`delete` round-trip is
      included, it cleans up the test service unconditionally (even on
      failure) and does not leave any persistent state on the runner
      (ephemeral runners make this low-risk, but the workflow should
      still clean up correctly as good practice).

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
