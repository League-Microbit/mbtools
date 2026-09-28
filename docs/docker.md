# Running mbregistry in Docker

This is the operator reference for the container distribution channel added
in sprint 009: an alternative to the venv/`.deb` install (see the main
[README](../README.md) and [docs/service.md](service.md)) for **Linux**
hosts that would rather pull and run a versioned image than manage a host
Python venv or a `.deb` upgrade cycle. It documents the same daemon,
`mbregistry` (plus the `mbdeploy`/`mbserial`/`mbrelay` clients), packaged and
launched differently — nothing about the code inside the container differs
from a native install.

- [1. The image](#1-the-image)
- [2. Linux only](#2-linux-only)
- [3. Before you start: stop the native service](#3-before-you-start-stop-the-native-service)
- [4. Run recipe](#4-run-recipe)
- [5. How USB works in the container](#5-how-usb-works-in-the-container)
- [6. How mDNS/peering works in the container](#6-how-mdnspeering-works-in-the-container)
- [7. Reaching the daemon from the host](#7-reaching-the-daemon-from-the-host)
- [8. Updating](#8-updating)
- [9. Limitations](#9-limitations)

## 1. The image

Every `v*` release tag publishes a multi-arch (`linux/amd64` +
`linux/arm64`) image to GitHub Container Registry:

```
ghcr.io/league-microbit/mbtools:<version>
ghcr.io/league-microbit/mbtools:latest
```

Built by [`.github/workflows/docker.yml`](../.github/workflows/docker.yml)
from the [`Dockerfile`](../Dockerfile) in this repo, on the same `v*`-tag
trigger `release-deb.yml` uses (see the README's `.deb` release procedure)
— the tag must match `pyproject.toml`'s version or the build fails. A
`workflow_dispatch` run or a PR touching the Dockerfile/workflow builds both
architectures without pushing, as a build-correctness check only; it does
not publish a new `:latest`.

The image runs `mbregistry` as its default command (`ENTRYPOINT
["mbregistry"]`, `CMD ["run"]`); `mbdeploy`, `mbserial`, and `mbrelay` are
also on `PATH` inside the container for `docker exec` or
`--entrypoint`.

## 2. Linux only

Docker Desktop on macOS and Windows runs containers inside a VM that has no
USB passthrough and no LAN multicast — both are required (sections 5 and 6
below), so this image cannot reach a locally-attached micro:bit or take
part in mDNS peering on those platforms. `braeburn`, `gala`, and any
Windows host keep the native install (`docs/service.md`); this is not a
gap this sprint attempts to work around.

## 3. Before you start: stop the native service

A native `mbregistry.service` and the container want the same TCP ports,
the same serial/USB devices, and (if you point them at the same paths) the
same database and socket. Run at most one `mbregistry` per host:

```sh
sudo systemctl stop mbregistry
sudo systemctl disable mbregistry   # if switching to the container permanently
```

## 4. Run recipe

The supported recipe is `--network host`, a full `/dev` bind mount plus
device-cgroup rules for the relevant device majors, and persistent volumes
for the daemon's database and socket directories. The example
[`compose.yaml`](../compose.yaml) in the repo root is the authoritative
version of this recipe; the equivalent `docker run` is:

```sh
docker run -d \
  --name mbregistry \
  --network host \
  --restart unless-stopped \
  -v /dev:/dev \
  -v /var/lib/mbregistry:/var/lib/mbregistry \
  -v /run/mbregistry:/run/mbregistry \
  --device-cgroup-rule='c 166:* rmw' \
  --device-cgroup-rule='c 189:* rmw' \
  --device-cgroup-rule='c 242:* rmw' \
  ghcr.io/league-microbit/mbtools:latest
```

Or with Compose, from a checkout (or just the one file):

```sh
docker compose up -d
```

The three device-cgroup rules are the majors confirmed on a real host
(`hodr`: Debian 13, aarch64) via `cat /proc/devices`: `166` (`ttyACM`,
the micro:bit's CDC serial port), `189` (`usb_device`, raw USB — pyOCD's
CMSIS-DAP v2 backend), and `242` (`hidraw` — pyOCD's CMSIS-DAP v1
backend). **`hidraw`'s major is not guaranteed to be `242` across every
distro/kernel** — it's dynamically assigned; if a board doesn't show up
after starting the container on a different host, check
`grep hidraw /proc/devices` there and adjust the rule.

**Fallback**: if the cgroup rules above don't cover a given host (a
different `hidraw` major, or some other class of device), drop them and
add `--privileged` instead (`privileged: true` in `compose.yaml`, commented
out by default) — full device access, no cgroup-rule bookkeeping, at the
cost of a much broader container privilege grant.

The container runs as root either way, matching `mbregistry.service`'s own
privilege level (needed for raw USB/CMSIS-DAP access).

## 5. How USB works in the container

`mbregistry` finds boards the same way natively and in the container:
pyserial's `comports()` reads `/sys` for each device's vid/pid/serial
number and opens `/dev/ttyACM*` for the CDC serial connection; flashing
with pyOCD needs raw CMSIS-DAP access, either `/dev/bus/usb/*` via libusb
(CMSIS-DAP v2 boards) or `/dev/hidraw*` (v1 boards).

A static `--device /dev/ttyACM0`-style flag only grants access to the
device node that exists *at container start time* — it's a snapshot. A
micro:bit replug (or a fresh enumeration after a power cycle) gets a new
device node the container was never granted access to, so the board
silently disappears until the container is recreated. Bind-mounting the
whole `/dev` directory (section 4) instead means new device nodes appear
inside the container as they appear on the host; the device-cgroup rules
then control which device *classes* (majors) the container may open,
without needing to know node names in advance.

## 6. How mDNS/peering works in the container

`--network host` is required, not just convenient. `mbregistry` discovers
peers via mDNS (`_mbregistry._tcp.local.`), which relies on IP multicast —
multicast doesn't cross a bridge network, so a bridged container would
never see or be seen by other hosts' registries. Host networking also
means the daemon advertises the *host's* real IP and hostname, not a
private container IP/hostname — both matter: peers connect back to the
advertised address, and (per this project's own multi-homed self-filter
fix, `src/mbtools/registry/peering.py`) a host's own mDNS self-discovery
is partly filtered by comparing hostnames, which only works if the
container is advertising the host's actual hostname. See this repo's
`CLAUDE.md`, "Peering ports (sprint 003)", for the full list of ports this
implies are needed between hosts: **7440** (remote control-plane API),
**7442** (peering PUB/event bus), **7443** (peering snapshot REQ/REP), and
**7444** (relay pool). With `--network host` none of these need `-p`
mapping — they're already the host's own ports, exactly as a native
install uses them.

Bridge networking is not supported for the reasons above; there is no
recipe for it in this project.

With this recipe, a containerized `mbregistry` is indistinguishable from a
native install to every other peer on the LAN.

## 7. Reaching the daemon from the host

Because `/run/mbregistry` is bind-mounted (section 4), the container's API
socket is the same file the native daemon would use. Host-side client
commands — `mbregistry list`, `mbdeploy deploy`, `mbserial`, `mbrelay
connect` — installed on the host (venv or `.deb`) work unmodified against
a containerized daemon; no `--socket` override is needed. Equivalently, run
a client command inside the container itself:

```sh
docker exec mbregistry mbregistry list
```

## 8. Updating

Pull the new image and recreate the container; the bind-mounted
`/var/lib/mbregistry` and `/run/mbregistry` volumes mean the device
database and any peer state survive the restart:

```sh
docker compose pull && docker compose up -d
```

or, with plain `docker run`:

```sh
docker pull ghcr.io/league-microbit/mbtools:latest
docker rm -f mbregistry
# re-run the `docker run` command from section 4
```

Pin a specific released version instead of always tracking `:latest` by
using `ghcr.io/league-microbit/mbtools:<version>` in place of `:latest`
above.

## 9. Limitations

- **Linux only** (section 2) — Docker Desktop's VM on macOS/Windows has no
  USB passthrough or LAN multicast.
- **One `mbregistry` per host**, regardless of whether it's native or
  containerized — the native service and the container are mutually
  exclusive on the same host (section 3); they cannot run side by side.
