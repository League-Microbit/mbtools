---
status: proposal
date: 2026-09-24
---

# robot-console on mbregistry

**Proposal, not yet planned into a sprint.** This covers how robot-console
(`League-Microbit/robot-console`, a Node host process plus a React UI) should
use mbregistry, and what each side has to change. It also covers two
requirements from the stakeholder:

- robot-console must work when no mbregistry is running, by starting its own;
- several mbregistry instances must be able to run on one machine.

## 1. Why

robot-console and mbregistry both do the same four jobs today:

| Job | robot-console | mbregistry |
|---|---|---|
| Find USB boards | Its own 1 s poll of DAPLink `0d28:0204`, joined to HID by USB serial; names each board over SWD (`packages/host/src/devices.ts`, `watchers/usbWatcher.ts`) | `usbwatch.py` + HELLO identification (`identity.py`) |
| Find remote boards | Browses `_mbserial._tcp`, `_mbrelay._tcp` and `_mbflash._tcp` separately (`watchers/mdnsWatcher.ts`) | One peer mesh over `_mbregistry._tcp`, with a device table replicated to every peer |
| Lock a board | `board_owner` and `relay_leases` rows in one process's SQLite. They are not shared across hosts and never expire, so a crash can leave a stale row. WiFi and `mbserial` links have no lock at all (`connect/connector.ts` `resolveExclusivity`) | A per-uid lock that exists only while its connection is open, so it can never go stale. Checked across the fleet |
| Radio names | `GET /names/<name>`, which creates the entry when it doesn't exist | `name_registry`, replicated to every peer. The `names_get` op does not create entries |

If both run on one machine, they compete for the same serial ports. That is
the conflict that led to mbtools. robot-console's own
`docs/design/architecture.md` §12 lists multi-host coordination as out of
scope, and that is the part mbregistry already does.

**Decision:** mbregistry owns every physical board (enumeration, identity,
locks, serial, flashing). robot-console becomes an mbregistry client. robot-console
never opens a USB serial or HID device itself.

## 2. Division of responsibility

| Stays in robot-console | Moves to (or already lives in) mbregistry |
|---|---|
| UI, the WebSocket to the browser, sessions, the telemetry harvester | USB enumeration and identification |
| WiFi robots (`_robotlink._tcp`, port 7654). These are not USB devices | Serial open, break, DTR and RTS |
| The relay command plane (`!CG`, `!MODE`, …), bridging a robot through a relay, the radio sweeper | Locks across the fleet |
| The "owned" rule: a student sees only robots they plugged in | Flashing (`send_hex` + `flash`) |
| Arbitration inside one console process, e.g. a student's connect pre-empting a sweep | Radio name registry |
| | Peer discovery and the fleet device list |

robot-console's architecture already separates watchers (which write rows)
from the connector (which opens links). mbregistry therefore enters
robot-console as **one watcher and one transport adapter**:

- **`mbregistryWatcher`** replaces `usbWatcher` and the `_mbserial`,
  `_mbrelay` and `_mbflash` branches of `mdnsWatcher`. It writes a
  `devices` row per registry device and a link with
  `transport = "mbregistry"`, `address = {endpoint, uid}`.
  `devices.usb_serial` gets the registry `uid`. `devices.id` (FICR.DEVICEID)
  comes from the registry's `serial_payload`, and the name from `device_name`.
- **`mbregistryStream` adapter** implements the existing stream interface
  on top of the §3.2 protocol. `sendBreak()` maps to a `BREAK` frame. A DTR/RTS
  toggle replaces the DAPLink HID reset in `relayBridger`.
- `board_owner` and `relay_leases` stop being authoritative for boards. The
  console holds one mbregistry lock per board for the lifetime of a session,
  and hands that board between its own tasks internally (sweep ↔ student
  session). That replaces the current acquire-then-release-in-`finally`
  pattern.

## 3. Protocol use

### 3.1 Discovery

1. Resolve a local registry (§4).
2. On the local socket, call `list`. This returns the local host's devices
   plus every peer's devices; each peer row carries `endpoint` (`ip:port`)
   and `peer_reachable`.
3. Subscribe to changes with the new `watch` op (§5, item 2).

### 3.2 Opening a session

1. Open TCP to the device's `endpoint`, or to the local instance's remote
   port for a local device.
2. Send `{"op":"lock","uid":…,"kind":"serial"|"relay","label":…}`.
3. Send `{"op":"stream","uid":…}`. After the `ok` reply the connection
   switches to binary frames `[type u8][len u32 BE][payload]`, as defined in
   `stream_frame.py`.
