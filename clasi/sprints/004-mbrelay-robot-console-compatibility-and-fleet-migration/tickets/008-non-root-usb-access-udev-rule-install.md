---
id: 008
title: 'Non-root USB access: udev rule install'
status: open
use-cases: [SUC-005]
depends-on: []
github-issue: ''
issue: non-root-usb-access-and-pyocd-permission-hang.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Non-root USB access: udev rule install

## Description

Found during sprint 002 hardware acceptance
(`docs/acceptance/002-hardware.md`): the Nolanet nodes need `sudo` for
every local `mbdeploy deploy`/`debug` and `mbserial`, because user
`eric` has no udev rule or `plugdev`/`dialout` group access to the
DAPLink device (VID:PID `0d28:0204`). `braeburn` (macOS) needs no
`sudo`. Fold the fix into `registry.cli`'s existing `install-service`
command (architecture Decision 9) rather than a new command, since it's
already this project's one "run once to set up the host" entry point.

**Approach**
- Write a udev rule for VID:PID `0d28:0204` covering both the tty device
  node and the raw USB device node CMSIS-DAP/pyOCD uses.
- Add the operating user to whatever group the rule grants access via
  (e.g. `plugdev`), or use `TAG+="uaccess"` if that's a cleaner fit for
  the target distros — decide and document the choice.
- Extend `cmd_install_service` to write the udev rule file (alongside its
  existing systemd-unit write) and print (not silently run, matching the
  existing pattern for the systemd follow-up commands) the
  `udevadm control --reload-rules && udevadm trigger` an operator needs
  to run.
- Must be idempotent and safe to re-run on an already-in-service host
  (architecture Migration Concerns) — re-running `install-service` must
  not disrupt a running `mbregistry.service`.

**Files to create/modify**
- `src/mbtools/registry/cli.py` (extended — `cmd_install_service`, new
  udev-rule template constant alongside `_SYSTEMD_UNIT_TEMPLATE`).
- `tests/registry/test_cli.py` (extended).

**Documentation updates**: `docs/wiki/` (public) gets the udev-rule step
added to the generic install procedure (no machine specifics). The
internal Robot Garage wiki update for the four already-provisioned
Nolanet nodes is sprint 005's fleet-migration work, not this ticket's —
this ticket only needs to make the *fresh-install* path correct; ticket
011 re-runs `install-service` on a real Nolanet node to confirm the
idempotent re-run case.

## Acceptance Criteria

- [ ] `install-service` writes a udev rule matching VID:PID `0d28:0204`
      for both the tty and raw USB device nodes.
- [ ] The rule grants the operating (non-root) user access without
      requiring a logout/login beyond what `udevadm trigger` /
      a replug provides.
- [ ] Re-running `install-service` on a host that already has the rule
      installed is idempotent (no duplicate rule file, no service
      disruption).
- [ ] Documented follow-up commands (`udevadm control --reload-rules`,
      `udevadm trigger`) are printed, not silently executed, matching the
      existing systemd-unit pattern.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/test_cli.py`
  (confirm the existing systemd-unit-write path is unaffected).
- **New tests to write**: assert the udev rule file's contents (VID:PID,
  both device-node match rules) and idempotent re-run behavior, using a
  temp directory instead of real system paths.
- **Verification command**: `uv run pytest tests/registry/test_cli.py`
