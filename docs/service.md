# Installing and running mbtools as a service

This is the operator reference for installing `mbtools` and running its
daemon, `mbregistry`, on Linux, macOS and Windows. It is written for people
and for AI agents: every path, port, flag and environment variable below was
checked against the source and the programs' own `--help`. Where this page
and the code disagree, the code wins. Run `<program> --help` or
`<program> <subcommand> --help` for the authoritative flag list.

- [1. What runs where](#1-what-runs-where)
- [2. Install](#2-install)
- [3. `mbregistry run`: defaults](#3-mbregistry-run-defaults)
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
| `mbregistry run` | The daemon. One per host. Watches USB, identifies micro:bits, keeps the device database, grants locks, peers with other hosts. | It *is* the daemon. |
| `mbregistry list` | Client | yes |
| `mbdeploy deploy / list / debug` | Client | yes |
| `mbdeploy build` | Local build helper | no |
| `mbserial` | Client | yes |
| `mbrelay connect / names` | Client | yes |

Run **exactly one** `mbregistry` per host. Two daemons on one host (say a
per-user one and a system one) fight over the same USB devices and the same
TCP ports. The second one fails to bind port 7440.

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
/opt/mbtools/bin/python -c "import importlib.metadata as m; print(m.version('mbtools'))"
```

None of the four programs has a `--version` flag. Use the
`importlib.metadata` line above, or `pip show mbtools`.

## 3. `mbregistry run`: defaults

`mbregistry run` needs no flags. The daemon keeps its database and local
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
  `mbregistry run --help` as a normal user, it shows the per-user path.
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
| **7444** | TCP | Robot-console-compatible relay pool (`_mbrelay._tcp`) | no override; `--no-relay-pool` disables it |
| **7445** | HTTP | Robot-console-compatible `/names/<name>` API | no override; always on |
| 5353 | UDP | mDNS (multicast) | n/a |

mDNS service types:

| Service | Advertised by | Instance name | Meaning |
|---|---|---|---|
| `_mbregistry._tcp` | every `mbregistry` | host name | Peer discovery. TXT carries this host's remote/peering ports. |
| `_mbrelay._tcp` | the relay pool (unless `--no-relay-pool`) | host name | Legacy relay discovery for robot-console. SRV port = 7444, TXT `registry=7445`. |

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

### `mbregistry run`

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
| `--no-relay-pool` | (none) | pool on | Set on hosts with no relay boards |
| `--windows-service` | (none) | off | Only for the Windows SCM `binPath=`; not for interactive use |

### Clients

| Variable / flag | Used by | Meaning |
|---|---|---|
| `--socket`, `MBREGISTRY_SOCKET` | all clients | Talk to the daemon at this socket instead of auto-discovery |
| `GITHUB_TOKEN` | `mbdeploy deploy --repo` | GitHub API token for release lookups (rate limits, private repos) |

`mbdeploy deploy --repo` caches downloaded hex files in
`~/.cache/mbtools/hex/<owner>/<repo>/<tag>/`.

### `mbregistry install-service`

| Flag | Default |
|---|---|
| `--output PATH` | `/etc/systemd/system/mbregistry.service` |
| `--udev-output PATH` | `/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules` |
| `--user NAME` | `$SUDO_USER`, then `$USER`, then the current user. Only affects the printed `usermod` line. |

## 6. Linux: systemd service and udev rule

### 6.1 Install the service

```sh
sudo /opt/mbtools/bin/mbregistry install-service
```

This **writes two files and prints the follow-up commands without running
them**:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now mbregistry.service
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev <you>     # then log in again (new SSH session)
```

It is idempotent. Re-running rewrites both files with identical content and
never starts, stops, or restarts the service. Without root it fails with
`mbregistry: could not write /etc/systemd/system/mbregistry.service: ...`
and exits 1.

Run it with the venv you want the service to use (`/opt/mbtools/bin/...`).
The unit's `ExecStart=` is the Python interpreter of whoever ran
`install-service`.

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
- **To add flags or env vars, don't edit the unit.** `install-service`
  overwrites it. Use a drop-in (`sudo systemctl edit mbregistry.service`):

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
# (VID:PID 0d28:0204). Written by `mbregistry install-service`
# (ticket 008) -- re-running it overwrites this file with identical
# content, so re-running install-service is idempotent. See
# mbtools.registry.cli.render_udev_rule()'s docstring for why each rule
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

Uninstall:

```sh
sudo systemctl disable --now mbregistry
sudo rm /etc/systemd/system/mbregistry.service /etc/udev/rules.d/99-mbregistry-cmsis-dap.rules
sudo systemctl daemon-reload && sudo udevadm control --reload-rules
sudo rm -rf /opt/mbtools /var/lib/mbregistry    # optional: code and database
```

## 7. macOS: launchd

`mbregistry install-service` does **not** support macOS; it writes systemd
files. Use one of the two plists below. No USB drivers or udev equivalent
are needed on macOS. A normal user can open the micro:bit's serial and
CMSIS-DAP interfaces.

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
interpreter directly (`python -m mbtools.registry.cli run`). This is the
same entry point the systemd unit uses, and it keeps pyOCD on the same
interpreter.

`KeepAlive` → `SuccessfulExit=false` restarts the daemon after a crash
(non-zero exit) but not after a clean stop, which matches systemd's
`Restart=on-failure`. `ThrottleInterval` spaces restarts 10 s apart.

### 7.1 LaunchDaemon (root, starts at boot)

Install into `/opt/mbtools` (section 2.1). Then:

```sh
sudo tee /Library/LaunchDaemons/org.jointheleague.mbregistry.plist >/dev/null <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.jointheleague.mbregistry</string>
    <key>ProgramArguments</key>
    <array>
        <string>/opt/mbtools/bin/python</string>
        <string>-m</string>
        <string>mbtools.registry.cli</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Library/Logs/mbregistry.log</string>
    <key>StandardErrorPath</key>
    <string>/Library/Logs/mbregistry.log</string>
</dict>
</plist>
EOF
sudo chown root:wheel /Library/LaunchDaemons/org.jointheleague.mbregistry.plist
sudo chmod 644        /Library/LaunchDaemons/org.jointheleague.mbregistry.plist
plutil -lint          /Library/LaunchDaemons/org.jointheleague.mbregistry.plist

sudo launchctl enable    system/org.jointheleague.mbregistry
sudo launchctl bootstrap system /Library/LaunchDaemons/org.jointheleague.mbregistry.plist
sudo launchctl kickstart -k system/org.jointheleague.mbregistry   # (re)start now
```

To add flags, append more `<string>` elements to `ProgramArguments` (e.g.
`--no-relay-pool`, `--peer`, `other-host`). To set an env var such as
`MBREGISTRY_TOKEN`, add it to `EnvironmentVariables`. After editing, run
`bootout` and then `bootstrap` again.

Manage it:

```sh
sudo launchctl print system/org.jointheleague.mbregistry | head -30   # state, pid, last exit code
sudo launchctl kickstart -k system/org.jointheleague.mbregistry      # restart
sudo launchctl bootout system/org.jointheleague.mbregistry           # stop and unload
sudo launchctl disable system/org.jointheleague.mbregistry           # don't load at boot
tail -f /Library/Logs/mbregistry.log
```

### 7.2 LaunchAgent (one user, starts at login)

launchd doesn't expand `~`, so the heredoc below writes absolute paths from
`$HOME`. Install into `~/.local/share/mbtools` (section 2.2) first.

```sh
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
cat > ~/Library/LaunchAgents/org.jointheleague.mbregistry.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>org.jointheleague.mbregistry</string>
    <key>ProgramArguments</key>
    <array>
        <string>$HOME/.local/share/mbtools/bin/python</string>
        <string>-m</string>
        <string>mbtools.registry.cli</string>
        <string>run</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>$HOME</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$HOME/Library/Logs/mbregistry.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/Library/Logs/mbregistry.log</string>
</dict>
</plist>
EOF
plutil -lint ~/Library/LaunchAgents/org.jointheleague.mbregistry.plist

launchctl enable    gui/$(id -u)/org.jointheleague.mbregistry
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.jointheleague.mbregistry.plist
launchctl kickstart -k gui/$(id -u)/org.jointheleague.mbregistry
```

If you used `uv tool install` instead, point `ProgramArguments[0]` at that
tool's interpreter. Find it with
`head -1 "$(command -v mbregistry)"`, which prints the shebang line.

Manage it with the same verbs as above, using `gui/$(id -u)` in place of
`system`, no `sudo`, and the log at `~/Library/Logs/mbregistry.log`.

### 7.3 macOS notes

- **Local Network privacy (macOS 15+).** Programs a user starts can be
  blocked from the LAN until they are allowed in *System Settings → Privacy
  & Security → Local Network*. If a LaunchAgent daemon finds no peers but
  the same command run in Terminal does, look there first. A root
  LaunchDaemon is not subject to this prompt.
- **Application Firewall.** If it is on, allow incoming connections for
  the venv's Python (section 10).
- To run in the foreground for debugging, stop the service first. Only one
  daemon may run per host. Then run `mbregistry run` (as yourself) or
  `sudo mbregistry run` (to use the system paths).

## 8. Windows: SCM service (not hardware-verified)

> **Not verified on real hardware.** The Windows code paths (named-pipe API,
> SCM integration, `%ProgramData%` paths) are covered only by unit tests
> with fakes. Nobody has run them against a real board on Windows yet.

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
  service and run `C:\mbtools\Scripts\mbregistry run` in a console.
- Firewall: see section 10.

## 9. Peering, `--peer`, and the auth token

- **Automatic.** Every daemon advertises and browses `_mbregistry._tcp` over
  mDNS. It connects to each peer's ZeroMQ PUB (7442) and snapshot (7443)
  ports and merges the peer's devices into its own list (HOST column =
  peer's host name). No configuration is needed on a flat LAN where
  multicast works.
- **Explicit peers** for networks mDNS can't cross (VLANs, VPNs, Wi-Fi
  with client isolation):
  `mbregistry run --peer host-a --peer host-b.example.org:7440`. `PORT` is the
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

```sh
# Linux, /opt venv
sudo /opt/mbtools/bin/pip install --upgrade "git+https://github.com/League-Microbit/mbtools.git"   # or @<tag>
sudo /opt/mbtools/bin/mbregistry install-service     # idempotent; picks up any unit/rule change
sudo systemctl daemon-reload
sudo systemctl restart mbregistry
mbregistry list

# macOS LaunchDaemon
sudo /opt/mbtools/bin/pip install --upgrade "git+https://github.com/League-Microbit/mbtools.git"
sudo launchctl kickstart -k system/org.jointheleague.mbregistry

# uv tool (per user)
uv tool install --reinstall "git+https://github.com/League-Microbit/mbtools.git"
```

If you **recreate** the venv instead of upgrading it (e.g. `uv venv
--clear`), stop the service first. The running root daemon leaves root-owned
`__pycache__` files that a non-root rebuild can't delete. Upgrade one host
at a time and check `mbregistry list` on it and on one peer before moving
on.

## 12. Logs

The daemon logs to **stderr**: the start-up line (section 3), plus
warnings and errors from peering, the store and the remote API. It logs no
INFO messages, so a quiet log is normal.

| Platform | Where |
|---|---|
| Linux (systemd) | `journalctl -u mbregistry` (`-f` to follow, `-b` since boot, `--since "10 min ago"`) |
| macOS LaunchDaemon | `/Library/Logs/mbregistry.log` |
| macOS LaunchAgent | `~/Library/Logs/mbregistry.log` |
| Windows service | not captured; run `mbregistry run` in a console to see output |
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

Usual cause: **a second `mbregistry`**. A foreground `mbregistry run` left
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

Fix: run `sudo mbregistry install-service` if the rule is missing, then
`sudo udevadm control --reload-rules && sudo udevadm trigger`, then
`sudo usermod -aG plugdev $USER`, and **log in again** (new SSH session).
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
