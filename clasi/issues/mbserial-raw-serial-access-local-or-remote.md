---
status: pending
---

# mbserial: raw serial connection to a micro:bit, local or remote

## Description

`mbserial` hands the caller a raw serial connection to a named micro:bit.
It is the successor to `mbdeploy connect` and `mbdeploy serve`'s
`_mbserial` service (brief §5).

## Scope

- `mbserial <name>`: interactive terminal. Also usable as a library that
  returns a serial-like object (the `SocketSerial` idea from mbdeploy's
  `remote.py`).
- **Transport.**
  - Local devices: open the port directly after taking the registry lock,
    or always go through the registry's TCP stream, so local and remote
    behave the same. The brief allows "always get a TCP connection".
  - Remote devices: always go through the owning host's registry.
  - WebSocket/HTTP access is a possible addition.
- **Don't reboot on connect.** Connecting must not reboot a robot (DTR and
  RTS held low) unless `--reset` is given.
- **Control operations.** Reset/BREAK and DTR over the remote stream.
- **Busy boards.** A busy device fails fast, naming the holder: lock kind,
  PID and host.

## Port from

`mbdeploy`: `console.py` (`open_port`, `interact`, `relay_socket`), and
`remote.py` (`SocketSerial`, the connect flow).
