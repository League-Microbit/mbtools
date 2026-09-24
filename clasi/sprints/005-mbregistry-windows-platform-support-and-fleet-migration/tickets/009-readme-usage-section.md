---
id: 009
title: README usage section
status: in-progress
use-cases:
- SUC-005
depends-on: []
github-issue: ''
issue: mbtools-fleet-deployment-tooling-and-migration-docs.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# README usage section

## Description

`mbtools` currently has no user-facing documentation beyond
`docs/design/` and `docs/brief.md` — the README stops at a program table
and a stale "Status: early planning... Only `mbregistry` is under active
development this sprint" note left over from before sprint 001 finished.
Add a concise usage section and correct the stale status text, so a new
user can get all four programs running from the README alone (SUC-005).
**No machine specifics** — this repo is public; install/usage guidance
only, no hostnames/IPs/per-node paths (the same constraint CLAUDE.md
places on `docs/wiki/` in the sibling `mbdeploy` project, restated here
for this repo).

**Approach**
- Replace the stale "Status: early planning" paragraph — all four
  programs have real implementations as of sprint 004.
- **Install**: `uv sync` (already documented) plus a one-line note that
  `mbregistry` needs `install-service` (Linux) once, as root, before
  `run`ning it as a real daemon — point at `mbregistry install-service
  --help`/its own printed instructions rather than duplicating them.
- **The four commands**, each with its one most common invocation:
  - `mbregistry list [--json]`
  - `mbdeploy deploy <name> --repo OWNER/REPO` (and `--hex FILE` as the
    local-file alternative)
  - `mbserial <name>`
  - `mbrelay connect <robot>[@host]`
- **Common flows**: a short "peering/remote" paragraph showing `@host`
  syntax reaching a remote host's registry (used identically by
  `mbdeploy`/`mbserial`/`mbrelay`), and `mbregistry run --peer
  HOST[:PORT]` for explicit cross-network peering (spec §3.9).
- **Ports**: a short table or list of the always-on ports
  (`mbregistry`'s local API, remote API 7440, peering PUB/REP
  7442/7443, robot-console-compatibility pool 7444/names 7445) — enough
  for someone reading this to understand what's listening, without any
  host-specific detail.
- Keep the existing program table and "Development" section; this
  ticket adds a new section between them, doesn't replace them.

**Files to create/modify**
- `README.md`.

**Documentation updates**: this ticket *is* the documentation update.

## Acceptance Criteria

- [x] The stale "Status: early planning... Only `mbregistry` is under
      active development" text is removed/corrected.
- [x] A new "Usage" (or similarly named) section documents install and
      the four programs' most common invocation each, matching the
      actual current CLI (`--repo`/`--hex` on `mbdeploy deploy`,
      `<robot>[@host]` on `mbrelay connect`, `--peer` on `mbregistry
      run`) — verified against `build_parser()` in each program's
      `cli.py`, not guessed.
- [x] The peering/`--peer`/`@host` behavior is explained in at least one
      sentence, since it's the one piece of behavior a new user
      wouldn't discover from `--help` alone (it spans multiple
      programs).
- [x] The listening-ports list matches the actual current defaults in
      `registry/cli.py`/`registry/console_compat/relay_pool.py` (7440,
      7442, 7443, 7444, 7445) — no invented or stale port numbers.
- [x] No hostname, IP address, or per-node filesystem path appears
      anywhere in the new section (public-repo constraint).
- [x] The existing program table and "Development" section remain
      intact.

## Testing

- **Existing tests to run**: none — documentation-only ticket.
- **New tests to write**: none.
- **Verification command**: N/A — manually run each documented example
  command locally (`uv run mbregistry list`, etc.) against a
  dev-machine-safe target (per CLAUDE.md, never `mbdeploy deploy`/
  `debug` against the dev Mac itself) to confirm the README's exact
  invocations work as written, not just look plausible.

## Implementation Notes

- Pull the exact flag names from each program's `build_parser()`
  (`src/mbtools/deploy/cli.py`, `src/mbtools/serial/cli.py`,
  `src/mbtools/relay/cli.py`, `src/mbtools/registry/cli.py`) rather than
  from memory or from this ticket's own prose — CLI surfaces can drift
  from planning docs.
