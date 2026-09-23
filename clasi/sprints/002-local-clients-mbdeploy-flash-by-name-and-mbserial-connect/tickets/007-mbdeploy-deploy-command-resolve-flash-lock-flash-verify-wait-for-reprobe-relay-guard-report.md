---
id: '007'
title: 'mbdeploy: deploy command (resolve, flash-lock, flash, verify, wait-for-reprobe,
  relay guard, report)'
status: open
use-cases: [SUC-001, SUC-002, SUC-005]
depends-on: ['001', '003', '005', '006']
github-issue: ''
issue:
- mbdeploy-flash-by-name-via-mbregistry.md
- mbdeploy-install-latest-release-hex-from-a-github-repo.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbdeploy: deploy command (resolve, flash-lock, flash, verify, wait-for-reprobe, relay guard, report)

## Description

Build `mbdeploy deploy <name> [--hex FILE | --repo OWNER/REPO[@TAG]
[--asset NAME]] [--force-relay]` in `mbtools.deploy.cli` (replacing the
sprint-001 stub), composing the modules built in tickets 001, 003, 005,
006. This is SUC-001/SUC-002's implementation ticket.

Flow (per sprint.md's Use Cases, SUC-001 and SUC-002):

1. Resolve `<name>` via `registry.client.find()` (ticket 001).
2. **Relay guard**: if the resolved device's role indicates a relay
   (contains `RELAY`/`BRIDGE`) and `--force-relay` was not given, refuse
   before taking any lock or touching the hex file at all.
3. Resolve the hex file: `--hex FILE` used directly, or `--repo
   OWNER/REPO[@TAG]` resolved via `deploy.release` (ticket 006) — these
   are mutually exclusive; exactly one (or neither, if there's a
   sensible default — decide and document) must be given.
4. Lock the device (`kind=flash`) via `registry.client.lock()`. On a
   `locked` failure, fail fast naming the holder's kind and PID
   (SUC-005) — no retry, no blocking wait.
5. Run `deploy.flash.flash_hex()` (ticket 005), streaming its `log`
   output to the terminal live.
6. Call `registry.client.mark_flashed(uid)` (ticket 003) so
   `flash_count` advances. Per sprint.md's Migration Concerns
   deployment-sequencing note: if this call fails with
   `invalid_request` (an old, sprint-001-vintage `mbregistry` with no
   `mark_flashed` op), log it as a non-fatal warning and continue — never
   fail an otherwise-successful flash over this.
7. Unlock the device (this release is what triggers the daemon's
   existing flash-pending re-probe — no separate "trigger re-probe" call
   needed).
8. Poll `registry.client.find()` for the re-probed announcement, up to a
   bounded timeout, and report it. If nothing new arrives within the
   timeout, report that plainly (never hang, never claim success it
   can't back up) — this is the client-side half of the magni finding;
   ticket 004's registry-side mitigation is what should make this poll
   usually succeed.
9. If `flash_hex()` reported the post-mass-erase blank-board case,
   surface that explicitly, distinctly from an ordinary flash failure.

Also implement the deployment-sequencing acceptance behavior from
sprint.md's Migration Concerns (`mark_flashed` against an old daemon is
non-fatal) as a first-class acceptance criterion of this ticket, not an
afterthought — it's the one place in this sprint where two different
sprints' daemon/client versions could legitimately be running against
each other on a real fleet node.

## Acceptance Criteria

- [ ] `mbdeploy deploy <name> --hex FILE` flashes a local device
      end-to-end: resolve, lock, flash (with ticket 005's retry/erase
      behavior intact), `mark_flashed`, unlock, wait-for-reprobe, report.
- [ ] `mbdeploy deploy <name> --repo OWNER/REPO[@TAG] [--asset NAME]`
      resolves the hex via `deploy.release` (ticket 006) before locking
      anything, then proceeds identically to the `--hex` path; the
      selected release tag and asset are printed.
- [ ] A relay target without `--force-relay` refuses before any lock is
      taken or any hex file is resolved/downloaded.
- [ ] An already-locked device fails fast with the holder's kind and PID
      (`EXIT_LOCKED`), no retry or blocking wait.
- [ ] A `mark_flashed` call that fails with `invalid_request` (old
      daemon) is logged as a warning and does not fail the command —
      verified by a test that fakes exactly that response.
- [ ] The wait-for-reprobe step has a bounded timeout; a board that
      never re-announces within it is reported plainly as "no new
      announcement arrived," not treated as success and not hung
      indefinitely.
- [ ] A post-mass-erase blank board (ticket 005's `flash_hex` outcome)
      is reported as a distinct, explicit failure mode — never folded
      into a generic "flash failed" message.
- [ ] `--hex` and `--repo` are mutually exclusive; giving both is a
      usage error (`EXIT_USAGE`) before any device interaction.

## Testing

- **Existing tests to run**: `uv run pytest tests/registry/
  tests/deploy/` (sprint 001's registry suite plus tickets 001-006's new
  test modules, must all still pass).
- **New tests to write**: an in-process daemon+API against a `tmp_path`
  store and socket (sprint 001's own CLI-test pattern), with
  `deploy.flash`/`deploy.release` injected as fakes — full
  `mbdeploy deploy` flows for: `--hex` success; `--repo` success; relay
  guard refusal; already-locked fail-fast; `mark_flashed`-against-old-
  daemon non-fatal warning; wait-for-reprobe timeout (no announcement
  arrives); blank-board report.
- **Verification command**: `uv run pytest tests/deploy/`
