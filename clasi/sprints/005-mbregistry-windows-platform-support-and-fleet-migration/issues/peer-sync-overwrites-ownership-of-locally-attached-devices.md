---
status: in-progress
sprint: '005'
tickets:
- 005-011
---

# Peer sync overwrites ownership of locally attached devices ("local must win")

## Description

Found in sprint 005 ticket 007, when mbregistry replaced the old mbrelay on
torture.

**Symptom.** torture's relay pool reports **0 devices**, even though its relays
are physically attached and its own daemon scans them as local.

**Cause.**
- hodr tested some of the same boards in earlier sprints. Its database still
  holds them as its own local rows, now disconnected.
- hodr keeps publishing those rows through the snapshot and event bus.
  `snapshot_local_devices` sends every `host IS NULL` row, including
  disconnected ones.
- On torture, `Store._upsert_device`'s remote path does an unconditional
  `host=?` UPDATE (store.py around lines 530-535, reached from
  `peering._apply_snapshot_device` and `_apply_event`). That re-tags torture's
  own local rows as belonging to hodr.
- `console_compat.relay_pool` only offers `host IS NULL` rows, so it sees
  nothing.

**Impact.** Boards move between hosts in real use. Whenever a board is moved,
the host it moved to can lose it. A production relay host then has an empty
pool, and robot-console finds no relays.

## Fix

1. **Local wins.** A remote upsert or event must never overwrite a row that
   this host currently owns and has attached (host NULL and connected).
   Log it and drop it.
2. **Don't advertise stale ownership.** Snapshots and events should send only
   devices that are currently connected. Alternatively, send the
   disconnected state, and receivers must not treat a disconnected remote row
   as a claim of ownership.
3. **Hand-over.** When a remote host reports a uid as newly attached and this
   host has it as local but disconnected, ownership moves to the remote host.
   When this host then sees the uid attached locally again, the local claim
   is restored.
4. **Tests.** Cover the torture/hodr scenario, the reverse move, and
   ping-pong.
5. **Recovery.** Existing databases (torture, hodr) must end up correct after
   a daemon restart with the fix, without anyone deleting DB files by hand.

Afterwards, verify on hardware: torture's pool lists its attached relays, and
`mbregistry list` everywhere shows them as `host=torture`.
