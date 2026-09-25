---
id: '004'
title: CLI wiring, Windows guard, install-service deprecation, docs
status: done
use-cases:
- SUC-001
- SUC-002
- SUC-003
- SUC-004
- SUC-005
depends-on:
- '002'
- '003'
github-issue: ''
issue: mbregistry-service-install-uninstall-user-system.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI wiring, Windows guard, install-service deprecation, docs

## Description

Wire the `service.py` orchestration built in tickets 002/003 into
`mbregistry`'s CLI as the new `service install|uninstall|status` command
group, retire `install-service` to a hidden deprecated alias, add the
Windows guard, and bring `docs/service.md`/`README.md` up to date.

1. **`build_parser()`**: add a `service` subparser with three
   sub-subcommands:
   - `install (--user | --system, mutually exclusive, required) [--dry-run]`
   - `uninstall (--user | --system, mutually exclusive, required) [--purge]`
   - `status` (no flags)
   Dispatch functions (`cmd_service_install`/`cmd_service_uninstall`/
   `cmd_service_status`) are thin: resolve platform
   (`sys.platform`), and for `darwin`/`linux` call straight into the
   ticket-002/003 `service.py` functions; for anything else (including
   `win32`) print `"mbregistry: service ... is not supported on
   Windows"` (or the actual non-Linux/macOS platform name) to stderr and
   return a nonzero exit code (`EXIT_ERROR`) without importing or calling
   `service.py`'s platform-specific functions at all.
2. **Deprecate `install-service`**: keep its existing subparser
   (`set_defaults(func=...)`), but point it at a new thin wrapper that
   prints a one-line deprecation notice to stderr ("mbregistry:
   install-service is deprecated, use 'mbregistry service install
   --system' — this will be removed in a future release") and then calls
   the same code path as `service install --system --dry-run` — i.e. it
   must still only *write files and print commands*, never actually load
   or start the service, matching users' existing expectations of the old
   command exactly. Remove it from `--help`'s subcommand listing if
   argparse allows hiding a subparser cleanly; otherwise leave it listed
   but clearly marked deprecated in its own `help=` text.
3. **`service status` output**: render a small table/text report (reuse
   `render.py` conventions if it fits, otherwise plain `print` lines is
   fine — this output shape is not specified elsewhere) covering both
   scopes' installed/running state and resolved paths, per SUC-004.
4. **Update `docs/service.md`**:
   - §6 ("Linux: systemd service and udev rule"): replace the manual
     `install-service` walkthrough with `mbregistry service install
     --system` / `--user`, keeping the "what it writes" content
     (unit/udev rule text) since that's still accurate, but describing it
     as written *and started* now.
   - §7 ("macOS: launchd"): replace the two hand-written `tee`/`launchctl`
     blocks with `mbregistry service install --system` / `--user`,
     keeping the rendered plist content shown for reference (now
     generated, not hand-typed).
   - Add an "Uninstall" subsection (there isn't one today beyond §6.4's
     manual `rm` commands) documenting `service uninstall` for both
     scopes and `--purge`.
   - Add a "Windows" note under §8 that `service ...` is not supported
     there and `install-service` remains the only path (deprecated
     elsewhere, unaffected on Windows).
   - Update §11 ("Upgrading") to mention the deprecated `install-service`
     alias and the one-release removal window.
5. **Update `README.md`**: its existing "Installing and running the
   daemon as a service" section (lines ~23-51 per this sprint's research)
   currently says macOS `install-service` "is not supported ... run it
   manually, per docs/service.md#7 ... or in the foreground" — replace
   with a pointer to `mbregistry service install --user|--system` on both
   platforms, keeping the "Windows not hardware-verified" note.
6. **Test file**: rewrite
   `tests/registry/cli/test_cli_install_service.py` to reflect the new
   command shape — split or rename it (e.g.
   `tests/registry/cli/test_cli_service.py` for the new commands, keep a
   slimmed-down `test_cli_install_service.py` for just the deprecated
   alias's behavior) — driving through `main([...])` the same way the
   existing file does, with the runner mocked at the `service.py` boundary
   (patch/inject, matching however tickets 002/003 exposed their runner
   parameter for CLI-level use).

## Acceptance Criteria

- [x] `mbregistry service install --user|--system [--dry-run]`,
      `service uninstall --user|--system [--purge]`, and `service status`
      all work end-to-end through `main([...])`, with the command runner
      mocked — no real `launchctl`/`systemctl`/`udevadm`/`usermod`/
      `loginctl` call in any test.
- [x] `--user`/`--system` are mutually exclusive and required on
      `install`/`uninstall`; omitting both or giving both is a clean
      argparse usage error (exit code 2), not a traceback.
- [x] `mbregistry service install --user` on a simulated Windows platform
      (`monkeypatch.setattr(cli_module.sys, "platform", "win32")`) prints
      "not supported on Windows" and exits nonzero; same for
      `uninstall`/`status`; `install-service` on the same simulated
      platform is unaffected (still renders `sc.exe` commands via the
      existing Windows path).
- [x] `mbregistry install-service` still writes the systemd unit/udev
      rule files and prints commands (unchanged observable behavior for
      an existing caller) but now also prints the deprecation notice to
      stderr, and never calls `systemctl`/`launchctl` to actually start
      anything.
- [x] `docs/service.md` §6/§7 describe only `mbregistry service install`;
      an "Uninstall" subsection exists for both platforms; §8/§11 updated
      per the Description above.
- [x] `README.md`'s service section no longer says macOS `install-service`
      "is not supported."
- [x] Full sprint-scope test run is green:
      `uv run pytest tests/registry/service/ tests/registry/cli/`.

## Implementation Plan

**Approach**: This ticket is the integration point — no new rendering or
orchestration logic, just argparse wiring, the deprecation shim, and
docs. Keep `cmd_service_install`/`cmd_service_uninstall`/
`cmd_service_status` thin (parse args, dispatch to `service.py`, print
its result) — mirroring `cmd_list`/`cmd_run`'s existing thin-dispatcher
shape, per the sprint architecture's Design Rationale.

**Files to modify**:
- `src/mbtools/registry/cli.py` (new subparsers, dispatch functions,
  deprecated alias, Windows guard; remove the now-relocated
  `render_systemd_unit`/`render_udev_rule` re-import shim from ticket 003
  once `cmd_install_service` is rewritten to use the deprecation path)
- `docs/service.md`
- `README.md`
- `tests/registry/cli/test_cli_install_service.py` (rewrite/split, per
  Description item 6)

**Files to create**:
- `tests/registry/cli/test_cli_service.py` (new command group's CLI-level
  tests)

**Testing plan**: Run the full registry test directory
(`uv run pytest tests/registry/`) since this ticket is the sprint's
integration point and the one place regressions across tickets 001-003
would surface. Per the source-code rule, this is still a ticket-scoped
run, not the full suite — the full suite runs once in `close_sprint`.

**Documentation updates**: `docs/service.md` and `README.md`, as detailed
above — this is this ticket's own deliverable, not a follow-up.
