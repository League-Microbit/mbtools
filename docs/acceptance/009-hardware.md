# Sprint 009 — Docker image for mbregistry: hardware acceptance on `hodr`

Sprint 009 (`Docker image for mbregistry`), ticket
`004-hardware-acceptance-run-the-container-on-hodr`. Run 2026-09-28
against `hodr` (Nolanet node, Debian 13 aarch64, dedicated test board
`vevav`/`2e78ea8f`, `RADIOBRIDGE/relay`, `/dev/ttyACM0`) as the build/run
target, and `meili` as the peer host, per project `CLAUDE.md`'s
hardware-testing table. This validates the whole container distribution
channel added by tickets 001-003 (`Dockerfile`/`.dockerignore`/
`compose.yaml`, the `docker.yml` GHCR workflow, and `docs/docker.md`)
against real USB and mDNS — the thing no unit/integration test exercises.

`head -1 $(which mbregistry)` on both `hodr` and `meili` prints
`#!/opt/mbtools/bin/python3` — both hosts' client commands run from
`/opt/mbtools`, not `~/mbtools-venv` (the path CLAUDE.md's ticket-007
paper-cut note describes on `meili`/`loki`). There is no `~/mbtools-venv`
on either host at all; `/opt/mbtools` is `scripts/deploy-host.sh`'s own
current install target and is also what `mbregistry.service`'s
`ExecStart=` points at (confirmed via `systemctl cat mbregistry`), so
this is not an instance of that quirk — just a note that the venv path
referenced in that older CLAUDE.md entry no longer describes either
host's layout. `meili`'s client build (`0.20260925.3`) trailed `hodr`'s
container build (`0.20260926.5`) by one day at session start; this
didn't affect any scenario below (the client/daemon wire protocol is
unversioned JSON over the API socket/TCP, not gated on matching
versions).

## Pre-existing state

