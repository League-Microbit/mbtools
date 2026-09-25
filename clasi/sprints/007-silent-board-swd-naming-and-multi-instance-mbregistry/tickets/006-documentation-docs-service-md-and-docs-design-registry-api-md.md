---
id: '006'
title: 'Documentation: docs/service.md and docs/design/registry-api.md'
status: open
use-cases: [SUC-002, SUC-003, SUC-004, SUC-005, SUC-006, SUC-007, SUC-008]
depends-on: ['001', '002', '003', '004', '005']
github-issue: ''
issue:
- name-silent-boards-over-swd-and-keep-list-table-clean.md
- robot-console-on-mbregistry-multi-instance-and-spawn-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Documentation: docs/service.md and docs/design/registry-api.md

## Description

Update every doc this sprint's success criteria name, once all five
functional tickets have landed and the exact flag/state/field names they
chose are known. `docs/service.md`'s own header promises "every path,
port, flag and environment variable below was checked against the
source" — this ticket is what keeps that promise true after this
sprint.

**`docs/service.md`**:
- §1 ("What runs where"): the "Run **exactly one** `mbregistry` per
  host... The second one fails to bind port 7440" framing is now wrong
  for the non-conflicting-port case introduced by this sprint — rewrite
  to describe when multiple instances are supported (distinct
  `--instance`, non-colliding ports via `0`/explicit values, the
  cross-instance claim preventing double-open of a board) versus when a
  second instance would still conflict (two instances both trying to
  bind the *same* explicit port).
- §4 ("Ports and mDNS"): add rows for `--pool-port`/
  `$MBREGISTRY_POOL_PORT` and `--names-port`/`$MBREGISTRY_NAMES_PORT` to
  the port table (currently "no override" for 7444/7445); update the
  mDNS table to mention `--instance` as the instance-name source
  (currently says "host name" unconditionally).
- §5 ("Flags and environment variables"): add every new flag from this
  sprint (`--pool-port`, `--names-port`, `--instance`, `--pipe`,
  `--only-uid`, `--exclude-uid`, `--ready-json`, `--exit-with-parent`,
  `--no-peering`) to the existing flags table, following its existing
  row format exactly (flag | env var | default | notes).
- Add a short new subsection (or extend an existing one) documenting the
  `--ready-json`/`--exit-with-parent`/`--no-peering` spawn recipe, and a
  one-line upgrade note for the `STATE_CONNECTED_NO_FIRMWARE` meaning
  change (per sprint.md's Migration Concerns), mirroring sprint 006's
  own upgrade-note precedent in this same doc.

**`docs/design/registry-api.md`**: add/update whatever this doc
documents about `list`'s response shape (new `STATE` value, the chip-
identity fields, the `--json` structured field that replaced
`error_note` prose) and about the claim concept (if this doc describes
per-uid exclusivity at all today — check at implementation time; add a
short section if not). Cross-reference `docs/design/robot-console-
integration.md` §5 for which items this sprint completed (1, 4, 5, 6)
versus what's still pending (2, 3, 7, 8 — sprint 008).

**Any user-facing doc describing `list` output** (check `README.md` and
anywhere else in `docs/` that shows an example `mbregistry list`/
`mbdeploy list` table) for a stale `STATE`/`FIRMWARE` example that no
longer matches the three-way state model.

## Acceptance Criteria

- [ ] `docs/service.md` §1's "run exactly one per host" framing
      accurately reflects when multiple instances are and aren't
      supported.
- [ ] `docs/service.md` §4's port table includes `--pool-port`/
      `--names-port` with correct default/override columns.
- [ ] `docs/service.md` §5's flags table includes every new flag from
      this sprint, each with its correct env var and default.
- [ ] `docs/service.md` documents the `--ready-json`/
      `--exit-with-parent`/`--no-peering` spawn recipe with a working
      example command.
- [ ] `docs/service.md` has a one-line upgrade note for the `STATE`
      meaning change.
- [ ] `docs/design/registry-api.md` documents the new `STATE` value,
      chip-identity fields, and `--json` structured field, matching
      what tickets 001/003 actually implemented (verify against the
      merged code, not against sprint.md's own draft names — sprint.md
      Open Question 1 left the exact constant name to the implementing
      ticket).
- [ ] `docs/design/registry-api.md` cross-references
      `docs/design/robot-console-integration.md` §5 for completed vs.
      pending items.
- [ ] No example `mbregistry list`/`mbdeploy list` table anywhere in
      `docs/` still shows the old two-way `no-firmware` state as the
      only "no announcement" case.
- [ ] Every path/port/flag/env-var claim in the edited sections is
      re-checked against the actual merged source and `--help` output
      (per `docs/service.md`'s own stated editorial standard), not
      copied from sprint.md's draft text verbatim.

## Implementation Plan

**Approach**: Read the actual merged code from tickets 001-005 (not this
sprint's planning draft) before writing a word — flag names, the state
constant's final name, and the exact JSON field shape are all
implementer decisions tickets 001-005 made, not guaranteed to match
sprint.md's placeholder examples verbatim.

**Files to modify**:
- `docs/service.md`
- `docs/design/registry-api.md`
- `README.md` and/or any other doc with a stale `list` example (found by
  grepping for `STATE` / `no firmware` / `no-firmware` across `docs/`
  and the repo root at implementation time).

**Testing plan**: No automated test changes expected (documentation-only
ticket). If `docs/service.md` or `docs/design/registry-api.md` has any
existing doctest-style/example-validation tooling (check for one before
assuming there isn't), run it. Otherwise, verification is manual:
run `mbregistry --help` / `mbregistry run --help` and diff the real flag
list against what's documented.

**Documentation updates**: this ticket *is* the documentation update for
the whole sprint — see Description above for the full list.
