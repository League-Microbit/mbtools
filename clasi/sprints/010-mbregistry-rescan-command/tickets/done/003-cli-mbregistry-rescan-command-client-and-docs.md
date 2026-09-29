---
id: '003'
title: 'CLI: mbregistry rescan command, client, and docs'
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '002'
github-issue: ''
issue: mbregistry-rescan-command.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# CLI: mbregistry rescan command, client, and docs

## Description

Surface ticket 002's `rescan` op as `mbregistry rescan [--dry-run]
[--json]`: a typed `RegistryClient.rescan()` method, the CLI
subcommand (summary line(s) followed by a fresh `list` table), and the
documentation updates (`docs/design/registry-api.md`, CLI
`--help`/README) sprint.md's scope calls for.

Follows `cmd_unlock`/`RegistryClient.force_unlock`'s existing
local-socket-only CLI pattern (find the local api address, connect,
call, handle `RegistryUnavailable`/`RegistryClientError` the same
way) — no new error-handling convention.

## Acceptance Criteria

- [x] `RegistryClient.rescan(dry_run=False)` sends `{"op": "rescan",
      "dry_run": dry_run}` and returns the parsed response (removed
      devices/peers, skipped-locked list, `dry_run` echo) — same
      typed-method shape as `list`/`force_unlock`.
- [x] `mbregistry rescan` (no flags): connects, calls `rescan()`,
      prints a short human summary matching the issue's own example
      wording ("removed 3 gone devices, 1 unreachable peer (braeburn)
      and its 2 devices; skipped 1 locked"), then calls `list()` and
      prints the fresh table via the existing `render_table` — two
      client calls composed in the CLI layer, not a combined server
      response.
- [x] `mbregistry rescan --dry-run`: prints what *would* be
      removed/skipped, does **not** print a fresh list table
      (nothing changed, so the existing `list` output would be
      identical noise) — the summary line makes clear this was a
      preview (e.g. "would remove ..." vs "removed ...").
- [x] `mbregistry rescan --json` / `--dry-run --json`: prints the
      server's structured response (removed/skipped/dry_run) as JSON,
      no human sentence; when not `--dry-run`, includes the fresh
      device list in the same JSON payload (one JSON object out, not
      two separate JSON blobs on stdout) — add a small `render.py`
      helper alongside `render_json`/`render_table` for this combined
      shape rather than hand-rolling `json.dumps` in `cli.py`.
- [x] `mbregistry rescan --socket <path>` (and `$MBREGISTRY_SOCKET`)
      resolves the local API address exactly like every other
      subcommand — reuses `find_local_api_address`, no new resolution
      logic.
- [x] Connection failure (`RegistryUnavailable`) prints the same
      "is the daemon running?" guidance and `EXIT_NO_DAEMON` every
      other subcommand already gives; a `RegistryClientError` (e.g.
      the remote-plane rejection, exercised only if someone points
      `--socket` at something that isn't this daemon — not expected in
      normal use) prints `exc.message`/`exc.exit_code`.
- [x] `docs/design/registry-api.md` gains a `### rescan` section
      (request/response shape, "local Unix socket / Windows pipe
      only — never on the remote TCP control plane" callout, modeled
      on the existing `force_unlock` section) and a line in the ops
      table near the top.
