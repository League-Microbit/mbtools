---
status: pending
---

# A flash over the local socket leaves the record stale: no lock log lines, no flash-triggered re-probe

## Measured: denning, tigez, 2026-10-16

`mbdeploy deploy --hex ... tigez` (local socket, run with sudo) flashed the
board successfully, including the CTRL-AP mass-erase recovery from a
`0x67` sector-erase failure. Afterwards:

- The board answered `ID` on `/dev/ttyACM0` with the new firmware.
- `mbregistry list` kept showing `no-firmware` for several minutes, and
  `mbregistry rescan` did not change it.
- The journal showed no `flash lock acquired/released` lines and no
  "awaiting re-enumeration" line for this flash, unlike a remote
  (TCP) flash from robot-console minutes later, which logged both and
  re-identified the board 13 s later.

So the local-socket flash path either never takes the registry's
`flash`-kind lock, or releases it without going through
`LockManager.release`, so `Daemon._on_flash_release` never arms the
re-probe. The record only recovered when a later remote flash released its
own lock.
