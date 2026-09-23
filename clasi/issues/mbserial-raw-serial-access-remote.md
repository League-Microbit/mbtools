---
status: pending
split_from: mbserial-raw-serial-access-local-or-remote.md
sprint: '003'
---

# mbserial: raw serial connection to a micro:bit (remote)

# mbserial: raw serial connection to a micro:bit (remote)

## Description

Extend `mbserial <name>` to reach a micro:bit attached to a **different
host**, split out of `mbserial-raw-serial-access-local-or-remote.md`
because it depends on the peering network built in
`mbregistry-peering-mdns-and-zeromq.md`, and on that issue's remote
stream protocol carrying control operations out-of-band.

## Scope

- Resolve `<name>` via the local registry, learning the owning host from
  the peer event stream.
- Request a `serial`-kind lock from the **owning host's** registry.
- Open a stream to the owning host's registry carrying both data and
  control operations (reset/BREAK, DTR) out-of-band — the mechanism
  (framed protocol, WebSocket control messages, or RFC 2217) is an open
  decision (spec §"Remote serial streams must carry control operations")
  that this issue's sprint should resolve or explicitly record as
  deferred.
- Same no-reboot-by-default behavior as the local flow, enforced over the
  network.
- On session end, release the lock on the owning host; an unclean session
  drop also triggers release via the owning registry's
  session-drop-releases-lock behavior.
- Busy-board errors name host as well as lock kind and PID.

## Depends on

- `mbserial-raw-serial-access-local-or-remote.md` (sprint 2) — the local
  connect flow this extends.
- `mbregistry-peering-mdns-and-zeromq.md` (sprint 3) — the peer event
  stream, remote registry addressing, and out-of-band control channel
  this relies on.

## Port from

`mbdeploy`: `remote.py` (`SocketSerial`, the connect flow, remote-facing
parts).
