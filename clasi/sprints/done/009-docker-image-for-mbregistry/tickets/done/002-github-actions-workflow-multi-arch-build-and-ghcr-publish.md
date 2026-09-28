---
id: '002'
title: 'GitHub Actions workflow: multi-arch build and GHCR publish'
status: done
use-cases:
- SUC-002
depends-on:
- '001'
github-issue: ''
issue: docker-image-for-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# GitHub Actions workflow: multi-arch build and GHCR publish

## Description

Add `.github/workflows/docker.yml`: a CI pipeline that builds the image
from ticket 001's `Dockerfile` for `linux/amd64` and `linux/arm64`,
pushes it to `ghcr.io/league-microbit/mbtools` tagged `:<version>` and
`:latest` on a pushed `v*` tag, and builds (without pushing) on
`workflow_dispatch` or on a PR that touches the Dockerfile or this
workflow. Mirror `release-deb.yml`'s existing pattern where it applies
(same `v*`-tag trigger, same tag-matches-`pyproject.toml`-version check)
rather than inventing a new convention — the sprint's Architecture
section calls this out explicitly as the reused pattern.

Build strategy: prefer a native-arm GitHub runner matrix (as
`release-deb.yml` does with `ubuntu-24.04-arm`) over QEMU emulation if
one is available for Docker builds in this org's GitHub plan — QEMU
emulated arm64 builds are much slower and flakier. Fall back to
`docker/setup-qemu-action` + `docker/setup-buildx-action` cross-build if
a native arm64 Docker-capable runner isn't available. Whichever is
chosen, note the reasoning in the workflow's own header comment (see
`release-deb.yml`'s header comment for the style to match).

## Acceptance Criteria

- [x] `.github/workflows/docker.yml` triggers on `push: tags: ["v*"]`,
      `workflow_dispatch`, and `pull_request` (paths filtered to the
      Dockerfile/`.dockerignore`/`compose.yaml`/this workflow file).
