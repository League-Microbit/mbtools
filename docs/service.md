# Installing and running mbtools as a service

This is the operator reference for installing `mbtools` and running its
daemon, `mbregistry`, on Linux, macOS and Windows. It is written for people
and for AI agents: every path, port, flag and environment variable below was
checked against the source and the programs' own `--help`. Where this page
and the code disagree, the code wins. Run `<program> --help` or
`<program> <subcommand> --help` for the authoritative flag list.

- [1. What runs where](#1-what-runs-where)
- [2. Install](#2-install)
- [3. `mbregistry service run`: defaults](#3-mbregistry-service-run-defaults)
- [4. Ports and mDNS](#4-ports-and-mdns)
- [5. Flags and environment variables](#5-flags-and-environment-variables)
- [6. Linux: systemd service and udev rule](#6-linux-systemd-service-and-udev-rule)
- [7. macOS: launchd](#7-macos-launchd)
- [8. Windows: SCM service (not hardware-verified)](#8-windows-scm-service-not-hardware-verified)
- [9. Peering, `--peer`, and the auth token](#9-peering---peer-and-the-auth-token)
- [10. Firewall](#10-firewall)
- [11. Upgrading](#11-upgrading)
- [12. Logs](#12-logs)
- [13. Operations and troubleshooting](#13-operations-and-troubleshooting)

## 1. What runs where

| Program | Kind | Needs a running `mbregistry`? |
|---|---|---|
| `mbregistry service run` | The daemon. One per host. Watches USB, identifies micro:bits, keeps the device database, grants locks, peers with other hosts. | It *is* the daemon. |
| `mbregistry list` | Client | yes |
| `mbregistry unlock --force` | Client (local socket only) | yes |
| `mbdeploy deploy / list / debug` | Client | yes |
| `mbdeploy build` | Local build helper | no |
| `mbserial` | Client | yes |
| `mbrelay connect / names` | Client | yes |

**One `mbregistry` per host, unless you give each instance its own
identity and ports.** By default, two daemons on one host (say a per-user
one and a system one) fight over the same TCP ports — the second one
fails to bind port 7440. Since sprint 007, a second (or third) instance
*can* coexist on the same host, if every instance that would otherwise
collide is given distinct, non-colliding settings:

- **Ports.** Give each instance its own `--remote-port`/`--peer-pub-port`/
  `--peer-snapshot-port`/`--pool-port`/`--names-port` (explicit values, or
  `0` for an ephemeral port — see section 4), or run extra instances with
  `--no-relay-pool` if only one needs 7444/7445 at all. The local API
  socket/pipe (`--socket`) also needs a distinct path per instance.
- **mDNS/peer identity.** Give each instance its own `--instance NAME` (see
  section 5) so they don't collide in mDNS or overwrite each other in
  peers' `peer`/`device` tables.
- **Which boards each instance may touch.** Every instance still competes
  for the same physical USB boards. A same-host, cross-*process* claim
  (`try_claim` on `<shared-runtime>/mbtools/claims/<uid>.lock`, section 5)
  makes sure only one instance at a time treats a given board uid as its
  own — the other instance simply skips that uid and doesn't list it,
  rather than both instances racing to open the same port. Use
  `--only-uid`/`--exclude-uid` (section 5) to partition boards
  deliberately between instances instead of leaving it to whichever
  instance wins the claim race.

A second instance that still tries to bind a port an existing instance
already holds — two instances both left at the default `--remote-port
7440`, for example — still fails to bind, exactly as before this sprint.
"Exactly one per host" remains the right mental model unless you have a
concrete reason (e.g. a spawned per-session instance, section 5's
`--ready-json`/`--exit-with-parent`/`--no-peering` recipe) to run more
than one.

Local vs. remote I/O. When a client works on a board plugged into **this**
host, the client opens the USB serial or CMSIS-DAP device itself, so the
*client's* user needs USB access (see [6.3](#63-non-root-usb-access-plugdev)).
When the board belongs to a **peer**, the client talks TCP to that peer's
daemon, and the daemon (root) does the I/O. A remote operation needs no local
USB permissions.

## 2. Install

> **Do not `pip install mbtools` from PyPI.** The `mbtools` name on PyPI
> belongs to an unrelated project. Install from GitHub as shown below.

Requirements:

- Python **3.10 or newer**.
- The dependencies (`pyserial`, `intelhex`, `pyocd`, `zeroconf`, `pyzmq`)
  install as wheels on Linux x86_64/aarch64, macOS and Windows. No compiler
  is needed. Hardware-verified on 64-bit Debian 13 (Raspberry Pi, aarch64),
  Ubuntu 24.04 (x86_64) and macOS 15. Windows is **not** hardware-verified.
- `git` on the host if you install with a `git+https://` URL. You can use
  the tarball URL form below instead.

Source URLs (use either one, pinned or unpinned):

```text
git+https://github.com/League-Microbit/mbtools.git              # latest main
git+https://github.com/League-Microbit/mbtools.git@<tag>        # pinned, e.g. @v0.20260924.2
https://github.com/League-Microbit/mbtools/archive/refs/heads/main.tar.gz   # no git needed
```

List the available tags with
`git ls-remote --tags https://github.com/League-Microbit/mbtools.git`.
Versions look like `0.YYYYMMDD.N`.

> The per-user default paths (section 3) and the no-default-route address
> fix (section 13.3) are on `main` after tag `v0.20260924.2`. Install from
> `main`, or from a tag newer than that, to get the behaviour this page
> describes.

### 2.1 For a service host (recommended): a system venv in `/opt/mbtools`

This gives the service a fixed interpreter path that doesn't depend on
anyone's home directory.

With `pip`:

```sh
sudo python3 -m venv /opt/mbtools          # Debian/Ubuntu: sudo apt install python3-venv first
sudo /opt/mbtools/bin/pip install "git+https://github.com/League-Microbit/mbtools.git"
sudo ln -sf /opt/mbtools/bin/mbregistry /opt/mbtools/bin/mbdeploy \
            /opt/mbtools/bin/mbserial   /opt/mbtools/bin/mbrelay /usr/local/bin/
```

With `uv` (`sudo`'s PATH usually lacks `~/.local/bin`, so pass the full path):

```sh
UV="$(command -v uv)"
sudo "$UV" venv --python 3.12 /opt/mbtools    # any Python >= 3.10
sudo "$UV" pip install --python /opt/mbtools/bin/python "git+https://github.com/League-Microbit/mbtools.git"
sudo ln -sf /opt/mbtools/bin/mbregistry /opt/mbtools/bin/mbdeploy \
            /opt/mbtools/bin/mbserial   /opt/mbtools/bin/mbrelay /usr/local/bin/
```

> If `uv venv` downloads its own Python, that interpreter lives in the
> invoking user's uv cache (`root`'s, under `sudo`). This works, but
> `--python /usr/bin/python3` keeps everything under `/opt/mbtools` and the
> system Python.

### 2.2 For one user (workstation, client-only host, or a macOS LaunchAgent)

```sh
uv tool install "git+https://github.com/League-Microbit/mbtools.git"   # puts the 4 commands in ~/.local/bin
# or
python3 -m venv ~/.local/share/mbtools
~/.local/share/mbtools/bin/pip install "git+https://github.com/League-Microbit/mbtools.git"
```

### 2.3 From a checkout (development)

```sh
git clone https://github.com/League-Microbit/mbtools.git && cd mbtools
uv sync            # .venv with mbtools (editable) + dev deps
uv run mbregistry --help
uv run pytest
```

### 2.4 Check the install

```sh
mbregistry --help && mbdeploy --help && mbserial --help && mbrelay --help
mbregistry --version
```

`mbregistry` has a top-level `--version` flag (sprint 008 ticket 006,
works without a subcommand): it prints `mbregistry <version>` — e.g.
`mbregistry 0.20260924.6` — where `<version>` is
`importlib.metadata.version("mbtools")`, and exits 0. This is the same
version string the `--ready-json` line's `version` key reports (section
3's spawn recipe). `mbdeploy`, `mbserial`, and `mbrelay` still have no
`--version` flag of their own; for those, use
`/opt/mbtools/bin/python -c "import importlib.metadata as m; print(m.version('mbtools'))"`
or `pip show mbtools`.

## 3. `mbregistry service run`: defaults

`mbregistry service run` needs no flags. The daemon keeps its database and local
API socket in a location that depends on the platform and on whether it runs
as root (`src/mbtools/registry/paths.py`):

| Platform / user | Database (`devices.db`) | Local API |
|---|---|---|
| Linux, root (the systemd service) | `/var/lib/mbregistry/devices.db` | `/run/mbregistry/api.sock` |
| Linux, normal user | `$XDG_STATE_HOME/mbregistry/devices.db` (default `~/.local/state/mbregistry/devices.db`) | `$XDG_RUNTIME_DIR/mbregistry/api.sock` if `$XDG_RUNTIME_DIR` exists, else `~/.cache/mbregistry/api.sock` |
| macOS, root (LaunchDaemon) | `/Library/Application Support/mbregistry/devices.db` | `/var/run/mbregistry/api.sock` |
| macOS, normal user (LaunchAgent / foreground) | `~/Library/Application Support/mbregistry/devices.db` | `~/Library/Application Support/mbregistry/api.sock` |
| Windows (any account) | `%ProgramData%\mbregistry\devices.db` | named pipe `\\.\pipe\mbregistry` |

- The daemon creates the parent directories. It `chmod`s the Unix socket to
  `0666`, so **any local user can talk to a root daemon**. On a shared
  machine this means any local account can lock and flash any board.
- **Clients find the daemon on their own.** `mbregistry list`, `mbdeploy`,
  `mbserial` and `mbrelay` look for the running user's own daemon socket
  first, then the system (root) one. A normal user on a host running the
  systemd service reaches it with no flags. Root looks the other way round:
  system first, then its own per-user path.
- Precedence everywhere: **flag > environment variable > default**
  (`--socket` / `$MBREGISTRY_SOCKET`, `--db` / `$MBREGISTRY_DB`).
- The `--help` text prints the default for whoever runs `--help`. If you run
  `mbregistry service run --help` as a normal user, it shows the per-user path.
- USB poll interval: `--interval`, default `2.0` seconds.
- On start the daemon prints one line to stderr naming everything it bound,
  e.g.
  `mbregistry: listening on /run/mbregistry/api.sock (local api), 7440 (remote api), store at /var/lib/mbregistry/devices.db, peering active (pub 7442, snapshot 7443), relay pool on 7444, names API on 7445`.
- `SIGTERM` / `SIGINT` shut it down cleanly and it exits 0.

## 4. Ports and mDNS

All listeners bind **all interfaces** (`0.0.0.0` / `tcp://*`). All ports are
unprivileged. None of these listeners can be turned off, except the relay
pool (`--no-relay-pool`).

| Port | Proto | Purpose | Override |
|---|---|---|---|
| (local) | Unix socket / named pipe | Local query/control API (section 3) | `--socket`, `$MBREGISTRY_SOCKET` |
| **7440** | TCP | Remote API: how peers and remote `mbdeploy`/`mbserial`/`mbrelay` reach this host's devices | `--remote-port`, `$MBREGISTRY_REMOTE_PORT` |
| **7442** | TCP | Peering ZeroMQ PUB (event bus) | `--peer-pub-port`, `$MBREGISTRY_PEER_PUB_PORT` |
| **7443** | TCP | Peering ZeroMQ snapshot REQ/REP | `--peer-snapshot-port`, `$MBREGISTRY_PEER_SNAPSHOT_PORT` |
| **7444** | TCP | Robot-console-compatible relay pool (`_mbrelay._tcp`) | `--pool-port`, `$MBREGISTRY_POOL_PORT`; `--no-relay-pool` disables it |
| **7445** | HTTP | Robot-console-compatible `/names/<name>` API | `--names-port`, `$MBREGISTRY_NAMES_PORT`; always on |
| 5353 | UDP | mDNS (multicast) | n/a |

`--pool-port 0`/`--names-port 0` bind an ephemeral port; the `_mbrelay._tcp`
SRV port and its `registry=` TXT value always reflect the port actually
bound, never the requested value (this also already held for 7440/7442/7443
via `--remote-port`/etc.).

mDNS service types:

| Service | Advertised by | Instance name | Meaning |
|---|---|---|---|
| `_mbregistry._tcp` | every `mbregistry` | `--instance`/`$MBREGISTRY_INSTANCE`, else host name | Peer discovery. TXT carries this host's remote/peering ports. |
| `_mbrelay._tcp` | the relay pool (unless `--no-relay-pool`) | `--instance`/`$MBREGISTRY_INSTANCE`, else host name | Legacy relay discovery for robot-console. SRV port = the bound pool port (default 7444), TXT `registry=<bound names-API port>` (default 7445). |

mDNS is built in (python-zeroconf). You don't need Avahi or Bonjour, and it
coexists with both. To look from the command line:

```sh
avahi-browse -rt _mbregistry._tcp        # Linux (package avahi-utils)
avahi-browse -rt _mbrelay._tcp
dns-sd -B _mbregistry._tcp               # macOS; then: dns-sd -L <instance> _mbregistry._tcp
```

**Which address is advertised.** The daemon advertises the IPv4 address the
kernel would use for its default route. It learns this by connecting a UDP
socket, which sends no packets. A host with **no default route** advertises
its first real interface address instead. That skips loopback, link-local,
and interfaces whose names start with `docker`, `br-`, `veth`, `virbr`,
`vmnet`, `bridge`, `utun`, `tun` or `tap`. It never advertises the
`127.0.1.1` that Debian maps the hostname to in `/etc/hosts`.

## 5. Flags and environment variables

### `mbregistry` (top-level)

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--version` | (none) | n/a | Print `mbregistry <version>` to stdout and exit 0; works without a subcommand (sprint 008 ticket 006) |

### `mbregistry service run`

| Flag | Env var | Default | Notes |
|---|---|---|---|
| `--socket PATH` | `MBREGISTRY_SOCKET` | section 3 | Local API socket (on Windows: a pipe name) |
| `--db PATH` | `MBREGISTRY_DB` | section 3 | SQLite device and name database |
| `--interval SEC` | (none) | `2.0` | USB poll interval |
| `--peer HOST[:PORT]` | (none) | (none) | Explicit peer, repeatable; PORT defaults to 7440 (section 9) |
| `--remote-port N` | `MBREGISTRY_REMOTE_PORT` | `7440` | |
| `--peer-pub-port N` | `MBREGISTRY_PEER_PUB_PORT` | `7442` | |
| `--peer-snapshot-port N` | `MBREGISTRY_PEER_SNAPSHOT_PORT` | `7443` | |
| `--auth-token SECRET` | `MBREGISTRY_TOKEN` | unset (no auth) | See the limitation in section 9 |
| `--pool-port N` | `MBREGISTRY_POOL_PORT` | `7444` | Relay pool (`_mbrelay._tcp`) TCP port; `0` = ephemeral |
| `--names-port N` | `MBREGISTRY_NAMES_PORT` | `7445` | `/names` HTTP port; `0` = ephemeral |
| `--instance NAME` | `MBREGISTRY_INSTANCE` | short hostname | mDNS/peer identity: the `_mbregistry._tcp`/`_mbrelay._tcp` instance name, and the value recorded as `peer.host`/`device.host` on peers |
| `--pipe NAME` | (none) | `registry.paths.default_pipe_name()` | Windows named-pipe transport name; ignored on non-Windows |
| `--only-uid UID` | (none) | no restriction | Only ever claim/probe this uid (repeatable) |
| `--exclude-uid UID` | (none) | (none) | Never claim/probe this uid (repeatable) |
| `--ready-json` | (none) | off | Print one JSON ready-line to stdout once every listener is bound (see the spawn recipe below) |
| `--exit-with-parent` | (none) | off | Exit cleanly (same path as `SIGTERM`) when stdin reaches EOF |
| `--no-peering` | (none) | peering on | Skip mDNS advertise/browse and the ZeroMQ peering bus entirely |
| `--no-relay-pool` | (none) | pool on | Set on hosts with no relay boards |
| `--windows-service` | (none) | off | Only for the Windows SCM `binPath=`; not for interactive use |

### Cross-instance board claim

Before any instance treats a USB-attached board as its own (lists it,
probes it, opens its port), it takes a non-blocking, same-host claim on
that board's uid — `flock` on
`<tempfile.gettempdir()>/mbtools/claims/<uid>.lock` (a world-writable,
sticky (`0o1777`) directory shared by every `mbregistry` on the host
regardless of `--user`/`--system` scope), plus best-effort `TIOCEXCL` on
the opened serial fd, on Unix; a no-op on Windows, where COM-port
exclusivity is already OS-native. An instance that loses the race simply
skips that uid for the current poll cycle and never lists it — it is
retried on a later cycle, not treated as an error. The claim is released
on detach, or automatically by the OS when the holding process exits
(a crashed instance leaves nothing behind). This is a same-host,
same-process-lifetime mechanism, entirely separate from the per-connection
`lock`/`unlock` API (section 9's peering/`LockManager` machinery) — a
board can be claimed by this instance and still be lock-free, or claimed
and locked, exactly as with a single instance. `--only-uid`/`--exclude-uid`
above partition which uids an instance will even attempt to claim, for
deliberate multi-instance setups (e.g. two instances on one test bench
each serving half the attached boards).

### Spawn recipe: `--ready-json` / `--exit-with-parent` / `--no-peering`

A parent process (robot-console, a test harness, …) that wants to spawn
and supervise a short-lived `mbregistry service run` child — without joining it to
the mDNS/peering fleet — combines three flags:

```sh
mbregistry service run --socket /tmp/mbregistry-session/api.sock \
  --db /tmp/mbregistry-session/devices.db \
  --instance session-1234 \
  --ready-json --exit-with-parent --no-peering
```

- `--ready-json` prints exactly one JSON line to **stdout**, once every
  requested listener is bound (every other diagnostic line this command
  prints goes to stderr, so a parent can read just stdout):

  ```json
  {"ready": true, "instance": "session-1234", "version": "0.20260924.6",
   "socket": "/tmp/mbregistry-session/api.sock",
   "ports": {"remote": 7440, "pool": 7444, "names": 7445}}
  ```

  (`peer_pub`/`peer_snapshot` would also appear here, alongside `remote`,
  if `--no-peering` were left off.) `ports` only carries the ports
  actually applicable: `peer_pub`/`peer_snapshot` are omitted under
  `--no-peering` (as in the example above), and `pool` is omitted under
  `--no-relay-pool`. Every value is the port actually bound (not merely
  requested) **except** `peer_pub`/`peer_snapshot`, which report the
  resolved requested value — `registry.peering.PeerDiscovery` (unchanged
  this sprint) exposes no bound-port equivalent to read back from.
  `version` (sprint 008 ticket 006) is `importlib.metadata.version
  ("mbtools")` — the same string `mbregistry --version` prints, below —
  so a spawning parent (robot-console's own minimum-version check) can
  read it from either surface.
- `--exit-with-parent` watches stdin for EOF (the parent closing its end of
  an inherited pipe, or dying outright) and then shuts down cleanly through
  the same path `SIGTERM` already takes.
- `--no-peering` skips constructing the mDNS/ZeroMQ peering component
  entirely — not "start then immediately stop." The relay pool and
  `/names` listener are unaffected; disable those separately with
  `--no-relay-pool` if the spawned instance shouldn't advertise
  `_mbrelay._tcp` either. A `--peer` given alongside `--no-peering` is a
  no-op, noted to stderr only.

### Clients

| Variable / flag | Used by | Meaning |
|---|---|---|
| `--socket`, `MBREGISTRY_SOCKET` | all clients | Talk to the daemon at this socket instead of auto-discovery |
| `GITHUB_TOKEN` | `mbdeploy deploy --repo` | GitHub API token for release lookups (rate limits, private repos) |

`mbdeploy deploy --repo` caches downloaded hex files in
`~/.cache/mbtools/hex/<owner>/<repo>/<tag>/`.

### `mbregistry unlock --force` (sprint 008)

```text
mbregistry unlock UID|NAME --force [--socket PATH]
```

A manual, operator-only override for a stale lock: drops the device's
lock regardless of who holds it, and closes the holder's own connection
so it observes EOF rather than silently losing exclusivity. `--force` is
required — there is no non-forcing `unlock` subcommand to fall back to
by omitting it. Local Unix socket / named pipe only: there is no
equivalent on the remote TCP port, and no automatic pre-emption — this
is always a deliberate action an operator takes. A device with no active
lock reports `not locked` and exits `0`, not an error.

```text
$ mbregistry unlock 9d2f... --force
mbregistry: 9d2f...: released flash lock (alice-laptop, 12m)
$ mbregistry unlock 9d2f... --force
mbregistry: 9d2f...: not locked
```

### `mbregistry service install` / `uninstall` / `start` / `stop` / `restart` / `status`

```text
mbregistry service install   (--user | --system) [--dry-run]
mbregistry service uninstall (--user | --system) [--purge] [--dry-run]
mbregistry service start     [--user | --system] [--dry-run]
mbregistry service stop      [--user | --system] [--dry-run]
mbregistry service restart   [--user | --system] [--dry-run]
mbregistry service status
```

`start`/`stop`/`restart` act on an already-installed service and never
write or remove files. Without a scope flag they act on whichever scope is
installed (it's an error if neither or both are). On macOS, `stop` is
`launchctl bootout` (the plist stays, so it loads again at next login/boot),
and `start`/`restart` are `launchctl bootstrap` + `kickstart` (`-k` for
restart). On Linux they are `systemctl [--user] start|stop|restart
mbregistry.service`.

`mbregistry service run` is the foreground daemon the service runs.
The old top-level `mbregistry run` still works as a hidden, deprecated
alias, so existing unit files and plists that invoke it don't need a
reinstall right away.

`--user`/`--system` is required and mutually exclusive on `install`/`uninstall` —
there is no default scope.

| Subcommand | Flag | Meaning |
|---|---|---|
| `install` | `--user` \| `--system` | Required, mutually exclusive. `--system` needs root/sudo. |
| `install` | `--dry-run` | Print what would be written and run; touch nothing. |
| `uninstall` | `--user` \| `--system` | Required, mutually exclusive. |
| `uninstall` | `--purge` | Also remove `devices.db` (kept by default). |
| `uninstall` | `--dry-run` | Print what would be stopped and removed; touch nothing. |
| `status` | (none) | Reports both scopes' installed/running state and paths. |

Not supported on Windows: every `service` subcommand exits nonzero with
`mbregistry: service ... is not supported on Windows` (section 8).

### `mbregistry install-service` (deprecated)

Hidden from `--help`. Kept for one release as an alias for
`mbregistry service install --system` — see section 11 ("Upgrading") for the
removal window.

| Flag | Default |
|---|---|
| `--user NAME` | `$SUDO_USER`, then `$USER`, then the current user. Only affects the printed `usermod` line. |

## 6. Linux: systemd service and udev rule

### 6.1 Install the service

```sh
sudo /opt/mbtools/bin/mbregistry service install --system
```

This **writes the unit and udev rule, and loads and starts the service** —
unlike the old `install-service`, nothing is left to run by hand:

```sh
systemctl daemon-reload
systemctl enable --now mbregistry.service
udevadm control --reload-rules
udevadm trigger
usermod -aG plugdev <you>     # then log in again (new SSH session)
```

`--dry-run` prints exactly these steps without writing or running anything —
useful to preview before running as root.

It is idempotent. Re-running rewrites both files with identical content and
reloads/re-enables the service — no duplicate rule file, no second service
instance. Without root it fails and exits 1.

Run it with the venv you want the service to use (`/opt/mbtools/bin/...`).
The unit's `ExecStart=` is the Python interpreter of whoever ran
`service install`.

**Per-user, no root (starts at login instead of boot):**

```sh
mbregistry service install --user
```

Needs the operator already in `plugdev`, and the system-scope udev rule
already written — both are one-time root-run steps (see 6.3). Without them,
`install --user` refuses and prints the exact `sudo` commands to run first,
rather than installing a daemon that can't open the boards (a lingering user
service has no logind seat, so `uaccess` alone grants nothing).

### 6.2 The unit it writes (`/etc/systemd/system/mbregistry.service`)

Rendered by `render_systemd_unit()`, with `/opt/mbtools/bin/python3` as the
interpreter that ran it:

```ini
[Unit]
Description=mbregistry -- micro:bit device registry daemon
After=network.target

[Service]
Type=simple
ExecStart=/opt/mbtools/bin/python3 -m mbtools.registry.cli run
Restart=on-failure
RestartSec=2
RuntimeDirectory=mbregistry
StateDirectory=mbregistry

[Install]
WantedBy=multi-user.target
```

- It runs as root, so the paths are `/var/lib/mbregistry/devices.db` and
  `/run/mbregistry/api.sock`. `StateDirectory=` and `RuntimeDirectory=`
  create exactly those directories.
- **To add flags or env vars, don't edit the unit.** `service install`
  overwrites it on every run. Use a drop-in (`sudo systemctl edit mbregistry.service`):

  ```ini
  [Service]
  Environment=MBREGISTRY_TOKEN=change-me
  # Flags without an env var (--peer, --no-relay-pool, --interval) need ExecStart replaced:
  ExecStart=
  ExecStart=/opt/mbtools/bin/python3 -m mbtools.registry.cli run --no-relay-pool --peer other-host
  ```

  Then run `sudo systemctl restart mbregistry`.

### 6.3 Non-root USB access (`plugdev`)

`/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules`, as written by
`render_udev_rule()`:

```udev
# mbregistry -- non-root access to the micro:bit DAPLink interface
# (VID:PID 0d28:0204). Written by `mbregistry service install`
# (ticket 006-003) -- re-running it overwrites this file with identical
# content, so re-running install is idempotent. See
# mbtools.registry.service.render_udev_rule()'s docstring for why each rule
# below grants access via both the plugdev group and uaccess rather than
# just one of the two.

# CDC-ACM tty device node (mbserial, and pyOCD's DAPLink serial transport)
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{idVendor}=="0d28", ATTRS{idProduct}=="0204", GROUP="plugdev", MODE="0660", TAG+="uaccess"

# Raw USB device node (CMSIS-DAP v2 / WinUSB transport, opened directly by pyOCD)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0d28", ATTRS{idProduct}=="0204", GROUP="plugdev", MODE="0660", TAG+="uaccess"

# hidraw device node (CMSIS-DAP v1 / HID transport)
KERNEL=="hidraw*", ATTRS{idVendor}=="0d28", ATTRS{idProduct}=="0204", GROUP="plugdev", MODE="0660", TAG+="uaccess"
```

- The **daemon** runs as root and doesn't need this. It is for **local
  clients** run by a normal user: `mbserial`, `mbdeploy deploy`/`debug` on
  a board plugged into this host, and `mbrelay` on a local relay.
- `TAG+="uaccess"` works only for a user logged in at a local seat.
  Headless / SSH hosts rely on the `plugdev` group, and a new group
  membership takes effect only in a **new login session**.
- Debian, Ubuntu and Raspberry Pi OS ship a `plugdev` group. Elsewhere,
  create it first with `sudo groupadd plugdev`.
- Until this is in place, prefix local client commands with `sudo`.

### 6.4 Everyday commands

```sh
systemctl status mbregistry
sudo systemctl restart mbregistry
sudo systemctl disable --now mbregistry          # stop and don't start at boot
journalctl -u mbregistry -f                      # follow the log
mbregistry list                                  # works as a normal user
```

To uninstall, see 6.5.

### 6.5 Uninstall

```sh
sudo mbregistry service uninstall --system     # stop, unload, remove the unit + udev rule
mbregistry service uninstall --user             # per-user scope
```

Stops and disables the service, removes the unit file (and, for `--system`,
the udev rule too, then reruns `daemon-reload`/`udevadm control
--reload-rules`), and removes the socket file. **Keeps `devices.db`** unless
`--purge` is given, in which case the whole state directory is removed too.
Never removes the operator from `plugdev`.

Safe to run even when nothing is installed at that scope — it says so and
exits 0 (safe to call repeatedly, e.g. from Ansible), naming the other scope
if *it* has an install. `--dry-run` prints what would be stopped and removed
without touching anything.

To remove the code itself afterward: `sudo rm -rf /opt/mbtools`.

## 7. macOS: launchd

`mbregistry service install` supports both scopes on macOS —
`--system` (a LaunchDaemon) and `--user` (a LaunchAgent). No USB drivers or
udev equivalent are needed on macOS. A normal user can open the micro:bit's
serial and CMSIS-DAP interfaces.

| | LaunchDaemon (root) | LaunchAgent (one user) |
|---|---|---|
| Starts | at boot, no login needed | when that user logs in (GUI session) |
| Plist | `/Library/LaunchDaemons/org.jointheleague.mbregistry.plist` | `~/Library/LaunchAgents/org.jointheleague.mbregistry.plist` |
| Database | `/Library/Application Support/mbregistry/devices.db` | `~/Library/Application Support/mbregistry/devices.db` |
| Socket | `/var/run/mbregistry/api.sock` | `~/Library/Application Support/mbregistry/api.sock` |
| Log | `/Library/Logs/mbregistry.log` | `~/Library/Logs/mbregistry.log` |
| launchctl domain | `system` | `gui/$(id -u)` |
| Use it for | an unattended board host | a workstation |

Pick **one**. Both plists use the same label. Each plist runs the venv's
interpreter directly (`python -m mbtools.registry.cli service run`). This is the
same entry point the systemd unit uses, and it keeps pyOCD on the same
interpreter.

`KeepAlive` → `SuccessfulExit=false` restarts the daemon after a crash
(non-zero exit) but not after a clean stop, which matches systemd's
`Restart=on-failure`. `ThrottleInterval` spaces restarts 10 s apart.

### 7.1 LaunchDaemon (root, starts at boot)

Install into `/opt/mbtools` (section 2.1). Then:

```sh
sudo mbregistry service install --system
```

This writes the plist below and runs the three `launchctl` steps
(`bootout` — tolerating "wasn't loaded" on a fresh install — then `enable`,
then `bootstrap system`), so the service is running immediately afterward.
`--dry-run` prints the plist and the three commands without touching
anything.

Rendered plist (`/Library/LaunchDaemons/org.jointheleague.mbregistry.plist`),
shown here for reference — generated by `render_launchd_plist()`, not
hand-typed:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>EnvironmentVariables</key>
	<dict>
		<key>PYTHONUNBUFFERED</key>
		<string>1</string>
	</dict>
	<key>KeepAlive</key>
	<dict>
		<key>SuccessfulExit</key>
		<false/>
	</dict>
	<key>Label</key>
	<string>org.jointheleague.mbregistry</string>
	<key>ProgramArguments</key>
	<array>
		<string>/opt/mbtools/bin/python</string>
		<string>-m</string>
		<string>mbtools.registry.cli</string>
		<string>run</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>/Library/Logs/mbregistry.log</string>
	<key>StandardOutPath</key>
	<string>/Library/Logs/mbregistry.log</string>
	<key>ThrottleInterval</key>
	<integer>10</integer>
	<key>WorkingDirectory</key>
	<string>/</string>
</dict>
</plist>
```

`ProgramArguments[0]` is the Python interpreter that ran `service install`
(here, `/opt/mbtools/bin/python`). `service install` regenerates and
overwrites this file on every run — there is no systemd-style drop-in for
launchd. To add flags (`--peer`, `--no-relay-pool`, …) or env vars
(`MBREGISTRY_TOKEN`), hand-edit `ProgramArguments`/`EnvironmentVariables`
after installing, then reload with `sudo launchctl bootout
system/org.jointheleague.mbregistry && sudo launchctl bootstrap system
/Library/LaunchDaemons/org.jointheleague.mbregistry.plist` — and avoid
re-running `service install` afterward, which would overwrite your edits.

Manage it:

```sh
sudo launchctl print system/org.jointheleague.mbregistry | head -30   # state, pid, last exit code
sudo launchctl kickstart -k system/org.jointheleague.mbregistry      # restart
sudo launchctl bootout system/org.jointheleague.mbregistry           # stop and unload
sudo launchctl disable system/org.jointheleague.mbregistry           # don't load at boot
tail -f /Library/Logs/mbregistry.log
```

### 7.2 LaunchAgent (one user, starts at login)

Install into `~/.local/share/mbtools` (section 2.2), or `uv tool install`,
first. Then, as yourself (no `sudo`):

```sh
mbregistry service install --user
```

Writes `~/Library/LaunchAgents/org.jointheleague.mbregistry.plist` — the
same shape as 7.1's plist, but with `ProgramArguments[0]` set to whichever
interpreter ran `service install` (find it with `head -1 "$(command -v
mbregistry)"` if you used `uv tool install`), `WorkingDirectory` set to
`$HOME`, and the log at `~/Library/Logs/mbregistry.log` — and runs
`launchctl bootout`/`enable`/`bootstrap gui/$(id -u)` instead of the
system-scope `bootstrap system`. `--dry-run` previews without touching
anything.

Manage it with the same verbs as 7.1, using `gui/$(id -u)` in place of
`system`, no `sudo`, and the log at `~/Library/Logs/mbregistry.log`.

### 7.3 macOS notes

- **Local Network privacy (macOS 15+) — use the LaunchDaemon on a peering
  host.** A LaunchAgent's Python has no Local Network permission and never
  gets a prompt for one, so macOS silently blocks its outbound LAN traffic
  and its mDNS: connects fail with `No route to host` (errno 65), and it
  never discovers a peer. Peers that connect *in* still work, so it looks
  half-alive. Seen on `gala`: every peer went `peer unreachable` and stayed
  that way across restarts, while the same command run from Terminal
  peered fine. A bare venv `python3` usually doesn't appear under *System
  Settings → Privacy & Security → Local Network* to allow it. A root
  LaunchDaemon (`--system`) is not subject to Local Network privacy.
- **The LaunchDaemon's interpreter must be on the boot disk.** The plist
  runs whichever Python ran `service install`. If that is a dev venv on
  an external volume, or its interpreter is a symlink onto one (uv's
  managed Pythons under `~/.local/share/uv` when `~/.local` is itself on
  another volume), root's launchd fails at load time with `dyld: Library
  not loaded: @executable_path/../lib/libpython3.13.dylib` in
  `/Library/Logs/mbregistry.log`. The volume may not even be mounted at
  boot. Give the daemon its own non-editable install on the boot disk:

  ```sh
  sudo mkdir -p /opt/mbtools && sudo chown "$USER":staff /opt/mbtools
  uv venv /opt/mbtools/venv --python /opt/homebrew/bin/python3
  uv pip install --python /opt/mbtools/venv/bin/python /path/to/mbtools
  sudo /opt/mbtools/venv/bin/mbregistry service install --system
  ```

  To update it, rerun the `uv pip install` line, then run
  `sudo mbregistry service restart`. Client commands from any other
  install still find the root daemon's socket (`/var/run/mbregistry/api.sock`).
- **Application Firewall.** If it is on, allow incoming connections for
  the venv's Python (section 10).
- To run in the foreground for debugging, stop the service first. Only one
  daemon may run per host. Then run `mbregistry service run` (as yourself) or
  `sudo mbregistry service run` (to use the system paths).

### 7.4 Uninstall

```sh
sudo mbregistry service uninstall --system     # stop, unload, remove the plist
mbregistry service uninstall --user             # per-user scope, no sudo
```

Same behavior as Linux's `service uninstall` (6.5): stops and unloads the
service, removes the plist and log file, keeps `devices.db` unless
`--purge` is given, and is a safe, exit-0 no-op when nothing is installed
at that scope. `--dry-run` previews without touching anything.

## 8. Windows: SCM service (not hardware-verified)

> **Not verified on real hardware.** The Windows code paths (named-pipe API,
> SCM integration, `%ProgramData%` paths) are covered only by unit tests
> with fakes. Nobody has run them against a real board on Windows yet.

> **`mbregistry service install`/`uninstall`/`status` are not supported on
> Windows.** Every one exits nonzero with `mbregistry: service ... is not
> supported on Windows` and never touches this section's SCM code at all.
> `install-service` (below) remains the only Windows install path — it is
> deprecated elsewhere (section 5, section 11) but unaffected on Windows,
> per the stakeholder's explicit "leave Windows code alone" decision for
> sprint 006.

Install into a venv, for example `C:\mbtools`:

```powershell
py -3.12 -m venv C:\mbtools
C:\mbtools\Scripts\pip install "git+https://github.com/League-Microbit/mbtools.git"
C:\mbtools\Scripts\mbregistry install-service
```

On Windows, `install-service` writes no files. It **prints** the commands to
run in an **Administrator** shell:

```bat
sc.exe create mbregistry binPath= "C:\mbtools\Scripts\python.exe -m mbtools.registry.cli run --windows-service" start= auto DisplayName= "mbtools micro:bit registry daemon"
sc.exe failure mbregistry actions= restart/60000/restart/60000/restart/60000 reset= 86400
sc.exe start mbregistry
```

(`binPath=` is whatever interpreter ran `install-service`. `sc.exe`
requires the space after each `=`.)

- `--windows-service` makes the process talk to the Service Control
  Manager. Without it, the SCM kills the process with error 1053. Don't use
  the flag interactively.
- The service runs as LocalSystem. Database:
  `%ProgramData%\mbregistry\devices.db`. Local API: `\\.\pipe\mbregistry`.
- Manage it with `sc.exe query mbregistry`, `sc.exe stop mbregistry` and
  `sc.exe delete mbregistry`.
- Logs: a service's stdout/stderr go nowhere. To see output, stop the
  service and run `C:\mbtools\Scripts\mbregistry service run` in a console.
- Firewall: see section 10.

## 9. Peering, `--peer`, and the auth token

- **Automatic.** Every daemon advertises and browses `_mbregistry._tcp` over
  mDNS. It connects to each peer's ZeroMQ PUB (7442) and snapshot (7443)
  ports and merges the peer's devices into its own list (HOST column =
  peer's host name). No configuration is needed on a flat LAN where
  multicast works.
- **Explicit peers** for networks mDNS can't cross (VLANs, VPNs, Wi-Fi
  with client isolation):
  `mbregistry service run --peer host-a --peer host-b.example.org:7440`. `PORT` is the
  peer's **remote API** port and defaults to 7440. An explicit peer is
  assumed to use **the same** 7442/7443 as this host. If a peer runs
  non-default peering ports, only mDNS (which reads them from TXT) can find
  it. Keep the whole fleet on the same ports.
- **Peering is symmetric in effect.** Each host must be able to reach the
  other's 7440/7442/7443.
- **Remote operations.** After peering, `mbdeploy deploy <name>` and
  `mbserial <name>` automatically go to the owning host's port 7440.
  `mbrelay connect <robot>@<host>` pins a session to a relay on a given
  peer.
- **A vanished peer** is marked unreachable, never deleted. Its boards
  show `peer unreachable` in `mbregistry list` until it comes back. When it
  reconnects, its snapshot is fetched again.

### Auth token

`--auth-token SECRET` (or `MBREGISTRY_TOKEN`) makes the daemon require the
secret on its **remote API (7440)** and **peering snapshot (7443)**
connections. It is **unset by default**. When it is set:

- Every daemon in the fleet must use the same token, or peers silently fail
  to sync.
- The ZeroMQ PUB stream (7442), the relay pool (7444) and the `/names` API
  (7445) are **never** authenticated.
- **Known gap:** the client programs (`mbdeploy`, `mbserial`, `mbrelay`)
  have no way to send a token. With a token set, **remote** deploy/serial/
  relay operations against that host fail with an `unauthorized` error.
  Local operations and `mbregistry list` are unaffected. Leave the token
  unset unless you only need peer-to-peer sync.

Treat the LAN as the trust boundary. Anyone who can reach port 7440 on a
host with no token can lock, flash and open serial on its boards.

## 10. Firewall

For peering and remote access, open these **inbound** on each board host:
TCP **7440, 7442, 7443**; TCP **7444, 7445** if robot-console uses this
host's relays or names API; UDP **5353** (mDNS multicast). A client-only
host (no boards, not peering) needs no inbound ports.

```sh
# Ubuntu / Debian with ufw
sudo ufw allow 7440/tcp && sudo ufw allow 7442:7445/tcp && sudo ufw allow 5353/udp

# firewalld (Fedora/RHEL)
sudo firewall-cmd --permanent --add-port=7440/tcp --add-port=7442-7445/tcp --add-service=mdns
sudo firewall-cmd --reload

# macOS Application Firewall (only if enabled): allow the interpreter that runs the daemon
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/mbtools/bin/python
sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp /opt/mbtools/bin/python
```

The macOS firewall works per binary. A venv's `bin/python` is a symlink, so
if the rule doesn't take, add the real binary as well: `realpath
/opt/mbtools/bin/python`.

```powershell
# Windows (Administrator PowerShell)
New-NetFirewallRule -DisplayName "mbregistry TCP" -Direction Inbound -Protocol TCP -LocalPort 7440,7442,7443,7444,7445 -Action Allow
New-NetFirewallRule -DisplayName "mbregistry mDNS" -Direction Inbound -Protocol UDP -LocalPort 5353 -Action Allow
```

## 11. Upgrading

Upgrades keep the database. The schema migrates itself on start.

**Upgrade note (sprint 007):** `mbregistry list`'s `no-firmware` `STATE`
now means only "confirmed blank after a flash-triggered re-probe" — a
board that simply never announced (no flash involved) now shows a
separate `no-answer` state instead of also being labeled `no-firmware`.
An existing `devices.db` row already sitting at `no-firmware` from before
this change relabels itself on its next real probe/attach event; there is
no backfill on upgrade.

```sh
# Linux, /opt venv
sudo /opt/mbtools/bin/pip install --upgrade "git+https://github.com/League-Microbit/mbtools.git"   # or @<tag>
sudo /opt/mbtools/bin/mbregistry service install --system     # idempotent; rewrites and restarts
mbregistry list

# macOS LaunchDaemon
sudo /opt/mbtools/bin/pip install --upgrade "git+https://github.com/League-Microbit/mbtools.git"
sudo /opt/mbtools/bin/mbregistry service install --system     # idempotent; rewrites and restarts

# uv tool (per user)
uv tool install --reinstall "git+https://github.com/League-Microbit/mbtools.git"
mbregistry service install --user
```

If you **recreate** the venv instead of upgrading it (e.g. `uv venv
--clear`), stop the service first (`service uninstall`, or the platform's
own stop verb). The running root daemon leaves root-owned `__pycache__`
files that a non-root rebuild can't delete. Upgrade one host at a time and
check `mbregistry list` on it and on one peer before moving on.

**A host with a previous `install-service`-written systemd unit/udev rule**
is upgraded in place the same way: re-running `service install --system`
overwrites both files with equivalent (system-scope) content and, this
time, also enables and starts the service — an explicit behavior change
from "write only" to "write and start."

**`install-service` is deprecated** (section 5) — a hidden alias for
`service install --system`, kept for exactly one release so a script that
still invokes it does not suddenly start and enable a real service it
never asked to run. It prints a deprecation notice to stderr on every
invocation and will be removed in the release after this one. Migrate any
script or Ansible task that calls `install-service` to `service install
--system` before then.

## 12. Logs

The daemon logs to **stderr**: the start-up line (section 3), plus
warnings and errors from peering, the store and the remote API. It logs no
INFO messages, so a quiet log is normal.

| Platform | Where |
|---|---|
| Linux (systemd) | `journalctl -u mbregistry` (`-f` to follow, `-b` since boot, `--since "10 min ago"`) |
| macOS LaunchDaemon | `/Library/Logs/mbregistry.log` |
| macOS LaunchAgent | `~/Library/Logs/mbregistry.log` |
| Windows service | not captured; run `mbregistry service run` in a console to see output |
| Foreground | the terminal |

The launchd log files grow without limit. Truncate them occasionally
(`sudo truncate -s 0 /Library/Logs/mbregistry.log`) or add a
`newsyslog.d` entry.

## 13. Operations and troubleshooting

Client exit codes are the same across all four programs. Use them in
scripts instead of parsing text:

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | generic failure |
| 2 | bad command line |
| 3 | **no daemon**: local API socket/pipe missing or unreachable |
| 4 | no such device |
| 5 | device locked by someone else |
| 6 | a flash ran and failed |
| 7 | `mbregistry service install --user` (Linux) refused: the operator isn't in `plugdev` and/or the system-scope udev rule doesn't exist yet — see the printed `sudo` commands |
| 130 | `mbdeploy` interrupted by Ctrl-C |

A quick health check for agents, run on the host:

```sh
systemctl is-active mbregistry            # Linux; macOS: sudo launchctl print system/org.jointheleague.mbregistry | grep state
mbregistry list --json >/dev/null; echo "exit=$?"     # 0 healthy, 3 no daemon
```

### 13.1 Daemon not reachable (exit 3, "is the daemon running?")

1. Is it running? Check `systemctl status mbregistry`, `launchctl print ...`
   or `sc.exe query mbregistry`. If it keeps restarting, read the log
   (section 12). The last traceback says why.
2. Is the client looking in the right place? The error names the socket
   path it tried. Clients try **your own** per-user socket, then the
   system one. Check both:
   `ls -l /run/mbregistry/api.sock "${XDG_RUNTIME_DIR:-/nonexistent}/mbregistry/api.sock" ~/.cache/mbregistry/api.sock`
   (macOS: `/var/run/mbregistry/api.sock`,
   `~/Library/Application Support/mbregistry/api.sock`).
   **Pitfall:** a client uses the first socket file that *exists*, even if
   nothing is listening on it. A leftover per-user socket from a per-user
   daemon that crashed or was killed hides a healthy system daemon. The
   client gets exit 3 while `systemctl status` says the service is fine.
   Delete the stale per-user socket (after confirming no per-user daemon is
   running: `pgrep -fl "mbregistry.*run"`).
3. A stale `MBREGISTRY_SOCKET` in your environment beats auto-discovery.
   Run `env | grep MBREGISTRY`.
4. A daemon started with a custom `--socket` can only be reached by passing
   the same `--socket` (or env var) to the client.
5. The socket file exists but the connection is refused: the daemon died
   uncleanly, or a second daemon replaced the socket (see 13.2). Restart
   the service. On start the daemon deletes and recreates its socket.

### 13.2 Port already in use

The daemon exits with `OSError: [Errno 98] Address already in use` (Linux)
or `[Errno 48]` (macOS), and the service manager keeps restarting it.

```sh
sudo ss -ltnp | grep -E ':(7440|7442|7443|7444|7445)\b'     # Linux
sudo lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(7440|744[2-5])' # macOS / Linux
```

Usual cause: **a second `mbregistry`**. A foreground `mbregistry service run` left
over, or both a user and a system daemon. Stop the extra one; run one per
host. **Then restart the real service.** A second daemon binds its local
socket *before* it fails on port 7440. If it used the same socket path, it
has replaced the running daemon's socket, and the crashed copy leaves that
socket dead. If it used a different path, it leaves a stale per-user socket
behind (see 13.1, pitfall). If another program owns the port, move `mbregistry` with
`--remote-port`/`--peer-pub-port`/`--peer-snapshot-port`, and move the
**whole fleet** to match (section 9). Ports 7444/7445 can't be moved. Use
`--no-relay-pool` to free 7444. 7445 is always used.

### 13.3 Peers can't reach this host / it advertises `127.0.1.1`

Symptom: other hosts list this host's boards but remote operations time out
or are refused, or mDNS shows a loopback address:

```sh
avahi-browse -rt _mbregistry._tcp | grep -A3 "$(hostname)"   # look at 'address = [...]'
ip route show default                                         # empty = no default route
```

- Builds before the no-default-route fix (anything up to tag
  `v0.20260924.2`) fall back to resolving the hostname. On Debian that
  gives `127.0.1.1`. **Upgrade** (section 11). Current builds pick the first
  real interface instead.
- Still wrong? The daemon skips interfaces named
  `docker*`/`br-*`/`veth*`/`virbr*`/`vmnet*`/`bridge*`/`utun*`/`tun*`/`tap*`.
  If the LAN is on one of those, or the first interface isn't the LAN,
  give the host a default route through the LAN interface.
- Also check the firewall (section 10), and test from a peer:
  `nc -vz <host> 7440`.

### 13.4 Permission denied on USB (local clients)

Symptom: `mbserial <name>` or `mbdeploy deploy <name>` on a board plugged
into **this** host fails with `Permission denied: '/dev/ttyACM0'`, or pyOCD
finds no probe or hangs. Remote boards are not affected.

```sh
ls -l /dev/ttyACM*                      # want: crw-rw---- root plugdev
id                                      # want: plugdev in the list
ls /etc/udev/rules.d/99-mbregistry-cmsis-dap.rules
```

Fix: run `sudo mbregistry service install --system` if the rule is missing
(it also reloads the udev rules and adds you to `plugdev`), then **log in
again** (new SSH session) for the new group membership to take effect.
Group membership doesn't apply to an existing session. Stopgap: `sudo
mbserial ...`. The daemon itself runs as root and is never affected.

### 13.5 Stale or missing peers

- `peer unreachable` in `mbregistry list`: that host's daemon is down or
  unreachable. Check it. The rows come back to life when it reconnects.
- A **retired** host's rows never go away on their own, because peers are
  never deleted. To clear them, on each host that still shows them:

  ```sh
  sudo systemctl stop mbregistry
  sudo /opt/mbtools/bin/python - <<'EOF'
  import sqlite3
  db = sqlite3.connect("/var/lib/mbregistry/devices.db")   # path per section 3
  db.execute("DELETE FROM device WHERE host = ?", ("OLDHOST",))
  db.execute("DELETE FROM peer WHERE host = ?", ("OLDHOST",))
  db.commit()
  EOF
  sudo systemctl start mbregistry
  ```

  The heavier option is to move `devices.db` aside and restart. Local
  boards are re-probed. Robot names (`mbrelay names`) come back from peers'
  snapshots, but check with `mbrelay names list`.
- A peer never appears: multicast is blocked between the hosts (VLANs,
  Wi-Fi isolation), or UDP 5353 / TCP 7442-7443 are firewalled. Add
  `--peer HOST` on one side (section 9), and confirm with `avahi-browse -rt
  _mbregistry._tcp` / `dns-sd -B _mbregistry._tcp` from each side.
- A peer appears but its boards don't: its 7443 (snapshot) is blocked, or
  the auth tokens differ. The daemon's log shows a warning about it.
- After a reboot the daemon may start before the network is up. If it sees
  no peers until restarted, restart it (`systemctl restart mbregistry` /
  `launchctl kickstart -k ...`).

### 13.6 Other quick checks

- `mbregistry list` shows a board as `gone`: it was unplugged, or USB
  re-enumerated. Replug. The daemon polls every `--interval` seconds.
- `mbdeploy deploy` refuses a board: its announced role looks like a
  relay/bridge. Use `--force-relay` only if you really mean to reflash a
  relay.
- Robot-console can't find relays: check `avahi-browse -rt _mbrelay._tcp`
  (port 7444, TXT `registry=7445`). Make sure `--no-relay-pool` isn't set on
  the relay host, and that ports 7444/7445 are open.