4. Closing the connection releases the lock. A lock can never outlive the
   session, so robot-console needs no stale-lock cleanup.

On a `locked` reply, the UI shows the holder's `label` and `since` (§5, items 3
and 7), e.g. "in use by alice-laptop / robot-console for 12 min". If the lock
looks stale, the UI also shows the `mbregistry unlock --force` command to run on
the owning host.

### 3.3 Flashing

`send_hex` then `flash` on the owning host's remote port. This streams log lines,
then a `result`. robot-console's own dapjs flashing path is retired. SWD
naming of silent boards is the pending mbtools issue on naming silent boards
over SWD; that should move into mbregistry rather than stay in robot-console.

### 3.4 Names

Use `names_get` / `names_set` on the local socket. They replace
`mbrelayRegistry.ts` and its `GET` that creates entries on read.

## 4. Finding or starting a registry (the fallback)

robot-console must work on a machine where no mbregistry is running. It
resolves the local registry in this order, at startup and again whenever the
connection drops:

1. **Explicit:** if `$ROBOT_CONSOLE_MBREGISTRY` is set, use it. The value is a
   socket path, a pipe name, or `host:port`. Never spawn in this case.
2. **Running instance:** try the standard client candidates, the same list
   as `paths.client_socket_candidates()`: this user's socket, then the system
   socket; on Windows, the default pipe. Connect, then send `list` as a
   liveness check.
3. **Previously spawned instance:** try the console-owned socket from step 4,
   in case another robot-console process for this user already started one.
4. **Spawn:** start `mbregistry run` as a child process, in user mode:
   ```
   mbregistry run --instance <host>-console
                  --socket  <console-state>/mbregistry/api.sock
                  --db      <console-state>/mbregistry/devices.db
                  --remote-port 0 --peer-pub-port 0 --peer-snapshot-port 0
                  --pool-port 0 --names-port 0
                  --no-peering            # default; see below
                  --ready-json --exit-with-parent
   ```
   Wait for the ready line (§5, item 6) to learn the ports that were actually
   bound, then continue as in step 2.
5. **Not installed:** mbtools is a declared prerequisite of robot-console,
   installed separately (the stakeholder guarantees it on every machine).
   robot-console looks for the executable at `$MBREGISTRY_BIN`, then on
   `PATH`. If it isn't found, or its version is below the minimum
   robot-console declares, startup reports a clear error naming the
   required version. robot-console does not bundle or install mbtools.
   There is no direct-USB fallback; see §7.

Behaviour of the spawned instance:

- **Lifetime** is tied to the robot-console process that started it (`--exit-with-parent`),
  so it never outlives the console and never becomes a hidden service.
- **Peering is off by default.** On a student laptop the boards are that
  student's. A setting (`mbregistry.shareBoards`) turns peering on, so
  the laptop joins the fleet like any other node.
- **Remote hosts are unaffected.** A remote host whose registry is
  unreachable shows its boards as `peer_reachable: false` (greyed out).
  robot-console never starts a registry on behalf of another host.
- **If a system service is installed later**, the next console start finds
  it in step 2 and does not spawn. Until then, the two instances must not
  compete for boards; the per-board claim in §5, item 5 guarantees that.

## 5. Changes to mbregistry

In priority order. Items 1, 4, 5 and 6 are what "several instances on one
machine" requires.

1. **Every port configurable, and 0 means ephemeral.** `--remote-port`,
   `--peer-pub-port` and `--peer-snapshot-port` already have flag and env
   forms. **The relay pool (7444) and `/names` (7445) do not:** `cmd_run`
   calls `assemble_relay_pool` / `assemble_names_api` with the module
   defaults. Add `--pool-port` / `$MBREGISTRY_POOL_PORT` and `--names-port` /
   `$MBREGISTRY_NAMES_PORT`. Every advertised port (the `_mbregistry` TXT
   record, the `_mbrelay` SRV record and its `registry=` TXT key) must be the
   port actually bound, not the one requested. robot-console already reads the relay and
   names ports from mDNS rather than hard-coding them.
2. **`watch` op** on the local socket and the remote port: after the `ok`
   reply, the server pushes JSON-lines events with the same types as the PUB
   bus (`attach`, `detach`, `identity`, `lock_state`, `name_set`,
   `name_clear`, plus `peer_up` / `peer_down`). This keeps Node clients off
   ZeroMQ.
3. **Lock `label`:** an optional string on `lock`, stored with the holder
   and returned in `locked.holder`, in `list`, and in `lock_state` events.
   It is for display only and never used for authorization. The holder's identity
   stays PID / session-uuid as today.
