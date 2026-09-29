---
status: in-progress
sprint: '010'
tickets:
- 010-001
- 010-002
- 010-003
- 010-004
---

# `mbregistry rescan`: drop gone devices and unreachable peers, then rescan

## Description

Stakeholder request (2026-09-28): add `mbregistry rescan`. After it runs,
`mbregistry list` should show only what's actually active right now: every
device showing `gone` and every peer that is `peer unreachable` gets
deleted, and the registry rescans. "It gets rid of all the cruft."

This is an explicit, operator-invoked exception to the store's "Decision
5" rule (a vanished peer is marked unreachable, never deleted) and to the
never-delete precedent for device rows. The automatic paths keep that
behaviour; only this command deletes.

## Behaviour (team-lead defaults; confirm or adjust during planning)

- **Runs against the local daemon only** (local API socket/pipe, like
  `unlock`). New API op, e.g. `{"op": "rescan"}`, on the local API
  (Unix socket and the Windows pipe). Not exposed on the remote TCP
  control plane (`remote_api.py`).
- **Deletes** from this host's store:
  - local device rows in `STATE_DISCONNECTED` ("gone");
  - every row owned by a peer that is currently unreachable;
  - remote-owned rows in `STATE_DISCONNECTED` (a reachable peer no
    longer advertises disconnected rows since sprint 005 ticket 011, so
    any such row is stale);
  - `peer` rows with `reachable = false`.
- **Keeps**: any row with an active lock (report it as skipped); the
  `name_registry` table (names/channels must stay stable); anything
  connected or attached.
- **Then rescans**: runs an immediate USB poll (not waiting for the next
  `--interval` tick) and asks each reachable peer for a fresh snapshot, so
  a board or peer that is actually alive reappears right away. A peer
  given with `--peer` or rediscovered over mDNS comes back by itself.
- **Publishes events** on the event bus for the removed rows, so
  `watch` clients and peers see them disappear (check what event kind
  fits; a device removal may need a new kind).
- **Output**: a short summary ("removed 3 gone devices, 1 unreachable
  peer (braeburn) and its 2 devices; skipped 1 locked") followed by the
  fresh `list` table. `--json` for scripts. `--dry-run` to show what
  would be removed without removing it.
- Race with a device that is mid-reattach: delete by uid inside the
  store's lock, only if the row is still in the gone/unreachable state
  at delete time.

## Things to check

- `src/mbtools/registry/store.py`: the "never delete" design notes and
  docstrings need updating to say `rescan` is the one explicit
  exception.
- `daemon.py`'s attach/detach diff (`previously_attached`, ticket
  005-011) must not be confused by a row vanishing underneath it.
- `docs/design/` and the CLI help/README.
- Hardware acceptance on two Nolanet nodes: unplug/replug or reflash
  to create a `gone` row, stop a peer's service to create an
  unreachable peer, run `rescan`, confirm the list is clean and that
  restarting the peer brings it straight back.