- [x] On a `v*` tag push, the tag-matches-version check runs first
      (ported from `release-deb.yml`'s "Check the tag matches the
      package version" step) and fails the build with a clear
      `::error::` message on mismatch, before any image build starts.
- [x] Builds both `linux/amd64` and `linux/arm64`.
- [x] On a `v*` tag push only: pushes
      `ghcr.io/league-microbit/mbtools:<version>` and
      `ghcr.io/league-microbit/mbtools:latest`.
- [x] On `workflow_dispatch` or a matching PR: builds both
      architectures but does not push (`docker buildx build` without
      `--push`, or `push: false` in the build-push-action).
- [x] Workflow has `permissions: packages: write` (and `contents: read`
      as needed) scoped to the job that pushes — not a blanket
      workflow-level grant beyond what's needed.
- [x] Pushed image carries OCI labels, at minimum
      `org.opencontainers.image.source` pointing at this repo (so the
      GHCR package page links back to it) and
      `org.opencontainers.image.version`.
- [ ] A `workflow_dispatch` run (triggered manually after this ticket
      lands) completes successfully end to end for both architectures.
      **Not verified in this session** — see Implementation Notes.
- [x] `release-deb.yml`'s release-notes body gets one added line
      pointing at the GHCR image (optional per the sprint plan, but
      include it if it's a small, low-risk addition — skip with a note
      in Implementation Notes if it turns out to complicate that
      workflow's existing `--notes` heredoc).

## Implementation Plan

**Approach**: Start from `release-deb.yml` as the template for the
tag-matches-version check and the overall job/step shape. Use
`docker/setup-buildx-action` and `docker/login-action` (against
`ghcr.io`, using `${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}`)
plus `docker/build-push-action` for the actual build/push, since that's
the standard, well-maintained action for this rather than hand-rolling
`docker buildx build` shell steps — prefer it unless there's a concrete
reason not to (note the reason in Implementation Notes if so). Decide
native-arm-runner vs. QEMU per the Description above and document the
choice in the workflow's header comment block, matching
`release-deb.yml`'s existing header-comment convention.

**Files to create**:
- `.github/workflows/docker.yml`

**Files to modify**:
- `.github/workflows/release-deb.yml` (only the release-notes body, if
  that acceptance criterion is kept — a one-line addition, not a
  restructuring).

**Testing plan**: No Python unit tests apply to a GitHub Actions
workflow file. Validate via a real `workflow_dispatch` run in this
repo's Actions tab (build-only path) before marking this ticket done,
plus a syntax/lint pass (`actionlint` if available, otherwise careful
manual review) since a workflow YAML error only surfaces at runtime.
Full tag-triggered push path can't be exercised without actually tagging
a release — leave that for the real next `v*` tag after this sprint
closes, and say so explicitly in Implementation Notes so it isn't
mistaken for having been verified.

**Documentation updates**: none directly (docs/docker.md is ticket 003)
beyond the workflow's own header comment.

## Implementation Notes

**Build strategy chosen**: native-arm runner matrix (`ubuntu-24.04` /
`ubuntu-24.04-arm`), no QEMU — same as `release-deb.yml`. Each matrix
leg only ever builds its own runner's native platform, so
`docker/setup-buildx-action`'s default driver never needs to
cross-emulate. The two per-arch images can't be combined into one
multi-arch manifest by either runner alone (neither has the other
arch's layers), so on a `v*` tag push each leg pushes its image to
GHCR **by digest only** (`push-by-digest=true`, no tag), uploads that
digest as a build artifact, and a separate `push` job (`needs: build`,
tag-push only) downloads both digests and runs
`docker buildx imagetools create` to publish the final
`:<version>`/`:latest` manifest list referencing both digests. This is
the pattern from Docker's own "distribute build across multiple
runners" docs
(https://docs.docker.com/build/ci/github-actions/multi-platform/),
not a hand-rolled alternative. On `workflow_dispatch`/PR (no push),
each matrix leg just builds its own platform with `push: false` — no
merge step runs since there's no manifest to merge.

**Verified this session** (no branch/tag pushed, no remote workflow
run triggered, per explicit instruction from the dispatching agent):
- `actionlint` (local install, `/opt/homebrew/bin/actionlint`
  v1.7.12) passes clean on both `.github/workflows/docker.yml` and
  the modified `.github/workflows/release-deb.yml` (one initial
  shellcheck SC2046 warning on the intentional word-splitting in the
  `imagetools create` digest-glob line, silenced with a
  `# shellcheck disable=SC2046` comment plus a note explaining why the
  splitting is wanted, once the reason was confirmed against the same
  pattern in Docker's own docs).
- Both workflow files parse as valid YAML (`python3 -c
  "import yaml; yaml.safe_load(open(f))"` for each).
- Manual review of the job/step logic against every acceptance
  criterion above.

**Not verified — explicitly out of scope for this session**:
- A real `workflow_dispatch` run in GitHub Actions (build-only path,
  both architectures). The dispatching agent's task explicitly said
  not to push the branch or trigger any remote workflow run, so this
  is deferred to whoever pushes this branch — run
  `gh workflow run docker.yml` (or the Actions tab) after the branch
  is on GitHub, confirm both `build (amd64)` and `build (arm64)` go
  green, and check this box off then.
- The full tag-triggered push path (`push-by-digest` → GHCR →
  `imagetools create` → `:<version>`/`:latest` manifest list). Per the
  ticket's own testing plan, this can only be exercised by actually
  tagging a release, which is out of scope here — leave it for the
  real next `v*` tag after this sprint closes. If it doesn't work as
  designed, the most likely failure points (based on the documented
  Docker pattern this follows) are: the per-arch `GITHUB_TOKEN`
  permissions for `packages: write` not being honored by GHCR for a
  first-ever push of this image name (a one-time manual
  `docker login`/push, or a repo-level package-visibility setting,
  may be needed to create the package the first time), or the
  digest-artifact download/glob in the `push` job not matching if
  `actions/upload-artifact@v4`'s default compression alters the
  (empty, zero-byte) digest marker files' names — it doesn't (names
  are preserved), but this is the one part of the pattern not
  exercised end-to-end here.
- GHCR package visibility/first-push behavior in general (new package
  name, org permissions) — nothing in this repo controls that; it's a
  one-time GHCR/org setting to check when the first real push happens.

**Deviation from the ticket's suggested action versions**: none of
`docker/setup-buildx-action`, `docker/login-action`,
`docker/build-push-action`, or `docker/metadata-action` were
previously used anywhere in this repo, so there was no existing pin
to match; used `@v3`/`@v3`/`@v6`/`@v5` respectively (current major
versions as of this session).