- [x] CLI `--help` (the `rescan` subparser's own `help=`/description)
      and any README/command-reference listing of `mbregistry`
      subcommands documents the new command, its flags, and its
      local-only scope.

## Implementation Plan

**Approach**: Add `rescan` to `client.py` next to `force_unlock`
(same `_request`/typed-return pattern). Add `cmd_rescan` to `cli.py`
next to `cmd_unlock`, and its `argparse` subparser next to
`unlock_p`'s own registration — `--dry-run` (`store_true`) and
`--json` (`store_true`, matching `list`'s own flag), plus the shared
`--socket`. Add the render helper for the combined
summary+list JSON shape to `render.py`.

**Files to modify**:
- `src/mbtools/registry/client.py` — `RegistryClient.rescan`.
- `src/mbtools/registry/cli.py` — `cmd_rescan`, its subparser.
- `src/mbtools/registry/render.py` — summary formatting (text +
  JSON), reusing `render_table`/`render_json` for the list half.
- `docs/design/registry-api.md` — new `### rescan` section + ops
  table row.
- Any CLI command-reference doc/README listing subcommands (check
  `docs/design/` and top-level `README.md` for an existing
  `mbregistry` command list to extend).

**Testing plan**:
- `tests/registry/client/`: `rescan(dry_run=...)` sends the right
  request shape and parses the response.
- `tests/registry/cli/`: `cmd_rescan` output for all four
  combinations (`rescan`, `--dry-run`, `--json`,
  `--dry-run --json`) against a fake/stub client, matching the
  acceptance criteria above (dry-run never prints a fresh table;
  `--json` is one combined object, not two blobs); `RegistryUnavailable`
  prints the standard daemon-not-running guidance and
  `EXIT_NO_DAEMON`.
- **Existing tests to run**: `uv run pytest tests/registry/client/
  tests/registry/cli/`.
- **Verification command**: `uv run pytest tests/registry/client/
  tests/registry/cli/`.

## Implementation Notes

- **Summary wording, adapted from the issue's literal example.**
  Ticket 002's wire response (`_op_rescan` in `_api_base.py`) reports
  `removed.devices` as one flat list of uids and `removed.peers` as a
  separate list of hostnames — it does not tag each removed device
  with *why* it was a candidate (local-gone vs. owned-by-a-removed-
  peer vs. a reachable peer's stale mirror; see `store.PurgeCandidates`,
  which *does* keep that breakdown, but `_op_rescan` flattens it via
  `PurgeCandidates.device_uids` before returning). The issue's own
  example sentence ("removed 3 gone devices, 1 unreachable peer
  (braeburn) and its 2 devices; skipped 1 locked") implies a
  peer-grouped breakdown the wire response can't reconstruct without
  an extra `list()` round-trip taken *before* the purge (to learn each
  candidate's owning host) — out of scope to add here since it would
  change ticket 002's already-done, already-tested response shape.
  `render_rescan_summary` (`render.py`) instead prints one line per
  *category* (devices removed, peers removed, locked skipped), each
  only when non-empty, in the same "removed N `<category>`: `<uids
  short_uid, or hostnames>`" style — matching the issue's wording
  style and its three categories, not the exact peer-grouped sentence.
  Confirmed with team-lead's dispatch framing ("a one-line-per-category
  summary") as the intended reading.
- **uid display**: a rescan response only carries bare uid strings, no
  precomputed `short_uid` field (unlike a `list`/`find` device dict).
  `render.py`'s `_short_uid_display` recomputes it via
  `mbtools.registry.identity.short_uid` (not a `uid[-8:]` fallback) so
  a uid named in the summary line matches the same short identifier the
  fresh `list` table below (or a prior `list --json`) would show for
  that same row.
- Verified end-to-end against a real `mbregistry run --no-peering
  --no-relay-pool` daemon (temp `--socket`/`--db`, short path under
  `$TMPDIR` — the AF_UNIX 104-byte `sun_path` limit rules out the
  session scratchpad directory here) with hand-seeded gone/
  unreachable-peer rows: `rescan --dry-run`, `rescan`, a second
  `rescan` reporting `nothing to remove`, and `rescan --json` all
  produced the expected output. `--no-relay-pool` was needed alongside
  `--no-peering` — `console_compat.relay_pool.RelayPool` advertises
  over mDNS independently of `--no-peering` and collides with macOS's
  own `mDNSResponder` on port 5353 in this dev environment (the same
  known UDP-5353 issue flagged for `test_cli_spawn.py`'s
  `test_ready_json_line_reaches_a_real_pipe_promptly`).
