---
id: 009
title: Docker image for mbregistry
status: executing
branch: sprint/009-docker-image-for-mbregistry
use-cases:
- SUC-001
- SUC-002
issues:
- docker-image-for-mbregistry.md
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Sprint 009: Docker image for mbregistry

## Goals

Ship `mbtools` (specifically `mbregistry`) as a Docker image, built and
published from GitHub, so a Linux host can run the registry daemon (and
use the client commands) without a venv or the `.deb`. The container
must reach the host's micro:bits over USB and must take part in mDNS
peer discovery, exactly as the native install does.

## Problem

Today `mbregistry` is installed either from a venv or a `.deb`. Some
Linux hosts would rather run it as a container (simpler upgrade/rollback,
no host Python/venv management). There's no supported way to do that,
and no automated build/publish pipeline for an image.

## Solution

Per the stakeholder's 2026-09-28 decisions (see
`clasi/issues/docker-image-for-mbregistry.md`, which this sprint links):

- Publish `ghcr.io/league-microbit/mbtools`, multi-arch
  (`linux/amd64` + `linux/arm64`), tagged `:<version>` and `:latest` on
  a `v*` tag — same trigger as `release-deb.yml`. `workflow_dispatch`/PR
  builds build without pushing.
- `--network host` only. Gives mDNS multicast, the real host IP/hostname
  in the peering advertisement, and ports 7440/7442/7443/7444 without
  `-p` mappings.
- USB: bind-mount `/dev` plus device-cgroup rules for the relevant
  majors (ttyACM, USB, hidraw), with `--privileged` documented as the
  simple fallback. Container runs as root, same as
  `mbregistry.service`.
- Bind-mount `/var/lib/mbregistry` and `/run/mbregistry` so host-side
  clients and a restarted container keep working across restarts.
- Deliverables: `Dockerfile` + `.dockerignore`, a `compose.yaml`
  example, `.github/workflows/docker.yml` (buildx multi-arch, GHCR
  push, tag-matches-version check), `docs/docker.md`, and a hardware
  acceptance pass on one Nolanet node (native service stopped first).

## Success Criteria

- Image builds multi-arch in CI and pushes to GHCR on a `v*` tag;
  `workflow_dispatch`/PR builds run without pushing.
- Running the image with the documented `--network host` + USB + volume
  recipe on a Nolanet node: the node's own board appears in its local
  registry and on peers, flash and serial work through it, and a
  USB replug is picked up (not just the start-time snapshot).
- `docs/docker.md` documents the run recipe, the USB/mDNS mechanics, and
  the Linux-only limitation, including stopping a native
  `mbregistry.service` first (both want the same ports/serial devices).

## Scope

### In Scope

- `Dockerfile` and `.dockerignore` (slim Python base, package + deps
  including pyocd/libusb, entrypoint `mbregistry`, default command
  `run`).
- `compose.yaml` example with host-network + USB + volume settings.
- `.github/workflows/docker.yml`: buildx multi-arch build, GHCR push on
  `v*` tags with the tag-matches-version check ported from
  `release-deb.yml`, build-only on `workflow_dispatch`/PR.
- `docs/docker.md`: USB/mDNS-in-container mechanics, run recipe,
  limitations.
- Hardware acceptance on one Nolanet node (e.g. `hodr`): native service
  stopped, image run in its place, board visible locally and to peers,
  flash/serial verified, replug verified.

### Out of Scope

- **macOS and Windows container support.** Docker Desktop on those
  platforms runs containers in a VM with no USB passthrough and no LAN
  multicast — `braeburn`, `gala`, and any Windows host keep the native
  install. This sprint documents that limitation; it does not attempt
  to work around it.
- **Bridge networking.** Not supported: multicast doesn't cross the
  bridge, and the daemon would advertise the container's private IP
  instead of the host's. `--network host` is the only supported mode.
- Any change to the native venv/`.deb` install paths — this sprint adds
  a new distribution channel, it doesn't change the existing ones.

## Test Strategy

Unit/integration coverage for anything genuinely new in the packaging
(e.g. the tag-matches-version CI check, ported from `release-deb.yml`'s
existing pattern) runs in the normal test suite. The core validation is
the hardware acceptance pass: running the built image on a real Nolanet
node against a real micro:bit, confirming USB and mDNS peering work
through the container exactly as they do for the native daemon.

## Architecture

**Compact** — adds one new distribution/packaging module (a Docker image
build, published from GitHub Actions) alongside the existing venv/`.deb`
install paths. No new cross-module dependency inside `mbtools`' own
Python code, no dependency-direction change, and no data-model change —
`mbregistry`, `mbdeploy`, `mbserial`, and `mbrelay` run inside the
container exactly as built today; only how the process is packaged and
launched changes. The full Architecture structure is written below per
the compact variant (no diagrams — a single additive packaging channel
with no new composition between existing modules has nothing a diagram
would clarify beyond the purpose statement below).