- `hodr`: Docker **already installed** (`Docker version 29.4.3, build
  055a478` — not installed by this session; ticket's "install if
  absent" step was a no-op). Native `mbregistry.service` was `active`/
  `enabled` at session start, `vevav` showing `free`/`host=local` on
  `/dev/ttyACM0`.
- `hodr`'s root filesystem (`/dev/mmcblk0p2`, a 6.8G SD-card partition)
  was **already at 100% used / 0 bytes free** at session start, before
  this session touched anything — `/usr` alone is 4.7G (normal Debian
  aarch64 footprint on this card), plus an unrelated pre-existing
  Docker Swarm-style monitoring stack (`cadvisor`, `node-exporter`,
  `traefik/whoami` images/containers, ~150MB, not part of `mbtools`,
  not touched by this session). This is a real, load-bearing finding
  for future sessions building on `hodr` — see "Disk space on `hodr`"
  below.

## Scenario: image build on `hodr` (arm64, native)

**PASS, after a disk-space workaround** (see below). GHCR has not
published an image yet — `gh api repos/League-Microbit/mbtools/packages/
container/mbtools/versions` returns 404 and no `v*` tag has been pushed
this sprint (`sprint.md`'s own Success Criteria are still open at this
point in the sprint) — so, per the ticket's own fallback, the image was
built locally: the repo tree was `rsync`'d to `~/mbtools-docker-src` on
`hodr` (95MB, matching the local tree's `pyproject.toml` version
`0.20260926.5` exactly) and built with `sudo docker build .` from that
copy, tagged `ghcr.io/league-microbit/mbtools:local` and `:latest`.

**Disk space on `hodr`**: the first build attempt failed immediately
in the `apt-get install libusb-1.0-0 udev` layer —
`E: You don't have enough free space in /var/cache/apt/archives/`
— because the host had 0 bytes free before the build even started.
Fixed by clearing two purely-local caches that cost nothing to
regenerate: `sudo docker builder prune -af` (203MB, stale build cache
from nothing — this was the *first* build attempt) and
`sudo rm -rf /var/lib/apt/lists/*` (148MB, the host's own apt package
lists, unrelated to the container build's isolated apt state) — freed
enough (333MB) for the rebuild to complete. The image itself is 385MB
on disk (86.8MB unique content over the shared `python:3.13-slim`
base). After validation, the image was removed
(`sudo docker rmi ghcr.io/league-microbit/mbtools:latest :local`) and
`sudo apt-get update` re-run to regenerate the apt lists cache this
session deleted, restoring the host to **151MB free** (up from the 0
bytes it started at, since the removed image/build-cache/rsync copy
freed more than the regenerated apt lists cost). This is a real,
recurring constraint on this specific host (small SD card + an
unrelated monitoring stack), not a Dockerfile/CI bug — GHCR CI builds
run on GitHub-hosted runners with ample disk and are unaffected. Noting
it here rather than in `docs/docker.md` (which documents the *image*,
not a specific host's disk budget) — a future session building on
`hodr` again should expect to need the same `docker builder prune -af`
/ apt-lists-clear step first, or should prefer pulling a published GHCR
tag instead of building locally once one exists.

No `Dockerfile`/`compose.yaml`/`docs/docker.md` correction was needed —
the documented recipe (device-cgroup-rule majors 166/189/242, confirmed
already correct for `hodr` per `docs/docker.md`'s own text) worked
exactly as written; see the Run Recipe scenario below.

## Scenario: run recipe, exactly as documented

**PASS.** Native `mbregistry.service` was stopped
(`sudo systemctl stop mbregistry`; `/var/lib/mbregistry/devices.db`
persisted on disk as expected) and the container started with the
**exact** `docker run` recipe from `docs/docker.md` section 4 (no ad
hoc variant — `--network host`, `-v /dev:/dev`, the two persistent
volumes, and the three documented `--device-cgroup-rule` majors,
no `--privileged` fallback needed):

```
sudo docker run -d --name mbregistry --network host --restart unless-stopped \
  -v /dev:/dev -v /var/lib/mbregistry:/var/lib/mbregistry -v /run/mbregistry:/run/mbregistry \
  --device-cgroup-rule='c 166:* rmw' --device-cgroup-rule='c 189:* rmw' --device-cgroup-rule='c 242:* rmw' \
  ghcr.io/league-microbit/mbtools:latest
```

Container log confirms it advertises the host's own identity, exactly
as section 6 of `docs/docker.md` describes:

```
INFO mbtools.registry.peering: peering: advertising hodr as hodr._mbregistry._tcp.local. (192.168.1.148:7440, pub_port=7442, snapshot_port=7443)
mbregistry: listening on /run/mbregistry/api.sock (local api), 7440 (remote api), store at /var/lib/mbregistry/devices.db, peering active (pub 7442, snapshot 7443, advertising 192.168.1.148), relay pool on 7444, names API on 7445
```

## Scenario: (a) own board shows `host=local`

**PASS.** `sudo docker exec mbregistry mbregistry list` inside the
container:

```
STATE  LOCKED  NAME   UID       FIRMWARE            HOST   PORT
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay   local  /dev/ttyACM0
```

## Scenario: (f) host-side client via the bind-mounted socket

**PASS.** `sudo mbregistry list` run directly on `hodr` (the host's own
`/opt/mbtools` client, no `--socket` override needed) shows the same
row, `host=local` — confirms `/run/mbregistry` being bind-mounted means
the container's API socket is the same file a host-side client already
looks for, exactly as `docs/docker.md` section 7 claims.

## Scenario: (b) peer sees `host=hodr`

**PASS.** `mbregistry list` on `meili` (a genuinely separate host, over
mDNS + the peering ports, not a loopback check):

```
STATE  LOCKED  NAME   UID       FIRMWARE            HOST  PORT
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay   hodr  /dev/ttyACM0
```

Confirms mDNS discovery and all three peering ports (7440/7442/7443)
work from inside the container under `--network host`, and that the
container's own self-filter correctly compares real hostnames (it
never shows up as `local` on `meili`, nor does `meili` ever show up on
`hodr`'s own list as anything but itself/other real peers).

## Scenario: (c) remote flash from a peer

**PASS.** Fetched the radio-relay firmware from the CLAUDE.md firmware
table (`gh release download v0.20260913.2 -R
League-Robotics/microbit-radio-relay -p MICROBIT.hex`), copied it to
`meili`, and flashed `hodr`'s board **from `meili`**, against the
containerized daemon, with `mbdeploy deploy vevav --hex
relay-MICROBIT.hex --force-relay` (`--force-relay` needed since
`vevav`'s already-announced role is a relay):

```
0016954 C flash erase sector failure (address 0x00000000; result code 0x67) [__main__]
flash failed — attempting CTRL-AP mass erase to recover a locked device, then retrying.
...
0025765 I Erased 271360 bytes (67 sectors), programmed 271360 bytes (67 pages), identical 0 bytes (0 pages) at 13.57 kB/s [loader]
mbdeploy: vevav re-announced as RADIOBRIDGE (flash_count=1)
```

The transient erase failure and automatic mass-erase retry is
`mbdeploy`'s own documented recovery behavior (same shape as the
transient-error retry noted in `docs/acceptance/005-hardware.md`
Scenario 3), not a container-specific bug — the flash still completed
and the board re-announced correctly. Confirmed end-to-end: the
container's own `mbregistry list` immediately after showed `vevav`
`free`/`local` again, still `RADIOBRIDGE/relay`. This is the clearest
proof the container's USB/pyOCD access (the raw-USB/hidraw
device-cgroup rules, not just the CDC serial ttyACM rule) works, since
flashing needs CMSIS-DAP access that plain serial reads/writes don't.

## Scenario: (d) serial via `mbserial`

**PASS, both locally and remotely.** Ground truth first, `sudo mbserial
vevav --reset HELLO` run directly on `hodr`:

```
DEVICE:RADIOBRIDGE:relay:vevav:536019796
```

Then the same command from `meili` (`mbserial vevav@hodr --reset
HELLO`) against the containerized daemon over the network returned the
identical banner — confirms the serial data path works both through
the bind-mounted `/dev` locally and relayed over the peering network
from a remote host.

## Scenario: (e) replug/reset detection

**PASS.** Physical unplug/replug wasn't available non-interactively
over SSH, so — per the ticket's own documented substitute, and mirroring
the real hardware-level technique sprint 005 ticket 001 used on
`magni` — a genuine USB-level detach/reattach was forced via sysfs
driver unbind/bind on `hodr`'s micro:bit (`1-1.3`, found via `udevadm
info -q path -n /dev/ttyACM0`):

```
$ sudo docker exec mbregistry mbregistry list | grep vevav   # before
free   -   vevav  2e78ea8f  RADIOBRIDGE/relay  local  /dev/ttyACM0

$ sudo sh -c 'echo -n 1-1.3 > /sys/bus/usb/drivers/usb/unbind'
$ sudo docker exec mbregistry mbregistry list | grep vevav   # after unbind
gone   -   vevav  2e78ea8f  RADIOBRIDGE/relay  local  /dev/ttyACM0
$ ls /dev/ttyACM0
ls: cannot access '/dev/ttyACM0': No such file or directory

$ sudo sh -c 'echo -n 1-1.3 > /sys/bus/usb/drivers/usb/bind'
$ ls /dev/ttyACM0
/dev/ttyACM0
$ sudo docker exec mbregistry mbregistry list | grep vevav   # after rebind
free   -   vevav  2e78ea8f  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

The device node disappeared entirely from the host during the unbind
(confirming this is a real device-node-level event, not just a state
flag) and the daemon inside the container correctly flagged `gone`,
then picked the new device node back up on rebind with the full
identity reprobed correctly (`RADIOBRIDGE/relay`, not just "some device
present") — no container recreation needed. This is exactly the
regression the bind-mount + device-cgroup-rules recipe exists to pass,
per `docs/docker.md` section 5: a static `--device /dev/ttyACM0` flag
would have kept the old (now-stale) device node reference and never
seen the new node the kernel assigns after re-enumeration. `meili`'s
own peer view was re-checked after rebind and also correctly showed
`vevav` `free`/`host=hodr` again, confirming the reattach propagated
through peering too, not just the local container view.

## Teardown

Container stopped and removed (`sudo docker stop mbregistry && sudo
docker rmi ...`); native `mbregistry.service` restarted
(`sudo systemctl start mbregistry`). Confirmed restored:

```
$ systemctl is-active mbregistry; systemctl is-enabled mbregistry
active
enabled
$ sudo mbregistry list | grep vevav
free   -   vevav  2e78ea8f  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

Matches the exact pre-session state (`active`/`enabled`, `vevav`
`free`/`local`). The built image (`ghcr.io/league-microbit/mbtools:local`
and `:latest`, 385MB) was removed and the apt-lists cache this session
cleared to make room for the build was regenerated
(`sudo apt-get update`), leaving `hodr` at 151MB free — better than the
0 bytes it had at session start, not worse. **Docker itself is left
installed** (`29.4.3`) per the ticket's instruction, since it was
already present before this session and nothing here requires removing
it.

## Summary

| Scenario | Result |
|---|---|
| Docker present/installed on `hodr` | Already present (`29.4.3`); no install needed |
| Image build on `hodr` (arm64, local — GHCR not yet published this sprint) | PASS, after clearing build cache + apt lists to work around a pre-existing 0-free-bytes disk state (host quirk, not a packaging bug) |
| Run recipe exactly as `docs/docker.md` documents (no ad hoc variant, no `--privileged` fallback needed) | PASS |
| (a) `hodr`'s own board shows `host=local` in its own registry view | PASS |
| (f) host-side client via the bind-mounted socket | PASS |
| (b) peer (`meili`) shows `hodr`'s board as `host=hodr` | PASS |
| (c) remote flash via `mbdeploy` from `meili` against the containerized daemon | PASS |
| (d) serial via `mbserial`, locally and from a peer | PASS |
| (e) replug/reset detection (USB unbind/bind substitute for a physical replug) | PASS |
| Teardown: `hodr` restored to native-service state | PASS — `active`/`enabled`, board `free`/`local`, matches pre-session state |

**No `Dockerfile`/`compose.yaml`/`docs/docker.md` correction was
needed** — every documented claim in `docs/docker.md` (the run recipe,
the device-cgroup majors, the peering/hostname behavior, the
bind-mounted-socket host-client claim, the USB replug rationale) held
up exactly as written against real hardware. The one real finding this
session surfaced is host-specific and operational, not a code or docs
defect: **`hodr`'s SD-card root partition is essentially always at or
near 100% full** (pre-existing, from normal OS footprint plus an
unrelated monitoring stack), so a from-source local image build there
needs `docker builder prune -af` and/or clearing `/var/lib/apt/lists`
first, or should pull a published GHCR tag once sprint 009's own
pipeline has pushed one — recorded here rather than as a `CLAUDE.md`
change, since it's a build-time operational note for this one host, not
a change to the hardware-testing table's own facts. No robot or relay
in active production use was disturbed — `vevav` is `hodr`'s own
dedicated, freely-flashable test board per `CLAUDE.md`; `torture`'s
relays were never touched.
