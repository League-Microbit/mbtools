---
status: in-progress
sprint: '004'
tickets:
- 004-008
- 004-009
- 004-011
---

# Non-root USB access on Linux nodes, and a fail-fast for pyOCD permission hangs

## Description

Found during sprint 002 hardware acceptance (`docs/acceptance/002-hardware.md`):

1. **`sudo` is required on the Nolanet nodes.** Every local `mbdeploy deploy`,
   `mbdeploy debug` and `mbserial` there needed `sudo`: user `eric` has no
   udev rule or `plugdev`/`dialout` access to the DAPLink device. braeburn
   (macOS) needs no sudo. Fix it as part of installation:
   - install a udev rule for VID:PID `0d28:0204`, covering both the tty and
     the USB device that CMSIS-DAP/pyOCD uses;
   - add the operating user to the right group;
   - fold both into `mbregistry install-service` and/or the fleet
     Ansible/imaging work.
2. **pyOCD hangs forever without permission.** Without USB access, pyOCD
   retries indefinitely, and `deploy.flash` runs it via `subprocess.Popen`
   with no timeout, so `mbdeploy deploy`/`debug` hang instead of failing.
   - Detect the permission condition up front: check the device node's
     access, or treat an early "no permission"/"unable to open" line in
     pyOCD's output as fatal.
   - Or bound the "no progress" time: no output for N seconds, rather than
     a total-runtime cap that could cut off a slow but real flash.
