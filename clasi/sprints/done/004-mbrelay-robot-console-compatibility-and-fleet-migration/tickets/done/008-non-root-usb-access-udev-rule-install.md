---
id: 008
title: 'Non-root USB access: udev rule install'
status: done
use-cases:
- SUC-005
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

- [x] `install-service` writes a udev rule matching VID:PID `0d28:0204`
      for both the tty and raw USB device nodes.
- [x] The rule grants the operating (non-root) user access without
      requiring a logout/login beyond what `udevadm trigger` /
      a replug provides.
- [x] Re-running `install-service` on a host that already has the rule
      installed is idempotent (no duplicate rule file, no service
      disruption).
- [x] Documented follow-up commands (`udevadm control --reload-rules`,
      `udevadm trigger`) are printed, not silently executed, matching the
      existing systemd-unit pattern.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/test_cli.py`
  (confirm the existing systemd-unit-write path is unaffected).
- **New tests to write**: assert the udev rule file's contents (VID:PID,
  both device-node match rules) and idempotent re-run behavior, using a
  temp directory instead of real system paths.
- **Verification command**: `uv run pytest tests/registry/test_cli.py`

## Implementation Notes

- **File path deviation**: this repo's actual per-command test-file
  convention (already established before this ticket) is
  `tests/registry/cli/test_cli_install_service.py`, not the flat
  `tests/registry/test_cli.py` this ticket's own text names (test module
  basenames must stay globally unique across `tests/`, and a `cli/`
  subpackage of one-file-per-subcommand already existed). Extended that
  file instead; `uv run pytest tests/registry/cli/test_cli_install_service.py`
  is the equivalent scoped command.
- **Rule covers three device nodes, not two.** Alongside the tty node
  (`mbserial`/pyOCD's serial transport) and the raw `usb` node (pyOCD's
  CMSIS-DAP v2/WinUSB transport), a third `KERNEL=="hidraw*"` rule is
  included for pyOCD's CMSIS-DAP v1/HID transport (per the dispatch
  brief's own pointer to pyOCD's udev guidance) — some DAPLink firmware
  negotiates HID instead of WinUSB, and without this rule that firmware
  would still need `sudo`.
- **Decision: grant access via both `GROUP="plugdev"`/`MODE="0660"` *and*
  `TAG+="uaccess"`, on every match rule** — not a single either/or choice
  between the two options the ticket's Description names. Rationale (see
  `render_udev_rule()`'s own docstring in `cli.py` for the full version):
  `TAG+="uaccess"` is the cleaner mechanism when it applies (immediate,
  no group/relogin needed) but is scoped to a systemd-logind seat's
  *active session*, and this ticket's actual target hosts (the four
  Nolanet nodes, CLAUDE.md's hardware test hosts table) are reached
  **only over SSH**, with no local seat for logind to track — so
  `uaccess` alone risks silently granting nothing on exactly the hosts
  this ticket exists for. `GROUP="plugdev"` has no such dependency and is
  reliable headless, at the cost of needing a *new login session* (e.g. a
  new SSH connection) after `usermod -aG plugdev <user>` for that session
  to pick up the new group — this is standard Linux behavior for any
  group-based grant, not something `udevadm trigger` can substitute for.
  Writing both costs nothing and covers both cases.
- **Caveat for ticket 011 (real-hardware verification)**: on the actual
  Nolanet nodes, expect the flow to be `install-service` (as root) →
  `udevadm control --reload-rules && udevadm trigger` (or replug) → **a
  new SSH session** (for the `plugdev` group membership to take effect in
  that session) → `mbdeploy deploy`/`mbserial` without `sudo`. A shell
  session already open at the time `usermod` runs will still show the old
  group list even after `udevadm trigger`/replug.
- **New CLI flags**: `--udev-output` (mirrors `--output`; defaults to
  `DEFAULT_UDEV_RULE_PATH` = `/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules`)
  and `--user` (names the operator in the printed `usermod` line; defaults
  to `$SUDO_USER`, then `$USER`, then `getpass.getuser()` — `$SUDO_USER`
  is checked first since `install-service` is normally run via `sudo`,
  where `getpass.getuser()`/`$USER` alone would report `root`).
- **Idempotency**: both the systemd unit and the udev rule are written
  unconditionally to their own fixed path with deterministic content on
  every call — a re-run simply rewrites the same bytes to the same path
  (no duplicate file). `cmd_install_service` never starts, stops, or
  restarts `mbregistry.service`, so "no service disruption" holds by
  construction, not because of any extra check.
- **Documentation deviation**: the ticket's own "Documentation updates"
  note points at `docs/wiki/` for the generic install procedure, but this
  repo (`mbtools`, distinct from the `mbdeploy` repo whose `CLAUDE.md`
  that convention comes from) has no `docs/wiki/` directory and no
  existing "generic install procedure" page (`README.md` has no install
  section; `docs/design/` covers architecture, not operator procedure).
  Documented the decision and the full rule/flag contract in
  `render_udev_rule()`'s and `cmd_install_service()`'s own docstrings in
  `cli.py` instead, matching this module's own established
  docstring-as-documentation convention (every other function in this
  file documents its own design decisions the same way). Flagging this
  here in case a later sprint does stand up `docs/wiki/` and wants this
  ticket's content folded in.
