---
status: in-progress
split_into:
- mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md
- default-mbregistry-compatibility-shims-off-once-robot-console-is-native.md
sprint: '007'
tickets:
- 007-002
- 007-004
- 007-005
- 007-006
- 007-007
---

# Run several mbregistry instances on one machine, and let robot-console spawn one

## Description

robot-console should stop enumerating USB boards and keeping its own
locks, and become an mbregistry client. When no registry is reachable,
robot-console starts its own local `mbregistry run`. Several mbregistry
instances must be able to run on one machine. The full design, including
the robot-console side, is in `docs/design/robot-console-integration.md`.

This issue covers the **multi-instance and spawn** part of the mbregistry
side (§5 items 1, 4, 5 and 6 of that doc). It was split on 2026-09-24:

- the client-API part (watch, lock label, `since` and `unlock --force`,
  `stream` on the local socket) is
  `mbregistry-api-for-robot-console-watch-lock-label-unlock-force-local-stream.md`;
- defaulting the compatibility shims off is
  `default-mbregistry-compatibility-shims-off-once-robot-console-is-native.md`.

## Work in mbregistry

1. **Every port configurable, and 0 means ephemeral.** Add
   `--pool-port` / `$MBREGISTRY_POOL_PORT` and `--names-port` /
   `$MBREGISTRY_NAMES_PORT`. Today `cmd_run` passes the defaults 7444/7445 to
   `assemble_relay_pool` / `assemble_names_api`, and there is no flag for either.
   Every advertised port must be the port actually bound, not the one
   requested: the `_mbregistry` TXT record, the `_mbrelay` SRV record, and
   its `registry=` TXT key.
2. **`--instance NAME`**, defaulting to the short hostname. Use it for the
   `_mbregistry._tcp` and `_mbrelay._tcp` instance names and for
   `peer.host` / `device.host`. Today both come from `gethostname()`, so two
   instances on one host collide in mDNS and in peers' `peer` table. Also add
   `--pipe NAME` on Windows, where the pipe name is currently the constant
   `\\.\pipe\mbregistry`.
3. **A per-board claim across instances on one machine**, so two instances
   never open the same board:
   - On Unix, `flock` on `<shared-runtime>/mbtools/claims/<uid>.lock`, plus
     `TIOCEXCL` on the open tty.
   - On Windows, COM ports are already exclusive to one opener.

   An instance that can't get the claim skips that board. Also add
   `--only-uid` / `--exclude-uid`.
4. **Spawn support:**
   - `--ready-json` prints one line with the instance name, socket and bound
     ports once every listener is up.
   - `--exit-with-parent` exits when stdin reaches EOF.
   - `--no-peering` turns off mDNS and ZeroMQ, which are always on today.

## Acceptance

- Two `mbregistry run` instances on one machine, each with its own
  `--instance`, socket, database and ephemeral ports, both appear
  correctly in mDNS and on a third peer. A board plugged in is listed by
  exactly one of them.
- A parent process can spawn `mbregistry run --ready-json
  --exit-with-parent --no-peering`, read the bound ports, and the child
  exits when the parent dies.
- `docs/service.md` (port table) and `docs/design/registry-api.md` are
  updated for the new flags.

## Decisions (stakeholder, 2026-09-24)

- mbtools is installed separately and is a prerequisite of robot-console.
  Nothing bundles or auto-installs it.
- No named sites.
- No pre-emption between clients. The only override is the operator's
  `unlock --force`, for stale locks.

The robot-console half (client, watcher, stream adapter, spawn logic,
removing its USB/SWD/lock code) needs a matching issue in the robot-console
repo.
