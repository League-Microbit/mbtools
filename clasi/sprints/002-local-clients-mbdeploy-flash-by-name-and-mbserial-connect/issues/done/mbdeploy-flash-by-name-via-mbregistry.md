---
status: done
split_into:
- mbdeploy-flash-by-name-remote.md
sprint: '002'
tickets:
- 002-001
- 002-002
- 002-003
- 002-004
- 002-005
- 002-007
- 002-008
- 002-010
---
# mbdeploy: flash a micro:bit by name through mbregistry (local)

## Description

Rebuild `mbdeploy` inside mbtools as a **client** of `mbregistry`. There is
no `mbdeploy serve` any more (brief §4).

Scope here is **local devices only** — flashing a board attached to the
same host `mbdeploy` runs on. Remote flash (through a peer host's
registry) is split out to `mbdeploy-flash-by-name-remote.md` (sprint 3),
which depends on the peering work in
`mbregistry-peering-mdns-and-zeromq.md`.

## Scope

- `mbdeploy deploy <name> [--hex FILE]` flow, local device:
  1. Resolve the name through the local registry.
  2. Lock the device (kind `flash`).
  3. Flash it — locally via pyOCD, or through the registry's minimal
     flash op (brief §9.4, open decision — record whichever path is
     taken as an explicit decision, not an accident of implementation).
  4. Verify the flash, and wait for the registry's post-flash re-probe.
  5. Report the new announcement.
- Keep today's hard-won flash behaviour:
  - retry once on a transient probe signature;
  - erase-then-reflash for a locked device;
  - report explicitly when a board is left blank.
- Build integration (the current `mbdeploy build`) and the relay guard:
  refuse to flash a relay without `--force-relay`.
- Debug and diagnostic commands (pyOCD) run locally under a lock of kind
  `debug`.
- `mbdeploy list` is a thin view over the registry. It can be shared with
  `mbregistry list`.

## Out of scope here

Flashing a device attached to a peer host — see
`mbdeploy-flash-by-name-remote.md`.

## Port from

`mbdeploy`: `flash.py`, `builder.py`, and the relevant parts of `cli.py`
and `remote.py`. Drop `server.py`: its job moves to `mbregistry`.
