---
id: '004'
title: 'Hardware acceptance: run the container on hodr'
status: done
use-cases:
- SUC-001
depends-on:
- '001'
- '002'
- '003'
github-issue: ''
issue: docker-image-for-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: run the container on hodr

## Description

Validate the whole Docker distribution channel against real hardware on
`hodr` (a Nolanet node, per project CLAUDE.md's hardware-testing table:
Debian 13, aarch64, one micro:bit dedicated to mbtools testing at
`/dev/ttyACM0`). This is the sprint's core validation per its Test
Strategy — unit/integration coverage doesn't exercise USB or mDNS, so
this hardware pass is the only real check that the container actually
reaches the board and takes part in peering the way the native daemon
does.

`hodr` may not have Docker installed yet; installing it is in scope for
this ticket (passwordless sudo, internet access available per CLAUDE.md).

Steps:

1. On `hodr`: install Docker if not already present
   (`docker --version` to check first).
2. Build the image on `hodr` (arm64, native — no QEMU needed) from this
   sprint's `Dockerfile`, or pull it from GHCR if ticket 002's pipeline
   has already published a tag reachable at this point in the sprint;
   note in Implementation Notes which path was used.
3. Stop and disable the native `mbregistry.service` on `hodr`
   (`sudo systemctl stop mbregistry`), per `docs/docker.md`'s documented
   prerequisite.
4. Run the container per `docs/docker.md`'s documented recipe
   (`--network host`, `/dev` bind mount + device-cgroup-rules, the two
   volumes).
5. Verify:
   - `hodr`'s own attached board shows up in `hodr`'s own
     `mbregistry list` (inside or via the bind-mounted socket from the
     host) as `host=local`.
   - A peer host (e.g. `meili`) sees `hodr`'s board with
     `host=hodr` in its own `mbregistry list` — confirms mDNS peering
     and the peering ports (7440/7442/7443) work from inside the
     container under host networking.
   - Flash the board via `mbdeploy` from the peer host (`meili`) against
     `hodr`'s containerized daemon — confirms remote flash works
     end-to-end through the container.
   - Serial: open a serial session (`mbserial`) against the
     containerized board, locally and/or from a peer, confirms the
     serial data path works.
   - Replug/reset detection: physically unplug and replug (or reset)
     the board while the container is running, confirm the daemon
     picks up the detach/reattach — this is the specific thing a
     static `--device` flag would *not* survive, so it's the key
     regression check for the bind-mount + cgroup-rules recipe over the
     simpler-looking alternative.
6. Restore `hodr` to its native `mbregistry.service` install afterwards
   (stop/remove the container, re-enable and start the native service)
   so the host is left in its normal dedicated-test-host state for
   other sprints.
7. Write `docs/acceptance/009-hardware.md` recording what was run, what
   passed, and any quirks found (following the style of the existing
   `docs/acceptance/00N-hardware.md` files referenced throughout project
   CLAUDE.md).

## Acceptance Criteria

- [x] Docker installed on `hodr` (or confirmed already present).
- [x] Image built or pulled successfully on `hodr` (arm64).
- [x] Native `mbregistry.service` stopped before the container test,
      confirmed restored and running again afterward.
- [x] Container run using exactly the recipe documented in
      `docs/docker.md` (not an ad hoc variant) — if the documented
      recipe turns out to need a correction to actually work, fix
      `docs/docker.md` (and `compose.yaml` if the bug is there) as part
      of this ticket and note the correction in Implementation Notes.
- [x] `hodr`'s board shows `host=local` in `hodr`'s own registry view
      while containerized.
- [x] A peer (e.g. `meili`) shows `hodr`'s board with `host=hodr`.
- [x] Remote flash via `mbdeploy` from a peer succeeds against the
      containerized daemon.
- [x] Serial session via `mbserial` succeeds against the containerized
      daemon.
- [x] A physical replug/reset is correctly detected by the containerized
      daemon (not just the start-time snapshot).
- [x] `docs/acceptance/009-hardware.md` written, documenting each
      scenario above and its result, following the existing
      `docs/acceptance/*.md` convention.
- [x] `hodr` restored to native-service state at the end of the session.

## Implementation Plan

**Approach**: Work over SSH to `hodr` (`ssh hodr`, user `eric`,
passwordless sudo) per project CLAUDE.md's hardware-testing rules. Treat
this as a real acceptance pass, not a smoke test — follow each step in
the Description in order, and if any step fails, treat it as a real bug
to fix in tickets 001-003's artifacts (not just a note), then re-run
from that step. This mirrors how prior sprints' hardware tickets found
and fixed real bugs during the acceptance pass itself (see e.g. sprint
005 ticket 011's and ticket 010's entries in project CLAUDE.md).

**Files to create**:
- `docs/acceptance/009-hardware.md`

**Files to modify**: potentially `docs/docker.md` and/or `compose.yaml`
from tickets 001/003, only if the hardware pass finds a real correction
needed in the documented recipe.

**Testing plan**: This ticket *is* the test — no separate automated
test suite applies to a live hardware/network scenario. Run the
project's existing `uv run pytest` suite only if any Python source is
touched (not expected; this ticket shouldn't need to touch
`src/mbtools`).

**Documentation updates**: `docs/acceptance/009-hardware.md` (new), plus
corrections to `docs/docker.md`/`compose.yaml` if the pass surfaces any.

## Implementation Notes

Full run log and evidence: `docs/acceptance/009-hardware.md`. Summary:

- Docker was already installed on `hodr` (29.4.3) — no install needed.
- GHCR has not published an image yet this sprint (no `v*` tag pushed;
  `gh api .../packages/container/mbtools/versions` 404s), so the image
  was built locally on `hodr` from an `rsync`'d copy of the repo tree,
  per the ticket's own documented fallback.
- **Real finding, not a code/docs bug**: `hodr`'s SD-card root
  partition was already at 0 bytes free before this session touched
  anything (normal Debian aarch64 footprint plus an unrelated
  pre-existing monitoring stack — not `mbtools`). The first build
  attempt failed in the `apt-get install` layer for exactly this
  reason. Fixed by clearing two safe-to-regenerate local caches
  (`docker builder prune -af`, `rm -rf /var/lib/apt/lists/*`) before
  rebuilding — no change to `Dockerfile`/`compose.yaml`/`docs/docker.md`
  was needed, the documented recipe worked exactly as written once
  there was room to build. Documented under "Disk space on `hodr`" in
  the acceptance doc for future sessions building there.
- All six verification scenarios (a-f in the ticket's Description,
  including remote flash via `mbdeploy` from `meili` using the radio
  relay firmware, and a real USB-level unbind/bind substitute for a
  physical replug) passed against real hardware — see the acceptance
  doc for full command/output evidence.
- `hodr` restored to its native `mbregistry.service` (`active`/
  `enabled`, board `free`/`local`, matching pre-session state); the
  built image was removed and the apt-lists cache regenerated, leaving
  the host with *more* free disk than it had at session start. Docker
  itself left installed per the ticket's instruction.
