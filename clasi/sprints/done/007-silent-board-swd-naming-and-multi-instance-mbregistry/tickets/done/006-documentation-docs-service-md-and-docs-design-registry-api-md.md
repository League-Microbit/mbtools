---
id: '006'
title: 'Documentation: docs/service.md and docs/design/registry-api.md'
status: done
use-cases:
- SUC-002
- SUC-003
- SUC-004
- SUC-005
- SUC-006
- SUC-007
- SUC-008
depends-on:
- '001'
- '002'
- '003'
- '004'
- '005'
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

- [x] `docs/service.md` §1's "run exactly one per host" framing
      accurately reflects when multiple instances are and aren't
      supported.
- [x] `docs/service.md` §4's port table includes `--pool-port`/
      `--names-port` with correct default/override columns.
- [x] `docs/service.md` §5's flags table includes every new flag from
      this sprint, each with its correct env var and default.
- [x] `docs/service.md` documents the `--ready-json`/
      `--exit-with-parent`/`--no-peering` spawn recipe with a working
      example command.
- [x] `docs/service.md` has a one-line upgrade note for the `STATE`
      meaning change.
- [x] `docs/design/registry-api.md` documents the new `STATE` value,
      chip-identity fields, and `--json` structured field, matching
      what tickets 001/003 actually implemented (verify against the
      merged code, not against sprint.md's own draft names — sprint.md
      Open Question 1 left the exact constant name to the implementing
      ticket).
- [x] `docs/design/registry-api.md` cross-references
      `docs/design/robot-console-integration.md` §5 for completed vs.
      pending items.
- [x] No example `mbregistry list`/`mbdeploy list` table anywhere in
      `docs/` still shows the old two-way `no-firmware` state as the
      only "no announcement" case.
- [x] Every path/port/flag/env-var claim in the edited sections is
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

## Implementation Notes

Read the actual merged code (`cli.py`, `claims.py`, `store.py`,
`render.py`, `identity.py`, `paths.py`, `console_compat/relay_pool.py`)
and every done ticket's own Implementation Notes before writing any doc
text, per this ticket's own Approach — no flag name, default, or state
constant was copied from sprint.md's draft text. Cross-checked against
real `mbregistry run --help`/`mbregistry --help` output, and against a
real `mbregistry run --ready-json --exit-with-parent --no-peering`
invocation (ephemeral ports, a short-path tmp socket/db) to confirm the
`--ready-json` payload shape and the `peer_pub`/`peer_snapshot`-omitted-
under-`--no-peering` behavior documented below actually matches (caught
and fixed one self-inconsistency in the docs' own spawn-recipe example
this way: an earlier draft's sample `--ready-json` output still showed
`peer_pub`/`peer_snapshot` despite the sample command using
`--no-peering`).

**`docs/service.md`**:
- §1 rewritten: "exactly one per host" is now conditional — lists what
  must be distinct across instances (ports, `--socket`, `--instance`)
  and the cross-instance claim that arbitrates board access, while
  keeping "exactly one, unless you have a concrete reason" as the
  default mental model. Two instances left at colliding default ports
  still fail to bind, unchanged.
- §4: `--pool-port`/`$MBREGISTRY_POOL_PORT` and
  `--names-port`/`$MBREGISTRY_NAMES_PORT` added to the port table
  (values confirmed against `console_compat/relay_pool.py`'s
  `DEFAULT_POOL_PORT = 7444`/`DEFAULT_NAMES_API_PORT = 7445`); the mDNS
  table's "Instance name" column now says `--instance`/
  `$MBREGISTRY_INSTANCE`, else host name, for both service types.
- §5: all nine new `run` flags added to the flags table (confirmed
  verbatim against `build_parser`'s `add_argument` calls and a live
  `--help` run); a new "Cross-instance board claim" subsection (the
  ticket's own note that `registry-api.md` should get this content led
  to giving `service.md` the operator-facing version too, since it's an
  operational concept an operator configuring `--only-uid`/
  `--exclude-uid` needs, not just a wire-protocol detail); a new "Spawn
  recipe" subsection with the three-flag example command, verified by
  actually running it (see above) rather than hand-typing the expected
  output.
- §11: one-line upgrade note on the `STATE_CONNECTED_NO_FIRMWARE`
  meaning narrowing, "relabels on next real event, no backfill" per
  sprint.md's Migration Concerns/Open Question 3 (documented as the
  chosen behavior, not flagged as still-open — no stakeholder objection
  surfaced during tickets 001-005).

**`docs/design/registry-api.md`**:
- `list`'s response JSON example gained `attached_no_announce` in the
  `state` enum and the two `chip_identity_*` fields, plus prose
  explaining the `connected_no_firmware` meaning narrowing, the
  STATE/FIRMWARE cell rendering for both states, the `NAME`-fallback
  behavior, and the "no more free-text line after the table" change.
- New "Cross-instance board claim" section (top-level `##`, placed
  before "Locking and connection lifetime" since it's a third kind of
  exclusivity alongside that section and the peer-replicated lock
  cache) — this doc had no prior claim/exclusivity write-up to extend,
  confirmed by grepping for "claim"/"exclusiv" before writing it, so a
  new section was added per the ticket's own contingency instruction.
- "Known limitations" gained two new bullets: the peering-payload
  chip-identity gap (already flagged in ticket 003's Implementation
  Notes as not this sprint's scope) and a cross-reference to
  `docs/design/robot-console-integration.md` §5 naming items 1/4/5/6 as
  completed this sprint and 2/3/7/8 as pending (sprint 008).

**Stale-example sweep**: grepped `docs/` and `README.md` for
`STATE`/`no-firmware`/`no firmware`. Two design docs
(`docs/design/usecases.md` UC-001/UC-002/UC-004,
`docs/design/specification.md` §3.8) still describe the old two-state
model in prose, but neither has an actual example `list`/`mbdeploy list`
*table* (the acceptance criterion's own scope) — they're the original
project-initiation brief/use-case documents, never updated for any
later sprint's table changes either (e.g. sprint 003's HOST column is
also absent from UC-004's prose), so leaving them as a historical
initial-brief snapshot follows existing precedent rather than scope
creep. Two files under `docs/acceptance/` (`002-hardware.md`,
`004-hardware.md`) contain real captured `list` table output from past
hardware sessions; left untouched as immutable session records (one
shows `STATE=gone`, unaffected by this sprint's split; neither shows a
`no-firmware` row being used to represent "didn't announce"). No
example table anywhere in `docs/`/`README.md` shows the old two-way
model.

**Testing**: no automated test changes (documentation-only ticket, per
the ticket's own Testing plan). Verified `mbregistry --help`/
`mbregistry run --help` output against every documented flag/default.
Ran `uv run pytest tests/registry/cli -q` (103 passed) as a sanity check
that this session's doc-only edits didn't touch any source file.
