# mbtools — stakeholder brief

Source input for CLASI project initiation, written 2026-09-23 from the
stakeholder's direction (Eric Busboom) plus an analysis of the two existing
codebases this project replaces. Sections 1–7 are the stakeholder's
requirements. Section 8 records facts learned from the existing code, and
section 9 lists the decisions that are still open.

## 1. Why this project exists

Two existing tools each find, identify and open every micro:bit on a host,
independently:

- **`mbdeploy`** (`Busboombot/mbdeploy`) flashes firmware and runs
  `mbdeploy serve`, a per-board TCP serial and flash daemon.
- **`mbrelay`** (`microbit-radio-relay/server`) runs a pool of RADIOBRIDGE
  relay boards behind a TCP port, plus a name registry.

Run both on one machine and they compete for the same serial ports. mbrelay
probes every micro:bit, resets robots with DTR and BREAK, and takes a lock
mbdeploy doesn't honour. mbdeploy writes `HELLO` into live relay sessions.
Neither one's flash path coordinates with the other.

The fix is architectural. **Enumeration, identification and exclusive access
belong to exactly one daemon.** Every other tool is a client of it.

`mbtools` is a new repo (`League-Microbit/mbtools`) holding that daemon and
the client tools, as four programs.

## 2. The four programs

| Program | Kind | One-line job |
|---|---|---|
| `mbregistry` | **The only daemon.** Runs as a service and is restarted on failure | Watch USB, identify micro:bits, keep the device database, grant exclusive locks, and be the one network endpoint other hosts talk to |
| `mbdeploy` | Client tool | Flash firmware to a micro:bit by name. Owns the user interface and the "fancy" parts (pyOCD diagnostics, debugging, retries, verification) |
| `mbserial` | Client tool | Give a raw serial connection to a micro:bit, local or remote |
| `mbrelay` | Client tool | Connect to a RADIORELAY/RADIOBRIDGE board and send and receive radio messages, as today's mbrelay does |

The stakeholder's list also called the third tool "mb console"; the name
settled on is **`mbserial`**.

### Guiding principle: one daemon

> "I prefer it if we could avoid having any but one server here, one daemon.
> I really like it if all the other tools were primarily local in that they
> operated on remote things by connecting to a single common remote server,
> which would be MB Registry."

So there is no `mbdeploy serve` and no `mbrelay serve`. A tool acting on a
remote micro:bit connects to the **remote host's `mbregistry`**.

## 3. `mbregistry` — the device registry daemon

### 3.1 Watching USB
- Listen for USB **attach and detach** events on **Linux and Windows**.
- Run as a real service (systemd on Linux, a Windows service on Windows) so
  it is restarted if it dies.

### 3.2 Identifying a device, least intrusive first
1. On attach, decide from USB information alone whether the device is a
   micro:bit, **without opening it**.
2. For a micro:bit, collect everything available without opening it: name,
   serial number (the DAPLink UID), serial port path, and any other
   descriptor data.
3. Create the device's record.
4. Then **briefly open** the serial port and capture its announcement line,
   sending `HELLO` if needed. "You should just be able to open it with a
   reset and then close it."
5. Save the result in the database.

### 3.3 When to re-probe
- A device that has **not been detached and reattached, and not been
  flashed, is never reopened** to re-check its announcement.
- After a **flash**, the announcement may have changed (new firmware, new
  role, new name). The registry must know a flash happened and re-probe the
  device once afterwards.

### 3.4 The database
- The database holds the micro:bits the host has seen. It is updated with
  new attachments and with devices that have been dropped.
- It is kept at **machine level, not per user**, "somewhere in var".
- The stakeholder specified **a SQL database, "a MySQL database"** (see open
  question 1).
- Other programs **consult** it through a service. It "doesn't have to be
  complicated because there are not a lot of devices"; iterating over all
  records is acceptable.

### 3.5 Locking
- The registry provides **exclusive locks**. A client asks it to lock a
  micro:bit for exclusive access.
- **A lock is tied to the holder's PID. If that process dies, the lock is
  released.**
- `mbdeploy`, `mbserial` and `mbrelay` all take a lock before touching a
  device.

