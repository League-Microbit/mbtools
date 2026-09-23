# mbtools — use cases

Numbered use cases for the `mbtools` system, derived from `docs/brief.md`
and the `clasi/issues/` scoping documents. These describe intended
behavior for sprint planning; they do not resolve any item listed as
"Open decisions" in `specification.md` — where a use case depends on an
open decision, its flow states the assumption it's written against and
flags it.

---

## UC-001: Board attach identification

**ID:** UC-001
**Title:** Registry identifies a newly attached micro:bit
**Actor:** `mbregistry` (daemon), triggered by a USB attach event
**Preconditions:**
- `mbregistry` is running as a service.
- A micro:bit (or any USB device) is plugged into the host.

**Main flow:**
1. The OS reports a USB attach event (or `mbregistry`'s poll of
   `comports()` notices a new port).
2. `mbregistry` inspects the USB descriptor alone — VID:PID, serial
   number, port path — without opening the device.
3. If VID:PID does not match `0x0D28:0x0204`, `mbregistry` ignores the
   device. Use case ends.
4. If it matches, `mbregistry` records the DAPLink UID (serial number),
   derives the short uid (`uid[16:24]`), and creates a device record with
   state "attached, unprobed."
5. `mbregistry` briefly opens the serial port with DTR/RTS held low,
   waits for an announcement line, sending `HELLO` if none arrives
   unprompted, then closes the port.
6. `mbregistry` parses the announcement against both known dialects (see
   `specification.md` §"Announcement formats"). On a parse match, it
   fills in role, common name, device name, and serial payload.
7. `mbregistry` saves the completed record to the database with state
   "connected," last-seen and last-probe timestamps set.
8. If peered with other registries, `mbregistry` publishes the new
   attach (and its identity, once probed) on the ZeroMQ event bus (see
   UC-013).

**Postconditions:**
- The device has one record in the database, connected, with parsed
  identity if the announcement was readable.
- The port is closed; no lock is held.

**Error flows:**
- **No announcement within timeout:** the record is saved with state
  "connected, no-firmware" (or equivalent) and no role/name. It still
  appears in listings, flagged.
- **Port fails to open (in use by something else):** the record is saved
  with state "attached, unprobed" and an error note; `mbregistry` does
  not retry indefinitely — it waits for the next attach/detach cycle or a
  post-flash re-probe trigger.
- **Malformed/unrecognized announcement:** the raw line is stored for
  diagnostics; role/name are left blank and the device is listed as
  unidentified rather than dropped.

---

## UC-002: Board detach

**ID:** UC-002
**Title:** Registry notices a device has been unplugged
**Actor:** `mbregistry`
**Preconditions:** The device has an existing record from UC-001.

**Main flow:**
1. The OS reports a USB detach event (or the poll loop notices the port
   is gone).
2. `mbregistry` marks the device's record "disconnected" (not deleted —
   history like first-seen and flash count is retained).
3. If any lock was held on the device, it is released as part of detach
   (a client holding a lock across a physical unplug cannot meaningfully
   keep it).
4. `mbregistry` updates last-seen and publishes the detach event to any
   peers (UC-013).

**Postconditions:**
- The device's record persists with state "disconnected."
- Any lock on the device is released.
- Listings (UC-004) show the device as gone, not silently drop it.

**Error flows:**
- **Detach and reattach in quick succession (e.g. a flash-triggered USB
  reset):** if this is part of a flash operation, UC-003 governs instead
  — the registry must distinguish "flash re-probe expected" from "user
  unplugged the board" so it doesn't spuriously reset flash-count
  bookkeeping. The exact disambiguation is implementation detail, not
  specified in the brief.

---

## UC-003: Post-flash re-probe

