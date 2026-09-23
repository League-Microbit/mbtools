---
status: done
sprint: '001'
tickets:
- 001-010
---

# Hardware acceptance testing on Nolanet (meili, loki, hodr, magni) and braeburn

## Description

Unit tests run against fakes. Each sprint also needs a **real-hardware
acceptance run** on the dedicated test hosts listed in `CLAUDE.md` (Hardware
test targets):
- four Nolanet Pi nodes, one micro:bit each;
- `braeburn`, a macOS x86_64 host with one micro:bit.

The development Mac has no micro:bits.

## Scope

- **Deploy mbtools to a host from the working tree.** Provide a repeatable
  script, e.g. `scripts/deploy-test-host.sh <host>`:
  - build a wheel;
  - copy it across;
  - install into a venv on the host (install `uv` if it's missing);
  - run the daemon, as a systemd service on Linux or in the foreground or
    under launchd on braeburn.
- **Retire the old mbdeploy on the Nolanet nodes.** Stop, disable and remove
  `mbdeploy.service` (user `jtl`, `/home/jtl/mbdeploy`). Nothing on it needs
  saving. It holds `/dev/ttyACM0`, so it must be gone before `mbregistry`
  runs.
- **Acceptance checks for each sprint:**
  - **Sprint 001:** on every host, `mbregistry` identifies the board.
    Check uid, short uid, port and announcement. Check that unplug and
    replug is detected (simulated with a USB unbind/rebind where possible;
    otherwise note it as manual). Check that a lock held by a killed PID is
    released. Check that a flash through the minimal flash op triggers
    exactly one re-probe, and that the new announcement is recorded.
  - **Later sprints** add their own checks: mbdeploy by name, mbserial local
    and remote, peering across the five hosts, mbrelay with relay firmware.
- **Test firmware** comes from GitHub releases (`MICROBIT.hex`): radio relay,
  nezha-robot-template, Remote-Joystick-Student. Flashing different firmware
  onto a board is how "the announcement changed after a flash" gets tested.
- **Record results** in `docs/acceptance/<sprint>-hardware.md`: host,
  firmware, observed output, pass/fail.
- **Fleet change:** when the old mbdeploy is removed from the nodes, update
  the Robot Garage wiki's mbdeploy page ("Current status" table), or record
  that it is stale if it cannot be edited.
