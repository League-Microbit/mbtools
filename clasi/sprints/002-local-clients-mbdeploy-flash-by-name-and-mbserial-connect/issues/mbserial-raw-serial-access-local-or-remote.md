---
status: in-progress
split_into:
- mbserial-raw-serial-access-remote.md
sprint: '002'
tickets:
- 002-009
- 002-001
- 002-010
---
# mbserial: raw serial connection to a micro:bit (local)

## Description

`mbserial` hands the caller a raw serial connection to a named micro:bit
attached to the **local host**. It is the successor to `mbdeploy connect`
and `mbdeploy serve`'s `_mbserial` service (brief §5).

Remote access (a board on a peer host) is split out to
`mbserial-raw-serial-access-remote.md` (sprint 3), which depends on the
peering work in `mbregistry-peering-mdns-and-zeromq.md`.

## Scope

- `mbserial <name>`: interactive terminal. Also usable as a library that
  returns a serial-like object (the `SocketSerial` idea from mbdeploy's
  `remote.py`).
- **Transport, local device.** Open the port directly after taking the
  registry lock, or always go through the registry's TCP stream so local
  and remote behave the same — the brief allows "always get a TCP
  connection" (spec §5.2, open decision; record whichever path is taken).
- **Don't reboot on connect.** Connecting must not reboot a robot (DTR and
  RTS held low) unless `--reset` is given.
- **Busy boards.** A busy device fails fast, naming the holder: lock kind
  and PID.

## Out of scope here

Connecting to a device attached to a peer host — see
`mbserial-raw-serial-access-remote.md`.

## Port from

`mbdeploy`: `console.py` (`open_port`, `interact`, `relay_socket`), and
`remote.py` (`SocketSerial`, the connect flow, for whatever of it is
reusable locally).
