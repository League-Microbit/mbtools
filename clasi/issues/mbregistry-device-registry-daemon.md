---
status: pending
---

# mbregistry: the single device-registry daemon (USB watch, identify, database, locks)

## Description

Build `mbregistry`, the **only daemon** in mbtools. It owns enumeration,
identification and exclusive access for every micro:bit on the host. All the
other tools (`mbdeploy`, `mbserial`, `mbrelay`) are its clients. Full
requirements are in `docs/brief.md` §3.

## Scope

- **USB watch.** React to attach and detach events on Linux (udev/netlink)
  and Windows. Polling `comports()` is an acceptable first cut, provided the
  interface allows event sources later.
- **Least-intrusive identification.**
  1. Classify from USB data only: VID:PID `0x0D28:0x0204`, serial number =
     DAPLink UID, port path.
  2. Then do one brief open, with reset, to capture the announcement,
     sending `HELLO` if needed, and close.
  3. Parse both announcement dialects (`DEVICE:<role>:...` and
     `device <role> ...`).
- **Re-probe rules.** Never reopen a device that has not been re-attached or
  flashed. Re-probe exactly once after a flash.
- **Database.** SQL, machine-level (see brief §9.1–9.2 for the SQLite vs
  MySQL and path decisions). One record per device: uid, short uid
  (`uid[16:24]`), port, announcement, role, common name, device name,
  first seen, last seen, connected, last probe, flash count and state.
- **Query service.** A local API (Unix socket; a named pipe on Windows) to
  list, get and find devices by name, short uid or uid. Simple iteration is
  fine.
- **Locks.** Exclusive per-device lock, tied to the holder's PID. The lock is
  released when the PID dies or the connection closes. A lock carries a kind
  (serial / relay / flash / debug) so listings can show *what* holds a
  board.
- **Flash awareness.** The registry knows when a device is being flashed and
  re-probes it afterwards. Whether that happens through a lock of kind
  "flash" or through the registry's minimal flash op is brief §9.4.
- **Minimal flash op.** Only if needed for remote flashing (brief §3.6):
  device by name or port, plus a hex file, flashed with pyOCD by UID,
  streaming log lines back.
- **Service install.** A systemd unit with restart on failure, and a
  Windows service.
- **Listing CLI.** `mbregistry list` (plus `--json`), with the conveniences
  learned from mbrelay:
  - a STATE column (free / locked by kind+pid / no-firmware / gone);
  - a short uid;
  - a FIRMWARE/version column;
  - an error note line under the table for any row that needs one;
  - stable exit codes.

## Out of scope here

Peering between registries (separate issue), and the remote network API
beyond what peering needs.

## Port from

- `mbdeploy`: `devices.py` (announcement parsing, `port_serial_map`,
  pyOCD probe list), `flash.py`.
- `microbit-radio-relay/server`: `inventory.py` state model, `cli.py`
  `_port_holder` (lsof holder naming), `short_uid`.
