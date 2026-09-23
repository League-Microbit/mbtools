---
id: '006'
title: 'deploy.release: GitHub latest-release hex fetch, tag pin, asset selection,
  cache'
status: done
use-cases:
- SUC-002
depends-on: []
github-issue: ''
issue: mbdeploy-install-latest-release-hex-from-a-github-repo.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# deploy.release: GitHub latest-release hex fetch, tag pin, asset selection, cache

## Description

Create `mbtools.deploy.release`, turning a `--repo OWNER/REPO[@TAG]`
reference into a local hex file path. This is new — no existing code to
port, per the issue's own text ("Details learned from the real release
pages").

Per sprint.md's Architecture and the issue's scope:

- Parse `--repo`'s value: `OWNER/REPO` (no tag) or `OWNER/REPO@TAG`.
- No `@tag`: call GitHub's `GET /repos/{repo}/releases/latest` — the
  release GitHub itself flags "Latest", **never** a tag literally named
  `latest` (the issue's own counter-example:
  `League-Robotics/microbit-radio-relay` has a moving `latest`-tagged
  release that is *not* its GitHub "Latest" release).
- With `@tag`: call `GET /repos/{repo}/releases/tags/{tag}`.
- Asset selection: prefer an asset named `MICROBIT.hex`; if absent, take
  the single `*.hex` asset; if more than one `*.hex` and none is
  `MICROBIT.hex`, error rather than guess. `--asset NAME` overrides the
  automatic choice entirely.
- Download the selected asset once, cache under
  `~/.cache/mbtools/hex/<owner>/<repo>/<tag>/<asset-name>`; a later call
  for the same `repo@tag` reuses the cached file without a network call.
- Print which release (tag) and asset were selected, so `mbdeploy
  deploy --repo ...`'s output tells the user what was actually flashed.
- Use the public GitHub API anonymously by default; an optional
  `GITHUB_TOKEN` environment variable is sent as a bearer token for
  rate-limit headroom. Use the standard library's `urllib.request` (no
  new dependency — see sprint.md's Design Rationale).

Per sprint.md's Migration Concerns forward note: this module returns
both a local file path and the raw downloaded bytes from the same call,
so sprint 003's remote flash (issue text: "download on the client, then
send the bytes to the remote registry") has something to build on
without a rewrite — but wiring that up is explicitly out of scope here.

Independent of every other ticket in this sprint except its eventual
caller (ticket 007) — a standalone GitHub-fetching module with no
registry or flash dependency.

## Acceptance Criteria

- [x] `OWNER/REPO` (no `@tag`) resolves via `releases/latest`, never a
      tag literally named `latest`.
- [x] `OWNER/REPO@TAG` resolves via `releases/tags/<TAG>`.
- [x] Asset selection prefers `MICROBIT.hex`; falls back to a single
      other `*.hex` asset; errors (does not guess) when more than one
      `*.hex` exists and none is `MICROBIT.hex`; `--asset NAME` overrides
      the automatic choice and is used verbatim.
- [x] A downloaded asset is cached under `~/.cache/mbtools/hex/<owner>/
      <repo>/<tag>/`; a second call for the same `repo@tag` makes no
      network request and returns the cached path.
- [x] The selected release tag and asset name are surfaced to the caller
      (return value, not just a printed line) so `mbdeploy deploy`
      (ticket 007) can report them to the user.
- [x] `GITHUB_TOKEN`, when set in the environment, is sent as a bearer
      auth header; its absence does not prevent anonymous access working
      for a public repo.
- [x] A repo/tag not found, or a rate-limit response, raises a clear,
      distinct error — never silently falls through to "no hex file
      found" or a generic exception a caller can't act on.
- [x] No new runtime dependency is added to `pyproject.toml` — uses
      `urllib.request`.

## Testing

- **Existing tests to run**: none (new module, no shared code touched).
- **New tests to write**: an injectable HTTP fetcher (same seam shape as
  ticket 005's injectable pyOCD runner) scripts `releases/latest`,
  `releases/tags/<tag>`, and asset-list responses — table-driven over
  MICROBIT.hex-present, single-other-hex, ambiguous-multiple-hex, and
  `--asset`-override cases; a repo-not-found and a rate-limit-response
  case; a cache-hit test asserting a second call for the same `repo@tag`
  triggers zero HTTP calls; a `GITHUB_TOKEN`-present test asserting the
  auth header is sent.
- **Verification command**: `uv run pytest tests/deploy/`