### 3.6 Flashing, minimal
- If a remote flash needs something local to the device, the registry
  provides a **very lightweight flash operation**:
  - identify one micro:bit by serial port or by name;
  - hand it a hex file;
  - it flashes.
- "If you don't need to put flashing in MB Registry, don't." The only reason
  to include it is that remote flashing needs a local agent next to the
  device, and under this architecture that agent is `mbregistry`, not an
  `mbdeploy` server.
- Everything beyond the minimal flash stays in `mbdeploy`: pyOCD debugging,
  diagnostics and the user interface.

### 3.7 Networking and peering
- `mbregistry` **advertises itself with mDNS**.
- mDNS is used **only for what genuinely needs the network**, i.e. finding
  registry peers. Device-level announcements do **not** go over mDNS.
- When one registry sees another's mDNS advertisement, they **establish a
  peering relationship and join a ZeroMQ network**.
- The ZeroMQ network carries **attach and detach events**. Every registry
  instance receives them and adds them to its own database. When you look
  for a device, you see **every device on any host reachable over the
  ZeroMQ network**.
- **Explicit peers (nice to have):** running `mbregistry` (or a client such
  as `mbrelay`) with a specified peer attaches to whatever network that peer
  is in. This makes it possible to **cross networks** that mDNS can't span
  today.

## 4. `mbdeploy` — flash by name

1. The user asks to flash a named micro:bit.
2. `mbdeploy` consults the registry to find which device that is, on this
   host or on a peer.
3. It gets a handle on the device and locks it.
4. It flashes, directly for a local board or through that host's registry
   for a remote one.
5. It **makes sure the flash worked**: verification, and the
   post-flash re-probe of the announcement.

`mbdeploy` keeps the fancy work: pyOCD debugging, failure diagnostics and
retries (today's retry on transient probe errors, erase-and-reflash for
locked devices, and blank-board reporting), building firmware, and the user
interface.

## 5. `mbserial` — raw serial access

- Hands the caller a raw serial connection to a named micro:bit.
- **Local or remote.** When the device is not local, access is over TCP,
  possibly HTTP/WebSocket. "Or you can just always get a TCP connection, and
  it'll deal with it whether it's local or non-local."
- Locks the device for the duration of the connection.

## 6. `mbrelay` — relay access

- Lets you connect to a RADIORELAY/RADIOBRIDGE board and **send and receive
  radio messages, just like the current mbrelay does**.
- Uses the registry's services: it asks the registry which attached
  micro:bits are relays, locks one, and runs the relay protocol on top of
  the serial connection.
- It does **not** find, enumerate, probe or list devices itself.

## 7. Repository and process

- The repo is `League-Microbit/mbtools`, at
  `/Users/eric/proj/robot-projects/mbtools`, set up with `dotconfig` and
  CLASI.
- Existing code is **ported from** `mbdeploy` (device layer, pyOCD flash,
  registry, mDNS, console relay) and `microbit-radio-relay/server` (relay
  protocol, normalization, name registry, client). The relay firmware source
  (`source/relay/`) stays in `microbit-radio-relay`.

## 8. Facts from the existing code that constrain the design

**Board identity**
- micro:bit USB ID is VID:PID `0x0D28:0x0204`, and the USB serial number is
  the DAPLink UID.
- The **last 8 hex characters of the UID are identical on every board**
  (`6e052820`, a DAPLink suffix). The per-board field is **`uid[16:24]`**,
  which is mbrelay's `short_uid`. Use it for any short identifier.

**Announcement formats** (both must parse)
- `DEVICE:<role>:<common>:<name>:<serial>`, e.g.
  `DEVICE:RADIOBRIDGE:relay:zavaz:4076631795`.
- `device <role> <common> <name> <serial>`, the robot dialect, e.g.
  `device NEZHA2 robot tovez ...`.
- Relay roles contain `RELAY` or `BRIDGE`.

**Resetting boards**
- On Linux, closing and reopening a port does **not** reset a DAPLink
  target; a **serial BREAK** does. On macOS, reopening does reset it.
- mbdeploy deliberately opens ports with DTR and RTS low so that
  "connecting to a robot must not reboot it". mbrelay opens with the
  defaults, which resets the board.
- The brief's "open it with a reset" at attach time is acceptable. A later
  `mbserial` connect to a robot should still **not** reboot it unless asked.

