---
id: '001'
title: Dockerfile, .dockerignore, and compose.yaml example
status: done
use-cases:
- SUC-001
depends-on: []
github-issue: ''
issue: docker-image-for-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Dockerfile, .dockerignore, and compose.yaml example

## Description

Add the container image definition itself: a `Dockerfile` that builds
and runs `mbtools` (specifically `mbregistry` as the long-running
daemon, plus the `mbdeploy`/`mbserial`/`mbrelay` client commands) from a
slim Python base image, a `.dockerignore` to keep the build context
small, and a `compose.yaml` example that documents the supported run
recipe (host networking, USB access, persistent volumes). This is the
foundational deliverable — tickets 002-004 build on it.

Per the stakeholder decisions already recorded in
`clasi/issues/docker-image-for-mbregistry.md` and carried into this
sprint's Architecture section:

- Base image: a slim Python image (`python:3.1x-slim` matching this
  project's supported Python range — check `requires-python` in
  `pyproject.toml`), Debian-based so `apt-get install libusb-1.0-0`
  (needed by `pyocd` for CMSIS-DAP v2 boards) is available.
- Install the project itself (`pip install .` or equivalent) so
  `mbregistry`, `mbdeploy`, `mbserial`, `mbrelay` console scripts are on
  `PATH`, exactly as `[project.scripts]` in `pyproject.toml` defines
  them.
- `ENTRYPOINT ["mbregistry"]`, `CMD ["run"]` — running the container
  with no args starts the daemon; other client commands can be invoked
  with `docker run --entrypoint mbdeploy ...` or `docker exec` against a
  running container.
- Container runs as root (matches `mbregistry.service`'s own privilege
  level — needed for raw USB/CMSIS-DAP access).
- `compose.yaml` example: `network_mode: host`; a `/dev` bind mount;
  `device_cgroup_rules` granting access to major 166 (`ttyACM*`) and
  major 189 (USB) plus `hidraw` (check `/proc/devices` on a real host
  for the current hidraw major — it's not fixed); volumes for
  `/var/lib/mbregistry` and `/run/mbregistry`; `privileged: true`
  included commented-out as the documented simple fallback.

## Acceptance Criteria

- [x] `Dockerfile` exists at the repo root (or a documented subdirectory
      consistent with where `.github/workflows/docker.yml` will expect
      it — coordinate path with ticket 002), builds `mbtools` from the
      repo source (not from PyPI, since this project isn't published
      there), and installs `libusb-1.0-0` (or whatever pyocd's CMSIS-DAP
      v2 backend actually needs on Debian slim — verify, don't assume).
- [x] `ENTRYPOINT` is `mbregistry`, default `CMD` is `run`.
- [x] `.dockerignore` excludes `.git`, `.venv`, `__pycache__`,
      `clasi/`, `docs/`, test artifacts, and anything else not needed
      in the build context.
- [x] `docker build` succeeds locally for the host's native architecture
      (arm64 on Apple Silicon dev machines, amd64/arm64 via
      `docker buildx build --platform linux/amd64,linux/arm64` if
      buildx is available locally — multi-arch CI build is ticket
      002's concern, but this ticket's Dockerfile must not contain
      anything arch-specific that would block it).
- [x] `docker run --rm <image> --version` and
      `docker run --rm --entrypoint mbdeploy <image> --help` both
      succeed against the locally built image.
- [x] `compose.yaml` at the repo root includes: `network_mode: host`,
      a `/dev:/dev` bind mount, `device_cgroup_rules` for ttyACM (166)
      and USB (189) majors plus hidraw, volumes for
      `/var/lib/mbregistry` and `/run/mbregistry`, and a commented-out
      `privileged: true` line with a one-line comment explaining it's
      the simple fallback if the cgroup-rules approach doesn't cover a
      given host's device majors.
- [x] No change to `src/mbtools/**` or any existing install path
      (venv, `.deb`) — this ticket only adds new packaging files.

## Implementation Plan

**Approach**: Write the Dockerfile using a single-stage build (no
compelling reason for multi-stage here — the image doesn't need to
exclude build tooling for size in a way that justifies the extra
complexity; revisit only if image size becomes a real problem).
Install system deps (`libusb-1.0-0`, and anything else pyocd's install
docs list for Debian) via `apt-get`, then `pip install .` from the
copied repo source. Verify against a real dev-machine `docker build` and
`docker run --version` before considering this done — don't just eyeball
the Dockerfile.

**Files to create**:
- `Dockerfile`
- `.dockerignore`
- `compose.yaml`

**Files to modify**: none expected.

**Testing plan**: No new automated/unit tests (this is packaging, not
Python code) — validate manually with `docker build` and
`docker run --rm <image> --version` / `--help` for each of the four
console scripts, on whichever architecture is available locally. Run
the project's existing test suite (`uv run pytest`) only if any
non-Docker file is touched incidentally (not expected).

**Documentation updates**: none in this ticket — `docs/docker.md`
(ticket 003) documents the run recipe and mechanics; this ticket's own
`compose.yaml` comments are the only in-file documentation needed here.
