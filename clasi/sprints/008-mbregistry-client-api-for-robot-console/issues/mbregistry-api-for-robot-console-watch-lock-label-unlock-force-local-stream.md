---
status: in-progress
split_from: robot-console-on-mbregistry-multi-instance-and-spawn-support.md
sprint: 008
tickets:
- 008-001
- 008-002
- 008-003
- 008-004
- 008-005
---

# mbregistry client API for robot-console: watch, lock label and since, unlock --force, stream on the local socket

## Description

Split on 2026-09-24 from
`robot-console-on-mbregistry-multi-instance-and-spawn-support.md`, which
keeps the multi-instance and spawn work. This is the client-API part of the
mbregistry side of `docs/design/robot-console-integration.md` (§5 items 2, 3,
7 and 8): what robot-console needs in the protocol to become an mbregistry
client without speaking ZeroMQ.

## Work in mbregistry

1. **`watch` op** on the local socket and the remote port: after the `ok`
   reply, pushed JSON-lines events (`attach`, `detach`, `identity`,
   `lock_state`, `name_set`, `name_clear`, `peer_up`, `peer_down`), the same
   types as the PUB bus. This keeps Node clients off ZeroMQ.
2. **Lock `label`:** an optional string on `lock`, returned in
   `locked.holder`, in `list`, and in `lock_state` events. Display only; it is
   never used for authorization. Holder identity stays PID / session-uuid.
3. **Understandable stale-lock breaking:**
   - Add `since` (ISO timestamp) to the lock holder, shown in
     `mbregistry list` and in `locked.holder`.
   - Add `mbregistry unlock --force UID|NAME`. It runs on the owning host
     over the local socket (relying on its file permissions), drops the lock,
     and closes the holder's connection or stream so the holder sees EOF.
   - The remote TCP port gets no force option, and there is no pre-emption
     between clients.
4. **`stream` on the local socket**, not only on the remote TCP port.

## Acceptance

- A `watch` client sees attach, detach and lock events. A `locked` reply
  carries the holder's `label` and `since`.
- `mbregistry unlock --force` on a board locked by a live client releases
  the lock, and that client's stream closes.
- `stream` works over the local socket with the same framing as the remote
  port (`stream_frame.py`).
- `docs/design/registry-api.md` is updated.

## Decisions (stakeholder, 2026-09-24)

- No pre-emption between clients. The only override is the operator's
  `unlock --force`, for stale locks. Students are not expected to use it.
- The lock `label` is informational only; authentication is unchanged
  (optional shared token, no per-user identity).