**Locks and flashing**
- pyserial `exclusive=True` is an advisory `flock` on POSIX. It only stops
  other `flock` users.
- Flashing (pyOCD `flash -t nrf52833 --uid <uid>`) uses SWD over USB, not
  the serial port. Flashing reboots the target under anyone holding the
  tty, which is why a flash must take the device's lock.

**Relay protocol** (reference: `microbit-radio-relay/docs/radio-relay-protocol.md`)
- The command plane is `\n`-terminated lines with `#` replies: `?`,
  `!VER?`, `!C`, `!CG`, `!CGT`, `!N`, `!P`, `!MODE`, `!FRAG`, `!ECHO`,
  `!DEFAULTS`, `HELLO`.
- `!GO` enters the data plane. There is **no in-band escape**; getting back
  to the command plane needs a reset.
- Relays are normalized on acquire and on release (RAW250, frag off, echo
  off, P7, ch0/grp10).
- The name registry maps robot name → (channel, group), stored in
  `names.json` and served over HTTP `GET/PUT/DELETE /names/<n>`.

**robot-console's dependencies on today's mbrelay** (repo
`league-projects/microbit/robot-console`). It needs:
1. An `_mbrelay._tcp` mDNS service whose SRV record points at a raw byte-pipe
   pool port, where each connection gets a freshly reset relay in the
   command plane.
2. TXT `registry=<port>` on the same host.
3. `GET /names/<name>` returning `{channel:int, group:int, source}`.

If any of these disappear, robot-console **silently** falls back to derived
addresses and mistunes robots that have been moved.

## 9. Open decisions

1. **Which SQL database?** The stakeholder said MySQL. A MySQL server is a
   second daemon on every node, including Pi Zeros, which works against
   "only one daemon". **SQLite** is SQL, embedded, needs no server, and
   suits a handful of devices. Recommendation: SQLite unless there is a
   reason for a shared server.
2. **Where do the files live?** `/run` is cleared at boot, which suits live
   state and the local socket (`/run/mbregistry/`). Identity worth keeping
   across reboots (names, first-seen, last announcement) belongs in
   `/var/lib/mbregistry/`. Windows needs an equivalent under `ProgramData`.
3. **How are locks held?**
   - Local clients: over a Unix socket, recording the peer PID
     (`SO_PEERCRED`). The lock is released when the PID dies *or* the
     connection closes.
   - Remote clients: a PID means nothing across hosts, so the lock is tied
     to the network session and released when it drops.
4. **Local flashing.** Does `mbdeploy` run pyOCD itself after taking a lock
   of kind "flash", or does it always flash through the registry's minimal
   flash? Going through the registry gives one code path, and the registry
   knows a flash happened. Debug sessions (gdb server) need local SWD
   access under a lock either way.
5. **Remote serial streams must carry control operations.** BREAK and DTR
   cannot cross a plain TCP pipe, but relays need a reset to leave the data
   plane. The registry's stream protocol needs an out-of-band control
   channel: a framed protocol, WebSocket control messages, or RFC 2217
   (which pyserial supports).
6. **Where does the relay pool live, and what about robot-console?**
   - With one daemon, "give me any free relay" becomes a client-side query
     plus a lock. robot-console, however, expects an `_mbrelay._tcp` pool
     port and `/names` HTTP.
   - Options: the registry hosts a compatibility endpoint; robot-console
     migrates to the registry API; or a transition period.
   - The name registry (robot → channel/group) also needs a home. A table
     in the registry database, replicated to peers, is the natural fit.
7. **Platforms.** Linux and Windows are required. macOS is where the tools
   are developed. Is macOS a supported daemon platform, or a development
   convenience only?
8. **Package name clash.** The fleet nodes already have the old `mbdeploy`
   package installed. Decide how `mbtools`' `mbdeploy` replaces it, and plan
   the migration: retire `mbdeploy serve` and `mbrelay.service`, and update
   the Ansible roles, the golden Pi Zero image and both wikis.
9. **What gets replicated between peers?** Attach and detach events for
   certain. Also needed:
   - live lock and "in use" state, to show busy remotely;
   - how a late joiner catches up (snapshot, then event stream);
   - removing a peer's devices when that peer disappears.
