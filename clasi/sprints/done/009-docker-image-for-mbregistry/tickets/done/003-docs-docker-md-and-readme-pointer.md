---
id: '003'
title: docs/docker.md and README pointer
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '001'
- '002'
github-issue: ''
issue: docker-image-for-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# docs/docker.md and README pointer

## Description

Write `docs/docker.md` documenting the container distribution channel
end to end, and add a short pointer to it from the project README.
Depends on tickets 001 and 002 so the documented recipe and image
reference (`ghcr.io/league-microbit/mbtools:<tag>`) match what actually
ships, rather than being written speculatively ahead of them.

Content to cover (per the sprint's Success Criteria and the issue's
`docs/docker.md` deliverable):

- **What this is and who it's for**: an alternative to the venv/`.deb`
  install for Linux hosts; explicitly Linux-only (link to the
  Out-of-Scope rationale — Docker Desktop's VM on macOS/Windows has no
  USB passthrough or LAN multicast).
- **Prerequisite**: stop a native `mbregistry.service` first if one is
  running on the target host — both want the same ports and serial
  devices (`sudo systemctl stop mbregistry`, `sudo systemctl disable
  mbregistry` if the switch is permanent).
- **Run recipe**: the `docker run` / `compose.yaml` invocation from
  ticket 001, with `--network host`, the `/dev` bind mount + device-
  cgroup-rules (with the `--privileged` fallback called out), and the
  two persistent volumes.
- **How USB works in the container**: pyserial's `comports()` reads
  `/sys` and opens `/dev/ttyACM*`; pyOCD needs raw CMSIS-DAP access
  (`/dev/bus/usb/*` via libusb for v2 boards, `/dev/hidraw*` for v1) —
  explain why a static `--device` flag doesn't survive a replug and why
  the bind-mount + cgroup-rules recipe is used instead.
- **How mDNS/peering works in the container**: why `--network host` is
  required (multicast doesn't cross a bridge; the daemon would
  otherwise advertise the container's private IP/hostname instead of
  the host's real one) and that this makes the container indistinguishable
  from a native install to other peers on the LAN.
- **How host-side clients reach the containerized daemon**: the
  `/run/mbregistry` bind mount means `mbregistry list` etc. run
  directly on the host (outside the container) work unmodified against
  the same socket.
- **Limitations**: Linux-only (link the reasoning above); one daemon
  per host regardless of native-vs-container (can't run both
  simultaneously — port/device contention).

## Acceptance Criteria

- [x] `docs/docker.md` exists and covers every bullet in the Description
      above.
- [x] The documented run recipe matches `compose.yaml` from ticket 001
      exactly (no drift between the example file and the prose — if
      ticket 001 changed after this ticket started, re-check before
      marking done).
- [x] The documented image reference matches ticket 002's actual GHCR
      path and tag scheme (`ghcr.io/league-microbit/mbtools:<version>`,
      `:latest`).
- [x] README gets a short pointer to `docs/docker.md` (one or two
      sentences plus a link) in whatever section already lists
      install/distribution options — do not restructure the README
      beyond that.
- [x] No change to any other existing doc's meaning (this is additive
      documentation for a new channel, consistent with the sprint's
      "no migration concerns" framing).

## Implementation Plan

**Approach**: Write `docs/docker.md` after tickets 001 and 002 are
functionally complete (their branches/commits merged into this sprint's
work), so every command and path in it can be copy-pasted and verified
rather than guessed. Cross-check every claim about *why* the recipe
looks the way it does (USB majors, host networking) against
`clasi/issues/docker-image-for-mbregistry.md`'s "Decisions already
made" section and this sprint's Architecture section, rather than
re-deriving the rationale from scratch.

**Files to create**:
- `docs/docker.md`

**Files to modify**:
- `README.md` (add the pointer)

**Testing plan**: No automated tests apply to documentation. Manually
walk through every command in `docs/docker.md` against a locally built
image (from ticket 001) to confirm it's accurate before marking this
ticket done — this is the closest thing to a test this ticket has,
short of ticket 004's real hardware pass.

**Documentation updates**: this ticket *is* the documentation update.
