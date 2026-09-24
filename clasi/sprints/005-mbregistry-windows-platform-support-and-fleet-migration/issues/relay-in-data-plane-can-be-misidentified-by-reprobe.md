---
status: in-progress
sprint: '005'
tickets:
- 005-001
---

# A relay in the data plane can be misidentified by a re-probe

## Description

Seen once in sprint 004 hardware acceptance (`docs/acceptance/004-hardware.md`,
ticket 011).

**What happened.** A RADIOBRIDGE relay (`togov` on hodr) was in the data
plane, transparently forwarding radio traffic for a robot (`gitev`). While it
was, a re-probe (`identity.probe()`: HELLO, then read) overwrote the relay's
registry row with the *robot's* announcement. For a moment it showed as
`gitev` / `NEZHA2` / robot.

**Why.** In the data plane, the relay forwards our `HELLO` over radio, and
the robot's `device ...` reply comes back through it. A direct
`mbserial --reset HELLO` confirmed the physical board had not changed.

**Recommended fix.**
- When a device's stored role is already relay/bridge (`RELAY`/`BRIDGE`),
  send a BREAK before HELLO on re-probe, so the relay is back in its command
  plane and answers for itself.
- Or never accept a role change from relay to non-relay without a USB
  re-attach or a flash.
- In both cases, never re-probe a device that is locked.

This sits in the daemon's shared re-probe path, which every device goes
through, so a mistake here affects the whole registry. Add a fake-serial
regression test that reproduces the forwarded-announcement case before
changing it.