4. **`--instance NAME`**, which defaults to the short hostname, as now. It
   becomes the mDNS instance name for both `_mbregistry._tcp` and
   `_mbrelay._tcp`, and the value peers store in `peer.host` / `device.host`.
   Today both are `gethostname()`, so two instances on one host collide in
   mDNS and overwrite each other in peers' `peer` table. The internal
   `peering_host` / `instance_host` parameters already exist; this item
   exposes them. It also adds `--pipe NAME` on Windows, where the pipe name
   is currently the constant `\\.\pipe\mbregistry`.
5. **A per-board claim across instances on one machine.** Two instances on one host must never
   both open a board. Before probing a port, an instance takes an exclusive,
   non-blocking claim on the board:
   - Unix: `flock` on `<shared-runtime>/mbtools/claims/<uid>.lock`, a
     world-writable sticky directory. Also `TIOCEXCL` on the open tty.
   - Windows: COM ports are already exclusive to one opener.

   If the claim fails, the instance skips that board and does not list it.
   Because the claim is released automatically when the process dies, a
   crashed instance leaves nothing behind. Add `--only-uid` / `--exclude-uid` for
   deliberate partitioning, e.g. two instances each serving half the hub on a
   test bench.
6. **Spawn support:**
   - `--ready-json` prints one line, `{"ready":true,"instance":…,"socket":…,"ports":{…}}`,
     to stdout once every listener is bound.
   - `--exit-with-parent` makes the daemon exit when stdin reaches EOF.
   - `--no-peering` skips mDNS advertising and browsing and ZeroMQ.
     Peering is currently always on.
7. **Understandable stale-lock breaking.** Locks already die with their
   connection. A lock can still look stuck when its holder is alive but hung,
   or its TCP peer vanished and keepalive hasn't fired yet. Two changes:
   - The holder record gains `since`, an ISO timestamp. `mbregistry list`
     and `locked.holder` show who holds the lock (label, PID or session plus
     IP) and for how long.
   - `mbregistry unlock --force UID|NAME`, run on the owning host, uses the
     local socket and relies on its file permissions. It drops the lock and closes the
     holder's connection or stream, so the holder sees end-of-file rather than
     silently losing exclusivity. The remote TCP port gets no force option.

   robot-console shows the holder and age, plus the exact command to run on
   the owning host. There is no "take over" button for students; this is an
   operator action.
8. **`stream` on the local socket**, not only on the remote TCP port.
   Until this lands, robot-console can use the local instance's remote port
   on `127.0.0.1`.
9. **Later:** once robot-console uses the native protocol, the
   compatibility shims (the relay pool and `/names` on HTTP) can be turned off by default and
   eventually removed.

## 6. Changes to robot-console

1. `mbregistryClient`: JSON-lines over the Unix socket, named pipe or TCP,
   with registry resolution and spawning (§4).
2. `mbregistryWatcher` and the `mbregistryStream` adapter (§2).
3. Add `mbregistry` to the link preference order, in place of `usb`, `mbserial` and
   `mbrelay`. Auto-connect a local, owned board.
4. Delete `usbWatcher`, `swdName`, the direct serialport / node-hid / dapjs
   paths, `mbrelayRegistry.ts`, and the `_mbserial` / `_mbrelay` / `_mbflash`
   branches of `mdnsWatcher`.
5. `board_owner` / `relay_leases` shrink to arbitration inside one process,
   or are replaced by the existing in-memory `keyedMutex` plus the
   pre-emption logic.
6. The "owned" rule: a device is owned by this console if it was ever
   local to the instance the console uses (i.e. `host` is NULL there). That
   keeps today's rule that you see only robots you plugged in, even though
   `list` now returns the whole fleet.
7. The console's own HTTP port (4795) already has a default constant. Make sure
   it is configurable, so two consoles can run on one machine as well.

## 7. Decisions and open questions

Decided by the stakeholder, 2026-09-24:

- **mbtools is a separately installed prerequisite.** robot-console
  depends on it and checks for a minimum version, but neither bundles nor
  installs it. There is no direct-USB fallback: keeping one would keep two
  lock systems alive. The fallback is "start mbregistry", not "skip
  mbregistry".
- **No named sites.** The host is the only grouping. A "location" is one
  LAN's mDNS reach, bridged by `--peer`.
- **No pre-emption across processes.** A lock held by one client cannot
  be taken by another. Inside one console, sweeps yield to students as
  they do today. The only override is the operator's
  `mbregistry unlock --force` on the owning host (§5, item 7), for locks that
  are actually stale. Students are not expected to use it.

Still open:

- **Authentication.** Unchanged: optional shared token, no per-user identity.
  The lock `label` is informational only.
