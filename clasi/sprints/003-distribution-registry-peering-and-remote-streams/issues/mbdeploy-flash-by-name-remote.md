---
status: in-progress
split_from: mbdeploy-flash-by-name-via-mbregistry.md
sprint: '003'
tickets:
- 003-003
- 003-008
- 003-012
- 003-014
---

# mbdeploy: flash a micro:bit by name through mbregistry (remote)

# mbdeploy: flash a micro:bit by name through mbregistry (remote)

## Description

Extend `mbdeploy deploy <name>` to reach a micro:bit attached to a
**different host**, split out of
`mbdeploy-flash-by-name-via-mbregistry.md` because it depends on the
peering network built in `mbregistry-peering-mdns-and-zeromq.md` — the
local registry must already know which peer owns the named device.

## Scope

- Resolve `<name>` through the local registry to discover the owning
  peer host (via the ZeroMQ event stream already in the local database).
- Request a `flash`-kind lock from the **owning host's** registry, not
  the local one.
- Initiate the flash through the owning host's registry: either its
  minimal flash op takes the hex file and flashes locally there, or
  `mbdeploy` streams the operation to it — same open mechanism decision
  as the local issue, applied across the network (brief §9.4).
- Wait for the owning host's re-probe (UC-003) and relay the new
  announcement back; verify and report, same as the local flow.
- Fail cleanly, not silently, if the owning host's registry becomes
  unreachable mid-operation; rely on the remote registry's own
  lock-release-on-session-drop so the device is not left locked forever.
- All local error flows (transient retry, erase-and-reflash for a locked
  device, blank-board reporting, relay guard) apply equally here, just
  reported back across the peer link.

## Depends on

- `mbdeploy-flash-by-name-via-mbregistry.md` (sprint 2) — the local flash
  flow this extends.
- `mbregistry-peering-mdns-and-zeromq.md` (sprint 3) — the peer event
  stream and remote registry addressing this relies on.

## Port from

`mbdeploy`: `remote.py` (remote flash paths, if any exist there beyond
`SocketSerial`).