**ID:** UC-003
**Title:** Registry re-probes a device exactly once after it is flashed
**Actor:** `mbregistry`, triggered by a flash completing (via `mbdeploy`
locally, the registry's own minimal flash op, or a remote flash — see
`specification.md` §"Local flashing" open decision)
**Preconditions:**
- The device has an existing record.
- A flash operation against this device's UID has just completed
  (successfully or not — the board's state may have changed either way).

**Main flow:**
1. The flashing party (whichever component actually ran pyOCD) marks the
   device as "just flashed" — incrementing its flash count.
2. `mbregistry` waits for the board to re-enumerate (flashing reboots the
   target) and re-opens the serial port exactly once, the same
   least-intrusive probe as UC-001 step 5 onward.
3. `mbregistry` updates the record's role, name, and announcement from
   the new probe, and updates last-probe.
4. `mbregistry` publishes the identity change to peers if peered.

**Postconditions:**
- The device's record reflects the new firmware's announcement (new
  role/name if changed).
- No further reopening happens until the next flash or detach/reattach
  (UC-001's re-probe rule).

**Error flows:**
- **Board doesn't re-enumerate within a timeout (bad flash, blank
  board):** the record is marked "no-firmware" or "blank," and this is
  surfaced back to whoever is waiting on the flash (`mbdeploy`, per
  UC-008) so it can report a blank-board failure rather than hanging.
- **Announcement changed to a different UID:** should not happen (UID is
  hardware-fixed) — treated as a probe error, not a new device.

---

## UC-004: List devices, local host only

**ID:** UC-004
**Title:** A client lists devices known to one registry
**Actor:** Any user or tool running `mbregistry list` / `mbdeploy list`,
or a client tool querying the registry's list API directly
**Preconditions:** `mbregistry` is running on the host being queried.

**Main flow:**
1. The caller connects to the local registry's query service (Unix
   socket / named pipe on Windows).
2. The caller requests the device list (optionally `--json`).
3. The registry iterates its database (simple iteration is acceptable —
   the brief notes device counts are small) and returns all records:
   name, short uid, role, STATE (free / locked by kind+pid / no-firmware
   / gone), FIRMWARE/version, port.
4. The caller renders a table (or JSON) with an error note line under
   any row that needs one.

**Postconditions:** No state change; read-only.

**Error flows:**
- **Registry not running / socket not present:** the client reports a
  clear "registry unavailable" error with a stable, documented exit
  code — not a stack trace.

---

## UC-005: List devices across peers

**ID:** UC-005
**Title:** A client lists devices visible across the peered registry
network
**Actor:** Same as UC-004, but the local registry is peered with others
(UC-013/UC-014)
**Preconditions:**
- Local `mbregistry` is running and has at least one active peer.
- The requested listing is not restricted to `--local-only` (if such a
  flag exists).

**Main flow:**
1. The caller requests the device list from the local registry, same
   entry point as UC-004.
2. The local registry's database already contains remote devices,
   because every attach/detach/identity-change event from every peer was
   applied as it arrived (UC-013) — this use case does not make a live
   network call per listing; it reads the local database, which is kept
   current by the event stream.
3. The registry returns the combined set, with each remote device tagged
   with its owning host so the caller can tell local from remote at a
   glance.
4. The caller renders the table, distinguishing host where relevant
   (e.g. a HOST column, or `name@host` display).

**Postconditions:** No state change; read-only.

**Error flows:**
- **A peer has vanished but its devices haven't been cleaned up yet:**
  those rows show a "peer unreachable" state rather than being silently
  dropped or shown as if still live — the exact cleanup policy is an
  open decision (`specification.md` §"What gets replicated between
  peers").

---

## UC-006: Lock a device

**ID:** UC-006
**Title:** A client takes an exclusive lock on a device before using it
**Actor:** `mbdeploy`, `mbserial`, or `mbrelay`, acting on behalf of a
user or script
**Preconditions:**
- The device is known to the registry (local or, for a remote lock, the
  owning host's registry) and currently unlocked.

**Main flow:**
1. The client resolves the target device (by name, short uid, or uid)
   through the registry's query API.
2. The client requests a lock of the appropriate kind (`serial`,
   `relay`, `flash`, or `debug`) for that device.
3. For a local client, the registry records the lock against the
   connection's peer PID (candidate mechanism: `SO_PEERCRED` on the Unix
   socket — see `specification.md` §"How are locks held?", still open).
4. The registry grants the lock and returns success; the client proceeds
   to use the device (open the serial port, flash, etc.).

**Postconditions:**
- The device shows as locked, by kind and holder, in any listing
  (UC-004/UC-005) until released.

**Error flows:**
- **Device already locked by someone else:** the registry refuses, and
  returns enough information (kind, holder PID, host) for the client to
  fail fast with a clear message — "locked for flash by pid 4821 on
  togov" rather than a generic busy error. This is the fast-fail path
  UC-010 and UC-011 depend on.
- **Device unknown to the registry:** clear "no such device" error,
  distinct from "locked."

---

## UC-007: Unlock a device, including holder PID death

**ID:** UC-007
**Title:** A device lock is released, normally or because its holder died
**Actor:** `mbregistry`, and the client that held the lock
**Preconditions:** A lock exists on the device (from UC-006).

**Main flow (normal release):**
1. The client finishes its operation (flash complete, serial session
   closed, relay released).
2. The client explicitly releases the lock, or simply closes its
   connection to the registry.
3. The registry marks the device free and updates any listing state.

**Alternate flow (holder process dies):**
1. The client process holding a local lock crashes or is killed without
   releasing the lock.
2. The registry detects the PID is gone (or the underlying connection
   drops, since the lock is tied to both — see `specification.md`
   §"Locking, general model").
3. The registry releases the lock automatically. No manual intervention
   or stale-lock cleanup command is needed.

**Postconditions:**
- The device is free and immediately lockable by the next client.

**Error flows:**
- **Remote holder's session drops uncleanly (network partition, not
  process death):** the brief's stated policy is that a remote lock is
  tied to the network session and releases when it drops — so this is
  handled the same as a local PID death, just keyed on the session
  instead of the PID. Exact mechanism (timeout vs. immediate) is not
  specified.

