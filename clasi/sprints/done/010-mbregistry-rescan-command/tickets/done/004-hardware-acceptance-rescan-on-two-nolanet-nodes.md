---
id: '004'
title: 'Hardware acceptance: rescan on two Nolanet nodes'
status: done
use-cases:
- SUC-001
- SUC-002
depends-on:
- '003'
github-issue: ''
issue: mbregistry-rescan-command.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Hardware acceptance: rescan on two Nolanet nodes

## Description

Validate `mbregistry rescan` (tickets 001-003) against real USB and
real peering on two Nolanet nodes, per project `CLAUDE.md`'s
hardware-testing table and sprint.md's Success Criteria. Use **`loki`
and `magni`** (both dedicated Nolanet test boards, `eric`/passwordless
sudo, per `CLAUDE.md`) — deliberately not `torture` (shared,
scarce-relay production host; `CLAUDE.md`'s standing rule is to prefer
the five per-developer hosts for routine testing). Write up the run as
`docs/acceptance/010-hardware.md`, following the existing
`docs/acceptance/00N-hardware.md` convention (see e.g.
`docs/acceptance/009-hardware.md`'s structure: header naming the
sprint/ticket/hosts/date, a "Pre-existing state" section, then one
subsection per scenario with the exact commands run and their actual
output).

This is the one thing no unit/integration test in tickets 001-003 can
exercise: real USB unplug/replug (or reflash) producing a genuine
`gone` row, and a real peer service stop/restart producing a genuine
`peer unreachable` row and then a real reconnect.

## Acceptance Criteria

- [x] Deploy the sprint branch's build to both `loki` and `magni`
      (`scripts/deploy-host.sh`, per its own usage comment — remember
      to stop `mbregistry.service` first, per project `CLAUDE.md`'s
      "`uv venv --clear` can't remove root-owned `__pycache__`" note).
- [x] On `loki`: unplug/replug (or reflash) its dedicated board to
      produce a `disconnected` row visible in `mbregistry list`; run
      `mbregistry rescan --dry-run` and confirm it reports the row as
      a would-remove candidate without changing `list`'s output; run
      `mbregistry rescan` and confirm the row is gone from `list`/
      `mbregistry list --json`.
- [x] Peering scenario across `loki`/`magni`: with both hosts peered
      (mDNS or `--peer`) and each seeing the other's board, stop
      `mbregistry.service` on `magni` (`sudo systemctl stop
      mbregistry`); confirm `loki`'s own `mbregistry list` eventually
      shows `magni` as `peer unreachable` (per the existing peer-vanish
      detection, unchanged by this sprint) and still lists `magni`'s
      device row; run `mbregistry rescan` on `loki` and confirm both
      the peer row and its mirrored device row are gone from `loki`'s
      `list`.
- [x] Restart `mbregistry.service` on `magni`
      (`sudo systemctl start mbregistry`) and confirm `loki`'s view
      recovers **on its own** (ordinary reconnect/re-peering, not a
      second `rescan`) — `magni` and its device reappear in `loki`'s
      `mbregistry list` without operator intervention beyond the
      restart, matching sprint.md's Success Criteria "restarting the
      peer brings it straight back."
- [x] Confirm a **locked** row survives rescan on real hardware: hold
      a lock on the disconnected/unreachable-peer scenario's device
      (e.g. `mbregistry lock <uid> serial` from a second session, or
      whatever lock-acquisition path is easiest to drive by hand) and
      confirm `rescan` reports it skipped and leaves it in `list`.
      (Note: a lock is normally released automatically when a device
      is detected disconnected — see `daemon.py`'s detach handling —
      so this scenario most likely needs the *peer-unreachable* case,
      where a remote-owned row's cached `remote_lock_kind` can still
      show non-`None`; confirm on real hardware which case actually
      produces an observable locked-and-purge-eligible row, and record
      whatever the real behavior turns out to be — this is exactly the
      kind of thing a hardware pass exists to confirm rather than
      assume.)
- [x] Confirm the forced-immediate-poll behavior: after an unplug that
      produces a `gone` row, replug the board *before* running
      `rescan`, and confirm `rescan`'s own forced poll picks it back
      up as `attached`/probed in the fresh table `rescan` prints,
      without needing to wait for the next `--interval` tick
      separately.
- [x] `mbregistry rescan --json` output captured and sanity-checked
      against tickets 002/003's documented response shape.
