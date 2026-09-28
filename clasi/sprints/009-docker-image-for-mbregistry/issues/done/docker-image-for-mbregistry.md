---
status: done
sprint: 009
tickets:
- 009-001
- 009-002
- 009-003
- 009-004
---

# Docker image for mbtools, built and published from GitHub

## Description

Ship mbtools as a Docker image so a Linux host can run `mbregistry` (and
use the client commands) without a venv or the `.deb`. Build it in
GitHub Actions and publish it on each release.

Stakeholder request (2026-09-28): generate, build and publish the image
from GitHub; the container must reach the host's micro:bits over USB and
must take part in mDNS peer discovery.

## Decisions already made (team-lead, 2026-09-28)

- **Registry**: GitHub Container Registry, `ghcr.io/league-microbit/mbtools`.
  Docker images can't usefully be GitHub release assets; the release
  notes link to the image instead. Push `:<version>` and `:latest` on a
  `v*` tag (the same trigger as `release-deb.yml`), multi-arch
  `linux/amd64` + `linux/arm64` (the Nolanet Pis are arm64). A
  `workflow_dispatch` / PR build must build without pushing.
- **Linux hosts only.** Docker Desktop on macOS/Windows runs containers
  in a VM with no USB passthrough and no LAN multicast, so `braeburn`,
  `gala` and Windows hosts keep the native install. Document this.
- **Networking**: `--network host`. That gives mDNS multicast on the
  LAN, the real host IP in the advertisement, the real hostname (the
  multi-homed self-filter in `peering.py` compares hostnames), and
  ports 7440/7442/7443/7444 without `-p` mappings. Bridge networking is
  not supported (multicast doesn't cross the bridge, and the daemon
  would advertise the container's private IP).
- **USB**: the daemon polls pyserial `comports()`, which on Linux reads
  `/sys` (vid/pid/serial) and opens `/dev/ttyACM*`; pyOCD flashing
  needs raw CMSIS-DAP access (`/dev/bus/usb/*` via libusb for v2
  boards, `/dev/hidraw*` for v1). `--device /dev/ttyACM0` alone is a
  start-time snapshot and breaks on replug/re-enumeration, so the
  supported recipe bind-mounts `/dev` and grants device-cgroup rules
  for the relevant majors (ttyACM 166, USB 189, hidraw — dynamic, check
  `/proc/devices`), with `--privileged` documented as the simple
  fallback. Container runs as root (as `mbregistry.service` does).
- **Host state**: bind-mount a volume for `/var/lib/mbregistry`
  (devices.db) and `/run/mbregistry` (api.sock) so host-side clients
  and a restarted container keep working.

## Deliverables

- `Dockerfile` (+ `.dockerignore`) — slim Python base, installs the
  package with its deps (pyocd needs libusb), entrypoint `mbregistry`
  with default command `run`.
- `compose.yaml` example with the host-network + USB settings.
- `.github/workflows/docker.yml` — buildx multi-arch, push to GHCR on
  `v*` tags with the tag-matches-version check from `release-deb.yml`.
- `docs/docker.md` — how USB and mDNS work in the container, run
  recipe, limitations (Linux only; stop a native `mbregistry.service`
  first because both want the same ports and serial devices).
- Hardware acceptance: run the image on one Nolanet node (e.g. `hodr`,
  native service stopped) and confirm its board appears locally and on
  peers, flash and serial work through it, and replug is picked up.
