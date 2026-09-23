---
status: pending
---

# mbdeploy: flash a micro:bit by name through mbregistry

## Description

Rebuild `mbdeploy` inside mbtools as a **client** of `mbregistry`. There is
no `mbdeploy serve` any more (brief §4).

## Scope

- `mbdeploy deploy <name> [--hex FILE]` flow:
  1. Resolve the name through the registry (local or peer).
  2. Lock the device.
  3. Flash: locally, or through the owning host's registry for a remote
     device.
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

## Port from

`mbdeploy`: `flash.py`, `builder.py`, and the relevant parts of `cli.py`
and `remote.py`. Drop `server.py`: its job moves to `mbregistry`.