- [x] Write `docs/acceptance/010-hardware.md` documenting every
      scenario above with actual commands/output (not paraphrased),
      any quirks found (cross-reference project `CLAUDE.md`'s existing
      known-quirks list — e.g. the `dwc_otg` STATE-flapping quirk, the
      stale-`/opt/mbtools`-symlink quirk — if either is observed to
      affect a scenario here), and update `CLAUDE.md` itself only if a
      **new** real-hardware quirk is found (per the existing
      convention every prior hardware-acceptance ticket follows).

## Implementation Plan

**Approach**: Standard hardware-acceptance ticket — no source changes
expected unless a real defect is found (in which case, fix it, add a
regression test in the relevant ticket 001-003 module, and document
the fix in this ticket's own Implementation Notes, following e.g.
sprint 005 ticket 011's or sprint 007 ticket 007's precedent for how a
hardware-found bug gets written up).

**Files to modify**:
- `docs/acceptance/010-hardware.md` (new).
- `CLAUDE.md` — only if a new quirk or a fix changes standing guidance
  (follow the existing pattern of dated, ticket-attributed entries).
- Any `src/mbtools/registry/*.py` / `tests/registry/**` file, only if
  a real defect surfaces.

**Testing plan**:
- This ticket's "testing" *is* the hardware acceptance pass described
  above — no new unit tests are anticipated unless a defect is found,
  in which case add a regression test in the module that owns the fix
  (`tests/registry/store/`, `tests/registry/api/`,
  `tests/registry/peering/`, etc., matching tickets 001-002's existing
  test layout) before closing this ticket.
- **Existing tests to run**: `uv run pytest` (full suite, once, since
  this is the sprint's last ticket before `close_sprint`'s own
  full-suite gate — no need to duplicate that run here beyond
  confirming nothing this ticket touched broke anything, per
  `.claude/rules/source-code.md`'s scoped-run guidance for ticket
  work).
- **Verification command**: `uv run pytest` plus the manual hardware
  scenarios above.

## Implementation Notes

Full write-up: `docs/acceptance/010-hardware.md`. Ran against `loki`
and `magni` (real USB unbind/bind on `loki`'s board, real
`mbregistry.service` stop/restart on `magni`), with `meili` as an
untouched bystander. Every scenario passed and matched tickets
001-003's documented behavior — **no source defect found, no
`src/`/`tests/` change made**.

Highlights:

- The known `/opt/mbtools` stale-symlink quirk (`CLAUDE.md`, ticket
  007-007) recurred on both `loki` and `magni` right after deploy —
  fixed the same way, by re-pointing `/usr/local/bin/{mbregistry,...}`
  at the fresh venv. Not a new quirk; `CLAUDE.md` unchanged.
- The forced-immediate-poll acceptance criterion needed isolating from
  the default 2s `--interval` (too short to distinguish "rescan's own
  forced poll" from "the next scheduled poll would have caught it
  anyway" over an SSH round trip) — confirmed unambiguously using a
  temporary manually-run instance at `--interval 30`.
- The "locked row survives rescan" criterion confirmed the ticket's
  own speculation was on the right track, with a precise mechanism: a
  lock taken directly against a host's own local socket for a
  *peer-owned* uid (allowed — the local socket is an
  operator-facing view of the whole fleet) is what `_op_rescan`'s
  skip-check (`self._locks.status(uid)`) actually consults — not the
  peer-mirrored `remote_lock_kind` display column, which `mbregistry
  list`'s `LOCKED` column shows for a peer-owned row instead. So a
  locally-taken lock on a remote device is real and rescan-protecting,
  but **invisible** in `list`'s own table. This is pre-existing,
  already-documented behavior (`_api_base.BaseAPIServer._device_dict`'s
  own docstring), confirmed on real hardware rather than a new defect —
  see `docs/acceptance/010-hardware.md`'s Scenario 2/3 for the full
  trace and the throwaway `hold_lock.py` script used to produce it.
- One flaky, non-reproducing test timeout
  (`test_watch_fans_out_to_every_connected_watcher`, 60s
  `pytest-timeout` under parallel load, passed cleanly in isolation)
  unrelated to anything this ticket touched.

Both hosts left running the branch build (`0.20260928.1`),
`mbregistry.service` active/enabled, both dedicated boards rebound and
reprobed correctly.
