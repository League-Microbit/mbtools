# mbtools — specification

Source: `docs/brief.md` (stakeholder direction, 2026-09-23) plus the five
`clasi/issues/` scoping documents. This specification preserves every
requirement and constraint from the brief, reorganized by program and by
cross-cutting concern. It does not resolve anything the brief leaves open
— see [Open decisions](#open-decisions) at the end, which restates the
brief's §9 as recommendations, not decisions.

## 1. Why this project exists

Two existing tools each find, identify and open every micro:bit on a
host, independently:

- **`mbdeploy`** (`Busboombot/mbdeploy`) flashes firmware and runs
  `mbdeploy serve`, a per-board TCP serial and flash daemon.
- **`mbrelay`** (`microbit-radio-relay/server`) runs a pool of
  RADIOBRIDGE relay boards behind a TCP port, plus a name registry.

Run both on one machine and they compete for the same serial ports.
mbrelay probes every micro:bit, resets robots with DTR and BREAK, and
takes a lock mbdeploy doesn't honour. mbdeploy writes `HELLO` into live
relay sessions. Neither tool's flash path coordinates with the other.

The fix is architectural: **enumeration, identification and exclusive
access belong to exactly one daemon.** Every other tool is a client of
it. `mbtools` is a new repo (`League-Microbit/mbtools`) holding that
daemon and the client tools, as four programs.

## 2. The four programs, at a glance

| Program | Kind | One-line job |
|---|---|---|
| `mbregistry` | **The only daemon.** Runs as a service and is restarted on failure | Watch USB, identify micro:bits, keep the device database, grant exclusive locks, and be the one network endpoint other hosts talk to |
| `mbdeploy` | Client tool | Flash firmware to a micro:bit by name. Owns the UI and the "fancy" parts (pyOCD diagnostics, debugging, retries, verification) |
| `mbserial` | Client tool | Give a raw serial connection to a micro:bit, local or remote |
| `mbrelay` | Client tool | Connect to a RADIORELAY/RADIOBRIDGE board and send/receive radio messages, as today's mbrelay does |

The stakeholder's list also called the third tool "mb console"; the name
that settled was **`mbserial`**.

### Guiding principle: one daemon

> "I prefer it if we could avoid having any but one server here, one
> daemon. I really like it if all the other tools were primarily local
> in that they operated on remote things by connecting to a single
> common remote server, which would be MB Registry."

There is no `mbdeploy serve` and no `mbrelay serve`. A tool acting on a
remote micro:bit connects to the **remote host's `mbregistry`**.

## 3. `mbregistry` — the device registry daemon

### 3.1 Watching USB
- Listen for USB **attach and detach** events on **Linux and Windows**.
- Run as a real service (systemd on Linux, a Windows service on Windows)
  so it is restarted if it dies.
- Polling `comports()` is an acceptable first cut, provided the interface
  allows swapping in real event sources (udev/netlink on Linux) later.

### 3.2 Identifying a device, least intrusive first
1. On attach, decide from USB information alone whether the device is a
   micro:bit, **without opening it** — VID:PID `0x0D28:0x0204`.
2. For a micro:bit, collect everything available without opening it:
   name, serial number (the DAPLink UID), serial port path, and any other
   descriptor data.
3. Create the device's record.
4. Then **briefly open** the serial port and capture its announcement
   line, sending `HELLO` if needed. "You should just be able to open it
   with a reset and then close it."
5. Save the result in the database.
6. Parse both announcement dialects (see
   [Announcement formats](#5-announcement-formats) below).

### 3.3 When to re-probe
- A device that has **not been detached and reattached, and not been
  flashed, is never reopened** to re-check its announcement.
- After a **flash**, the announcement may have changed (new firmware, new
  role, new name). The registry must know a flash happened and re-probe
  the device exactly once afterwards.

### 3.4 The database
- Holds the micro:bits the host has seen. Updated on new attachments and
  on devices that have been dropped.
- Kept at **machine level, not per user**, "somewhere in var".
- One record per device: uid, short uid (`uid[16:24]`), port,
  announcement, role, common name, device name, first seen, last seen,
  connected, last probe, flash count and state.
- The stakeholder specified **a SQL database, "a MySQL database"** — see
  [Open decisions](#1-which-sql-database). SQLite is the recommendation,
  not the decision.
- Other programs **consult** it through a service; it "doesn't have to be
  complicated because there are not a lot of devices" — simple iteration
  over all records is acceptable.

### 3.5 Query service
- A local API (Unix socket; a named pipe on Windows) to list, get and
  find devices by name, short uid or uid. Simple iteration is fine.

### 3.6 Locking
- The registry provides **exclusive locks**. A client asks it to lock a
  micro:bit for exclusive access.
- **A lock is tied to the holder's PID. If that process dies, the lock is
  released.**
- A lock carries a **kind** (`serial` / `relay` / `flash` / `debug`) so
  listings can show *what* holds a board, not just *that* it's held.
- `mbdeploy`, `mbserial` and `mbrelay` all take a lock before touching a
  device.
- See [Lock semantics](#6-lock-semantics) for the local-vs-remote holder
  model, which the brief leaves open.

### 3.7 Flashing, minimal
- If a remote flash needs something local to the device, the registry
  provides a **very lightweight flash operation**:
  - identify one micro:bit by serial port or by name;
  - hand it a hex file;
  - it flashes (pyOCD by UID), streaming log lines back.
- "If you don't need to put flashing in MB Registry, don't." The only
  reason to include it is that remote flashing needs a local agent next
  to the device, and under this architecture that agent is `mbregistry`,
  not an `mbdeploy` server.
- Everything beyond the minimal flash stays in `mbdeploy`: pyOCD
  debugging, diagnostics and the user interface.
- The registry knows when a device is being flashed (whether via a
  `flash`-kind lock or via its own minimal flash op — brief §9.4, still
  open) and re-probes it afterwards.

### 3.8 Service install and listing CLI
- A systemd unit with restart on failure; a Windows service.
- `mbregistry list` (plus `--json`), with the conveniences learned from
  mbrelay:
  - a STATE column (free / locked by kind+pid / no-firmware / gone);
  - a short uid;
  - a FIRMWARE/version column;
  - an error note line under the table for any row that needs one;
  - stable exit codes.

### 3.9 Networking and peering
- `mbregistry` **advertises itself with mDNS**.
- mDNS is used **only** for what genuinely needs the network — finding
  registry peers. Device-level announcements do **not** go over mDNS.
- When one registry sees another's mDNS advertisement, they **establish a
  peering relationship and join a ZeroMQ network**.
- The ZeroMQ network carries **attach and detach events**, and identity
  changes (e.g. after a re-probe following a flash). Every registry
  instance receives them and adds them to its own database, tagged with
  the owning host. When you look for a device, you see **every device on
  any host reachable over the ZeroMQ network**.
- **Joining late.** A new peer receives a snapshot, then the event
  stream. When a peer vanishes, its devices are marked unreachable or
  removed.
- **Explicit peers (nice to have):** running `mbregistry` (or a client
  such as `mbrelay`) with a specified peer (`--peer HOST[:PORT]`)
  attaches to whatever network that peer is in. This makes it possible to
  **cross networks** that mDNS can't span today.
- **Remote access.** Clients act on a remote device through **that
  host's** registry: lock, open a stream, flash. The remote stream
  protocol must carry control operations (reset/BREAK, DTR), because a
  plain TCP pipe cannot — see [Open decisions §5](#5-remote-serial-streams-must-carry-control-operations).
- Open: what else is replicated besides attach/detach (lock/busy state?
  the name registry?), and how a peer's devices are cleaned up when it
  disappears — see [Open decisions §9](#9-what-gets-replicated-between-peers).

### 3.10 Out of scope for the registry issue
Peering between registries is its own issue/ticket track, and the remote
network API is scoped no wider than what peering needs.

### 3.11 Port from
- `mbdeploy`: `devices.py` (announcement parsing, `port_serial_map`,
  pyOCD probe list), `flash.py`.
- `microbit-radio-relay/server`: `inventory.py` state model, `cli.py`
  `_port_holder` (lsof holder naming), `short_uid`.

## 4. `mbdeploy` — flash by name

There is no `mbdeploy serve` any more. `mbdeploy` is rebuilt as a
**client** of `mbregistry`.

### 4.1 Flow (`mbdeploy deploy <name> [--hex FILE]`)
1. Resolve the name through the registry (local or peer).
2. Get a handle on the device and lock it.
3. Flash: directly for a local board, or through that host's registry for
   a remote one.
4. **Make sure the flash worked**: verify, and wait for the registry's
   post-flash re-probe of the announcement.
5. Report the new announcement.

### 4.2 What stays in mbdeploy ("the fancy work")
- pyOCD debugging, failure diagnostics and retries:
  - retry once on a transient probe signature;
  - erase-then-reflash for a locked device;
  - explicit reporting when a board is left blank.
- Building firmware (today's `mbdeploy build`).
- The relay guard: refuse to flash a relay without `--force-relay`.
- Debug and diagnostic commands (pyOCD) run locally under a lock of kind
  `debug`.
- The user interface / CLI.

### 4.3 Listing
`mbdeploy list` is a thin view over the registry; it can share
implementation with `mbregistry list`.

### 4.4 Local flashing — open question
Whether `mbdeploy` runs pyOCD itself after taking a lock of kind `flash`,
or always flashes through the registry's minimal flash op, is not
decided — see [Open decisions §4](#4-local-flashing).

### 4.5 Port from
`mbdeploy`: `flash.py`, `builder.py`, and the relevant parts of `cli.py`
and `remote.py`. Drop `server.py` — its job moves to `mbregistry`.

## 5. `mbserial` — raw serial access

Successor to `mbdeploy connect` and `mbdeploy serve`'s `_mbserial`
service.

### 5.1 Interface
- `mbserial <name>`: interactive terminal.
- Also usable as a library returning a serial-like object (the
  `SocketSerial` idea from mbdeploy's `remote.py`).

### 5.2 Transport
- **Local devices:** either open the port directly after taking the
  registry lock, or always go through the registry's TCP stream so local
  and remote behave identically. The brief allows "always get a TCP
  connection, and it'll deal with it whether it's local or non-local" —
  not decided which.
- **Remote devices:** always go through the owning host's registry.
- WebSocket/HTTP access is a possible addition.

### 5.3 Reset semantics
- **Don't reboot on connect.** Connecting must not reboot a robot (DTR
  and RTS held low) unless `--reset` is given. See
  [Reset semantics](#7-reset-semantics) below for the platform-specific
  mechanics this depends on.
- Control operations (reset/BREAK, DTR) must be deliverable over the
  remote stream, not just locally.

### 5.4 Locking and busy boards
- Locks the device for the duration of the connection.
- A busy device fails fast, naming the holder: lock kind, PID and host.

### 5.5 Port from
`mbdeploy`: `console.py` (`open_port`, `interact`, `relay_socket`), and
`remote.py` (`SocketSerial`, the connect flow).

## 6. `mbrelay` — relay access

Rebuilt as a client that does **only** the relay protocol. It never
finds, enumerates, probes or lists devices itself — that is entirely the
registry's job (`relay.cli`'s own list()-through-the-registry-client
resolution, never a raw serial/socket scan).

### 6.1 Picking a relay
- `mbrelay connect <robot>[@<host>]` names a **robot** (a name-registry
  name), not a relay device directly. It resolves a free relay (by
  scanning the local registry's own `list` op for a device whose `role`
  contains `RELAY`/`BRIDGE` and is unlocked — a local one preferred when
  `@host` isn't given), optionally scoped to `@host`, and locks it
  (kind `relay`).
- `mbrelay names get/set/clear/list <name> [channel] [group]` operates on
  the name registry directly (through the registry daemon's own
  `names_get`/`names_set`/`names_clear`/`names_list` ops — the CLI never
  opens `devices.db` itself), for operator use without going through
  robot-console's HTTP compatibility endpoint (§6.6).

### 6.2 Reset and normalize
On acquire: `HELLO`, `!VER?`, then RAW250 / frag off / echo off / P7 /
ch0 grp10, verified with `?` — `relay.protocol.RelayControl`'s
`hello`/`firmware_version`/`normalize`, called directly (not its
`reset_and_normalize` convenience wrapper, which unconditionally closes
the channel again — fine for a locally-attached relay, but would drop a
remote relay's registry lock along with the connection it closes; see
`relay.cli`'s own module docstring). On release: `!DEFAULTS`
(`clear_stored_config`) only — the *next* acquire's `normalize()` already
forces the board back to defaults unconditionally, so release doesn't
need to repeat that work.

### 6.3 Tuning to a robot
`!CG` from the name registry (a **non-creating** lookup — an
unregistered robot name is reported as a distinct error, not silently
derived, unlike robot-console's own `/names` endpoint), `!GO`, then an
optional `PING` (skip with `--no-probe`).

### 6.4 Scripting and terminal
`--send LINE` (repeatable) / `--expect REGEX` scripting, or — with
neither given — a raw-mode interactive terminal (`--escape`, default
Ctrl-`]`), as in today's `mbrelay connect`.

### 6.5 Name registry
Robot name → (channel, group) mapping, with conflict reporting, in the
registry's `name_registry` table (`registry.store`, sprint 004 ticket
001), replicated to peers over the existing ZMQ event bus (ticket 002).
`mbrelay`'s own writes (`names set`/`names clear`, and `connect`'s own
lookup) go through the registry daemon's local API — new
`names_get`/`names_set`/`names_clear`/`names_list` ops on
`registry._api_base.BaseAPIServer`/`registry.api.RegistryAPIServer` — so
the daemon (which already owns both the `Store` and the `PeerDiscovery`
publisher) is what fires `publish_name_set`/`publish_name_clear` right
after its own write, the same "the component that owns the write fires
the callback" shape `LockManager`/`Daemon` already use for lock/attach
events. See
[Open decisions §6](#6-where-does-the-relay-pool-live-and-what-about-robot-console).

### 6.6 robot-console compatibility
robot-console relies on the `_mbrelay._tcp` pool port with
reset-by-reconnect, TXT `registry=`, and `GET /names/<name>`. Decide
between a compatibility endpoint hosted by the registry, migrating
robot-console, or a transition period. See the full contract in
[robot-console compatibility contract](#8-robot-console-compatibility-contract)
below — breaking it silently mistunes moved robots.

### 6.7 Port from
`microbit-radio-relay/server/src/mbrelay`: `relay.py`, `session.py`
(acquire/release and sniffing only), `transport.SerialChannel`,
`registry.py`, `naming.py`, `client.py`. Do **not** port `inventory.py`,
`firmware.py`, `admin.py`, or the advertiser half of `mdns.py` — those
responsibilities move to `mbregistry`.

## Cross-cutting concerns

### 1. Device identity and short uid
- micro:bit USB ID is VID:PID `0x0D28:0x0204`; the USB serial number is
  the DAPLink UID.
- The **last 8 hex characters of the UID are identical on every board**
  (`6e052820`, a DAPLink suffix). The per-board field is **`uid[16:24]`**,
  which is mbrelay's `short_uid`. Use it for any short identifier
  anywhere in `mbtools` (CLI output, logs, database keys where a short
  form is wanted).

### 2. Announcement formats
Both dialects must parse:
- `DEVICE:<role>:<common>:<name>:<serial>`, e.g.
  `DEVICE:RADIOBRIDGE:relay:zavaz:4076631795`.
- `device <role> <common> <name> <serial>`, the robot dialect, e.g.
  `device NEZHA2 robot tovez ...`.
- Relay roles contain `RELAY` or `BRIDGE`.

This parsing is shared logic (ported from `mbdeploy`'s `devices.py`) that
`mbregistry` owns; no client tool re-parses announcements independently.

### 3. Locking, general model
- The registry provides **exclusive locks**, one per device, tied to the
  holder.
- A lock carries a **kind**: `serial`, `relay`, `flash`, or `debug`, so
  listings can show what a board is doing, not just that it's busy.
- **Local holder identity:** tied to a PID. If that process dies, the
  lock releases. (Mechanism: see
  [Open decisions §3](#3-how-are-locks-held).)
- **Remote holder identity:** a PID means nothing across hosts — the
  brief's own recommendation is to tie a remote lock to the network
  session, released when it drops. Not yet decided as policy across all
  three client tools.
- All three client tools (`mbdeploy`, `mbserial`, `mbrelay`) take a lock
  of the appropriate kind before touching a device, with no exceptions.

### 4. Reset semantics
- On Linux, closing and reopening a port does **not** reset a DAPLink
  target; a **serial BREAK** does. On macOS, reopening does reset it.
  This platform difference is load-bearing for both the daemon's attach
  probe and any client's connect path.
- `mbdeploy` deliberately opens ports with DTR and RTS low so that
  "connecting to a robot must not reboot it." Today's `mbrelay` opens
  with the defaults, which resets the board — that is correct for relay
  acquire (which wants a known state) but wrong for a plain serial
  connect.
- The brief's "open it with a reset" at attach-probe time (§3.2 step 4)
  is acceptable — that's the registry's own brief probe, not a user
  session.
- A later `mbserial` connect to a robot should still **not** reboot it
  unless asked (`--reset`).
- pyserial `exclusive=True` is an advisory `flock` on POSIX — it only
  stops other `flock` users, so it is not a substitute for the registry's
  own lock.
- Flashing (pyOCD `flash -t nrf52833 --uid <uid>`) uses SWD over USB, not
  the serial port, but it reboots the target — which is why a flash must
  still take the device's lock even though it isn't touching the tty.

### 5. Relay protocol reference
(Full reference: `microbit-radio-relay/docs/radio-relay-protocol.md`.)
- The command plane is `\n`-terminated lines with `#` replies: `?`,
  `!VER?`, `!C`, `!CG`, `!CGT`, `!N`, `!P`, `!MODE`, `!FRAG`, `!ECHO`,
  `!DEFAULTS`, `HELLO`.
- `!GO` enters the data plane. There is **no in-band escape** — getting
  back to the command plane needs a reset.
- Relays are normalized on acquire and on release (RAW250, frag off, echo
  off, P7, ch0/grp10) — see §6.2 above.
- The name registry maps robot name → (channel, group), stored today in
  `names.json` and served over HTTP `GET/PUT/DELETE /names/<name>`.

### 6. Locking mechanics (candidate design, not decided)
See [Open decisions §3](#3-how-are-locks-held) — this is the
stakeholder's own sketch, not yet a decision:
- Local clients: over a Unix socket, recording the peer PID
  (`SO_PEERCRED`). The lock releases when the PID dies *or* the
  connection closes.
- Remote clients: tied to the network session, released when it drops.

### 7. Robot-console compatibility contract
`robot-console` (repo `league-projects/microbit/robot-console`) depends
on three things from today's mbrelay, all of which `mbtools` must either
preserve, replace with an equivalent, or explicitly retire with
migration:
1. An `_mbrelay._tcp` mDNS service whose SRV record points at a raw
   byte-pipe pool port, where each connection gets a freshly reset relay
   in the command plane.
2. TXT record `registry=<port>` on the same host.
3. `GET /names/<name>` returning `{channel:int, group:int, source}`.

If any of these disappear, robot-console **silently** falls back to
derived addresses and mistunes robots that have been moved. This is a
hard compatibility constraint, not a nice-to-have — the failure mode is
silent and field-visible (a moved robot gets mistuned), not a crash
someone would notice and report. Whatever `mbtools` does here needs to
either keep robot-console working unmodified, or come with an explicit,
tracked migration of robot-console itself. See
[Open decisions §6](#6-where-does-the-relay-pool-live-and-what-about-robot-console).

### 8. Migration
- The repo is `League-Microbit/mbtools`, at
  `/Users/eric/proj/robot-projects/mbtools`, set up with `dotconfig` and
  CLASI.
- Existing code is **ported from**:
  - `mbdeploy` (device layer, pyOCD flash, registry, mDNS, console
    relay);
  - `microbit-radio-relay/server` (relay protocol, normalization, name
    registry, client).
- The relay **firmware** source (`source/relay/`) stays in
  `microbit-radio-relay` — only the server/client side moves.
- The fleet nodes already have the old `mbdeploy` package installed, and
  the golden Pi Zero image and Ansible roles assume it. Migration must
  retire `mbdeploy serve` and `mbrelay.service`, and update the Ansible
  roles, the golden Pi Zero image, and both wikis (see
  [Open decisions §8](#8-package-name-clash)). This is a fleet-wide
  cutover, not a per-node opt-in — treat it with the same care the
  `mbdeploy` project's own CLAUDE.md gives to not disturbing robots in
  use.

## Open decisions

These are the brief's own §9, preserved as **open** — none of them has
been decided by the stakeholder. Where the brief states a recommendation,
it is quoted as a recommendation, not adopted as policy. Sprint planning
and ticketing must not silently resolve any of these; they should be
either explicitly deferred, or escalated to the stakeholder before
architecture that depends on them is locked in.

### 1. Which SQL database?
The stakeholder said MySQL. A MySQL server is a second daemon on every
node, including Pi Zeros, which works against "only one daemon."
**Recommendation (not decided): SQLite** — it is SQL, embedded, needs no
server, and suits a handful of devices. Adopt only if the stakeholder
confirms; do not treat SQLite as settled just because it fits the
one-daemon principle better.

### 2. Where do the files live?
`/run` is cleared at boot, which suits live state and the local socket
(`/run/mbregistry/`). Identity worth keeping across reboots (names,
first-seen, last announcement) belongs in `/var/lib/mbregistry/`.
Windows needs an equivalent under `ProgramData`. (Lower-stakes than the
others, but still not confirmed.)

### 3. How are locks held?
- Local clients: over a Unix socket, recording the peer PID
  (`SO_PEERCRED`). The lock is released when the PID dies *or* the
  connection closes.
- Remote clients: a PID means nothing across hosts, so the lock is tied
  to the network session and released when it drops.

This is the stakeholder's own sketch and the most concrete of the open
items, but it is still presented as a sketch, not a ratified mechanism —
Windows has no `SO_PEERCRED` equivalent out of the box and that gap is
unresolved.

### 4. Local flashing
Does `mbdeploy` run pyOCD itself after taking a lock of kind `flash`, or
does it always flash through the registry's minimal flash op? Going
through the registry gives one code path, and the registry knows a flash
happened. Debug sessions (gdb server) need local SWD access under a lock
either way. No recommendation is stated in the brief; this is a pure
open question.

### 5. Remote serial streams must carry control operations
BREAK and DTR cannot cross a plain TCP pipe, but relays need a reset to
leave the data plane. The registry's stream protocol needs an
out-of-band control channel: a framed protocol, WebSocket control
messages, or RFC 2217 (which pyserial supports). No mechanism is chosen.

### 6. Where does the relay pool live, and what about robot-console?
With one daemon, "give me any free relay" becomes a client-side query
plus a lock. robot-console, however, expects an `_mbrelay._tcp` pool
port and `/names` HTTP (see
[cross-cutting §7](#7-robot-console-compatibility-contract)).
- Options on the table: the registry hosts a compatibility endpoint;
  robot-console migrates to the registry API; or a transition period
  runs both. None is chosen.
- The name registry (robot → channel/group) also needs a home. "A table
  in the registry database, replicated to peers, is the natural fit" —
  stated as a fit, not a decision.

### 7. Platforms
Linux and Windows are required for the daemon. macOS is where the tools
are developed. Whether macOS is a supported daemon platform or a
development convenience only is unresolved.

### 8. Package name clash
The fleet nodes already have the old `mbdeploy` package installed.
Deciding how `mbtools`' `mbdeploy` replaces it — and planning the
migration (retire `mbdeploy serve` and `mbrelay.service`; update the
Ansible roles, the golden Pi Zero image, and both wikis) — is open.

### 9. What gets replicated between peers?
Attach and detach events for certain. Also needed, but not decided:
- live lock and "in use" state, to show busy remotely;
- how a late joiner catches up (snapshot, then event stream — the shape
  is sketched in §3.9 above but the exact protocol isn't);
- removing a peer's devices when that peer disappears (mark unreachable
  vs. delete — not decided).