---

## UC-008: Flash a device by name, local

**ID:** UC-008
**Title:** `mbdeploy` flashes a locally-attached micro:bit by name
**Actor:** A user running `mbdeploy deploy <name> [--hex FILE]`
**Preconditions:**
- The named device is attached to the same host `mbdeploy` is running
  on, and known to that host's registry.

**Main flow:**
1. `mbdeploy` resolves `<name>` through the local registry (UC-004's
   lookup, not full listing).
2. `mbdeploy` locks the device with kind `flash` (UC-006).
3. `mbdeploy` flashes it — directly via pyOCD, or through the registry's
   minimal flash op (open decision, `specification.md` §"Local
   flashing" — this flow is written against either path being possible).
4. `mbdeploy` waits for the registry's post-flash re-probe (UC-003) to
   complete.
5. `mbdeploy` verifies the flash succeeded and reports the new
   announcement (role/name) to the user.
6. `mbdeploy` releases the lock (UC-007).

**Postconditions:**
- The device runs the new firmware.
- The registry's record reflects the new announcement.
- No lock remains held.

**Error flows:**
- **Transient probe error during flash:** `mbdeploy` retries once before
  giving up, per today's hard-won behavior.
- **Device was already locked by someone else:** `mbdeploy` erases and
  reflashes rather than failing outright, per today's behavior for a
  locked device (this is `mbdeploy`'s own established recovery path, not
  a new registry-mediated one).
- **Board comes back blank (flash didn't take):** `mbdeploy` reports
  this explicitly rather than treating a blank board as success or a
  silent hang.
- **Target is a relay and `--force-relay` was not given:** `mbdeploy`
  refuses before locking or flashing anything.

---

## UC-009: Flash a device by name, remote

**ID:** UC-009
**Title:** `mbdeploy` flashes a micro:bit attached to a different host
**Actor:** A user running `mbdeploy deploy <name>` where `<name>`
resolves to a peer's device
**Preconditions:**
- The local registry is peered (mDNS or explicit `--peer`) with the host
  that owns the named device.

**Main flow:**
1. `mbdeploy` resolves `<name>` through its local registry, which already
   knows about the device via the peer event stream (UC-013), and learns
   which host owns it.
2. `mbdeploy` requests a lock on the device — routed to the **owning
   host's** registry, not granted locally, since the device is only
   physically reachable there.
3. `mbdeploy` initiates the flash through the owning host's registry:
   either its minimal flash op takes the hex file and flashes locally
   there, or `mbdeploy` streams the operation to it (mechanism is the
   same open decision as UC-008 step 3, applied across the network).
4. `mbdeploy` waits for that host's registry to re-probe (UC-003) and
   relay the new announcement back, then verifies and reports, same as
   UC-008 steps 5-6.

**Postconditions:** Same as UC-008, but all device-touching operations
happened on the remote host via its registry — `mbdeploy` never opened a
serial port or SWD connection directly to a board that isn't local to
it.

**Error flows:**
- **Owning host's registry is unreachable (peer dropped mid-operation):**
  `mbdeploy` fails the flash attempt cleanly rather than assuming success;
  the remote registry's own lock-release-on-session-drop (UC-007) ensures
  the device isn't left locked forever.
- All the local error flows from UC-008 apply equally, just reported back
  across the peer link.

---

## UC-010: `mbserial` connect, local device

**ID:** UC-010
**Title:** A user gets a raw serial session to a locally-attached board
**Actor:** A user running `mbserial <name>`, or a script using it as a
library
**Preconditions:** Named device is attached locally and unlocked.

**Main flow:**
1. `mbserial` resolves `<name>` through the local registry.
2. `mbserial` locks the device with kind `serial` (UC-006).
3. `mbserial` opens the connection — either directly (DTR/RTS held low)
   or via the registry's TCP stream even for a local device, per the
   "always get a TCP connection" option in `specification.md` §5.2 (open,
   not decided which path is taken).
4. By default the board is **not** reset. `mbserial` hands the user an
   interactive terminal, or a `SocketSerial`-like object to library
   callers.
5. On session end (user exits, or the library caller closes it),
   `mbserial` releases the lock (UC-007).

**Postconditions:** Board unchanged (not rebooted) unless `--reset` was
given; lock released at session end.

**Error flows:**
- **Device already locked:** `mbserial` fails fast, reporting the
  holder's kind, PID, and host (per UC-006's error flow) rather than
  blocking or retrying silently.
- **`--reset` given:** the board is deliberately reset (BREAK on Linux,
  reopen on macOS) as part of connect — an explicit exception to the
  no-reboot default.

---

## UC-011: `mbserial` connect, remote device

**ID:** UC-011
**Title:** A user gets a raw serial session to a board on a peer host
**Actor:** Same as UC-010, but `<name>` resolves to a remote device
**Preconditions:** Local registry is peered with the owning host.

**Main flow:**
1. `mbserial` resolves `<name>` via the local registry, learns the owning
   host, same as UC-009 step 1.
2. `mbserial` requests a `serial`-kind lock from the **owning host's**
   registry (not the local one).
3. `mbserial` opens a stream to the owning host's registry, which carries
   both data and control operations (reset/BREAK, DTR) out-of-band — the
   mechanism (framed protocol, WebSocket control messages, or RFC 2217)
   is an open decision (`specification.md` §"Remote serial streams must
   carry control operations").
4. Same no-reboot-by-default behavior as UC-010 step 4, now enforced over
   the network rather than locally.
5. On session end, the lock is released on the owning host, and the
   remote session's drop (if the connection dies uncleanly) also triggers
   release per UC-007's remote-session flow.

**Postconditions:** Same as UC-010, but mediated entirely through the
owning host's registry.

**Error flows:**
- Same busy/fail-fast behavior as UC-010, naming host as well as PID.
- **Control operation requested but the transport can't carry it yet**
  (if the out-of-band channel isn't implemented for a given transport):
  this is a real gap tied to the open decision above — flagged here as a
  use case that cannot be fully satisfied until that decision is made
  and built.

---

## UC-012: `mbrelay` connect to a robot by name

**ID:** UC-012
**Title:** A user connects to a robot over radio through a relay board
**Actor:** A user running `mbrelay connect [robot[@host]]`
**Preconditions:** At least one relay board (role contains `RELAY` or
`BRIDGE`) is known to the registry (local or peer) and free.

**Main flow:**
1. `mbrelay` asks the registry which attached devices are relays and free
   (it does not enumerate or probe boards itself, per
   `specification.md` §6).
2. `mbrelay` picks one — the named robot's preferred relay if specified,
   or any free relay otherwise — and locks it with kind `relay`
   (UC-006), local or remote per UC-009/UC-011's pattern depending on
   where that relay lives.
3. `mbrelay` resets and normalizes the relay on acquire: BREAK/reset,
   `HELLO`, `!VER?`, then RAW250 / frag off / echo off / P7 / ch0 grp10,
   verified with `?`.
4. `mbrelay` looks up `robot`'s channel/group in the name registry,
   sends `!CG`, then `!GO` to enter the data plane, then an optional
   `PING`.
5. The user gets an interactive terminal (or `--send`/`--expect`
   scripting runs against the session).
6. On release, `mbrelay` sends `!DEFAULTS` to restore the relay to its
   normalized resting state, then releases the lock.

**Postconditions:** Relay board returned to its normalized default state;
lock released.

**Error flows:**
- **No free relay:** `mbrelay` reports this rather than blocking
  indefinitely.
- **Named robot not found in the name registry:** reported as a distinct
  error from "no free relay" — a naming problem, not a resource
  problem.
- **robot-console compatibility path:** if a caller is robot-console
  rather than `mbrelay` itself, the `_mbrelay._tcp` pool port / `/names`
  HTTP contract applies instead of this CLI flow — see
  `specification.md` §"robot-console compatibility contract." This is
  called out because breaking it fails silently (mistuned robot), unlike
  the errors above.

---

## UC-013: Peer discovery via mDNS

**ID:** UC-013
**Title:** Two registries on the same LAN find each other and peer
**Actor:** Two or more `mbregistry` instances on the same network segment
**Preconditions:** Both hosts run `mbregistry`; mDNS traffic reaches both
(same subnet / no mDNS-blocking network gear).

**Main flow:**
1. Each `mbregistry` advertises an mDNS service (e.g. `_mbregistry._tcp`)
   on startup.
2. Each `mbregistry` also browses for that same service type.
3. When registry A sees registry B's advertisement (and vice versa), they
   open a ZeroMQ connection between them.
4. Each side sends the other a snapshot of its current device database.
5. Each side applies the peer's snapshot into its own database, tagging
   those records with the peer's hostname.
6. From then on, each side publishes its own attach/detach/identity-change
   events (UC-001/UC-002/UC-003) onto the ZeroMQ link, and applies the
   other's events as they arrive.

**Postconditions:** Both registries' databases contain both hosts'
devices, kept current by the live event stream (this is what UC-005 reads
from).

**Error flows:**
- **One side vanishes (process dies, network drops):** the other side
  detects the lost connection and marks that peer's devices unreachable
  (or removes them — not decided, see `specification.md` §"What gets
  replicated between peers"), rather than leaving stale "connected"
  records.
- **mDNS doesn't reach across the network (different subnet, VLAN,
  mDNS-blocking switch):** no peering happens via this use case at all —
  that's exactly the gap UC-014 exists to cover.

---

## UC-014: Explicit `--peer` across networks

**ID:** UC-014
**Title:** A registry or client joins a peer network mDNS can't reach
**Actor:** A user starting `mbregistry --peer HOST[:PORT]`, or a client
tool (e.g. `mbrelay --peer HOST[:PORT]`) that needs to reach a network
mDNS doesn't span
**Preconditions:** The target `HOST:PORT` is a reachable `mbregistry`
instance (or one node of an existing peered network), even though it's
outside this host's mDNS broadcast domain.

**Main flow:**
1. The user supplies `--peer HOST[:PORT]` at startup (or on a client
   command, for a one-off cross-network operation).
2. Rather than waiting for mDNS discovery, the process connects directly
   to `HOST:PORT`'s ZeroMQ endpoint.
3. From there, peering proceeds the same as UC-013 steps 4-6 (snapshot
   exchange, then live event stream) — the transport differs (direct
   connect vs. mDNS-triggered) but the peering protocol is the same.
4. Because that peer may itself be peered with others, this host now
   effectively joins the same network the explicit peer belongs to, not
   just a two-node link.

**Postconditions:** Same as UC-013 — a shared, live device view — but
achieved across a network boundary mDNS cannot cross.

**Error flows:**
- **`HOST:PORT` unreachable or not an `mbregistry`:** the process reports
  a clear connection error at startup rather than silently running
  unpeered.
- This is explicitly called out in the brief as "nice to have," so a
  minimal version (works, but without every robustness feature of
  mDNS-triggered peering) is an acceptable first cut.

---

## UC-015: Service install and restart

**ID:** UC-015
**Title:** `mbregistry` is installed as a managed service and recovers
from a crash
**Actor:** An installer/operator (human or Ansible), and the host's
service manager (systemd on Linux, Service Control Manager on Windows)
**Preconditions:** `mbtools` is installed on the host; the operator has
privileges to install a service unit.

**Main flow:**
1. The operator runs `mbregistry`'s install step (e.g. `mbregistry
   install-service`, or an Ansible role that does the equivalent), which
   writes a systemd unit (Linux) or registers a Windows service.
2. The service is enabled to start on boot and is started.
3. `mbregistry` begins watching USB and probing devices (UC-001).
4. At some later point, `mbregistry` crashes or is killed unexpectedly.
5. The service manager (systemd's `Restart=` policy, or the Windows
   service recovery configuration) restarts it automatically.
6. On restart, `mbregistry` re-scans currently-attached USB devices
   (since it missed whatever attach/detach events occurred while it was
   down) and rebuilds its live state; its persistent identity data
   (`/var/lib/mbregistry/` or the Windows equivalent — open decision,
   `specification.md` §"Where do the files live?") survives the crash
   even though `/run`-level live state does not.

**Postconditions:** `mbregistry` is running again with an accurate view
of currently-attached devices; any locks held by clients whose registry
connection dropped during the crash are gone (per UC-007), so clients
see a clean slate rather than phantom locks.

**Error flows:**
- **Service fails to start (bad config, permissions):** the service
  manager's own failure reporting applies (journal on Linux, event log
  on Windows) — `mbregistry` does not need bespoke crash-loop detection
  beyond what the service manager already provides.
- **Migration from the old fleet's `mbdeploy` package:** where an old
  `mbdeploy serve` or `mbrelay.service` unit already exists on a node,
  install must retire those rather than running both — see
  `specification.md` §"Migration" and §"Package name clash" (open
  decision on exact mechanics).