### What Changed

One new module: **Docker packaging** (`Dockerfile`, `.dockerignore`,
`compose.yaml`, `.github/workflows/docker.yml`, `docs/docker.md`). Its
purpose: build and publish a container image that runs the existing
`mbregistry` daemon (and `mbdeploy`/`mbserial`/`mbrelay` clients) on a
Linux host, as a third distribution channel next to the venv and the
`.deb`. Boundary: it packages and launches the existing
`src/mbtools` package unmodified — it owns the container image
definition, the CI workflow that builds/publishes it, and the operational
docs for running it; it does not own or change anything inside
`mbtools.registry`, `mbtools.deploy`, `mbtools.serial`, or
`mbtools.relay`. Serves SUC-001 and SUC-002 below.

### Why

Some Linux hosts (the Nolanet Pis, `torture`) would rather pull and run a
versioned container than manage a host Python venv or a `.deb` install/
upgrade cycle. GHCR + GitHub Actions gives a build/publish pipeline
comparable to the existing `.deb` release flow (`release-deb.yml`),
reusing its tag-matches-`pyproject.toml`-version check.

### Impact on Existing Components

None — additive. The container runs the unmodified project; the venv and
`.deb` install paths are untouched. The daemon inside the container binds
the same `/var/lib/mbregistry` and `/run/mbregistry` paths a native
install uses (bind-mounted from the host), so `mbregistry.service` and
the container image are mutually exclusive on one host (documented in
`docs/docker.md`) but neither one's code path changes because the other
exists.

### Migration Concerns

None — this adds a new, optional distribution channel alongside the
existing venv/`.deb` install; it changes nothing about how those work.
Operators choosing the container must stop a native `mbregistry.service`
first (port/serial-device contention, not a code migration).

### Design Rationale

The substantive decisions (GHCR as registry, `--network host` over
bridge, the `/dev` bind-mount + device-cgroup-rules USB recipe over
`--privileged`, bind-mounting `/var/lib/mbregistry` + `/run/mbregistry`)
were already made by the stakeholder and recorded in
`clasi/issues/docker-image-for-mbregistry.md`; they carry forward as-is
and are not re-litigated here. One addition at this tier: the CI
build-and-push trigger reuses `release-deb.yml`'s existing `v*`-tag +
tag-matches-version pattern rather than inventing a new one, so the two
release pipelines (image and `.deb`) stay triggered in lockstep from the
same tag.

### Open Questions

None outstanding — the stakeholder's 2026-09-28 decisions in the linked
issue resolve registry choice, networking mode, USB access strategy, and
host-state persistence. macOS/Windows container support is explicitly
out of scope (Docker Desktop's VM has no USB passthrough or LAN
multicast), not an open question.

## Use Cases

Compact sprint — brief use cases, not full narrative treatment.

**SUC-001: Operator runs `mbregistry` from a container on a Linux host.**
An operator on a Linux host (e.g. a Nolanet node) stops the native
`mbregistry.service`, then runs the published container with
`--network host`, a `/dev` bind mount plus device-cgroup rules (or
`--privileged`), and volumes for `/var/lib/mbregistry` and
`/run/mbregistry`. The containerized daemon discovers the host's
attached micro:bit(s) over USB, participates in mDNS peer discovery and
the 7440/7442/7443/7444 ports exactly as the native daemon does, and
host-side client commands (`mbregistry`, `mbdeploy`, `mbserial`) keep
working against the bind-mounted socket.

**SUC-002: CI builds and publishes a multi-arch image on release.**
On a pushed `v*` tag, GitHub Actions builds `linux/amd64` and
`linux/arm64` images (after checking the tag matches
`pyproject.toml`'s version, mirroring `release-deb.yml`), pushes
`ghcr.io/league-microbit/mbtools:<version>` and `:latest`, and links the
image from the release notes. A `workflow_dispatch` run or a PR touching
the Dockerfile/workflow builds both architectures without pushing, as a
build-correctness check.

## GitHub Issues

(GitHub issues linked to this sprint's tickets. Format: `owner/repo#N`.)

## Definition of Ready

Before tickets can be created, all of the following must be true:

- [ ] Sprint planning document is complete (sprint.md, including its
      Architecture and Use Cases sections)
- [ ] Architecture review passed (or skipped, for changes with no
      architectural impact)
- [ ] Stakeholder has approved the sprint plan

## Tickets

| # | Title | Depends On |
|---|-------|------------|
| 001 | Dockerfile, .dockerignore, and compose.yaml example | — |
| 002 | GitHub Actions workflow: multi-arch build and GHCR publish | 001 |
| 003 | docs/docker.md and README pointer | 001, 002 |
| 004 | Hardware acceptance: run the container on hodr | 001, 002, 003 |

Tickets execute serially in the order listed.
