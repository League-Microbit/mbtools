---
status: pending
---

# mbrelay: connect to a radio relay and send/receive, on top of mbregistry

## Description

Rebuild `mbrelay` as a client that does **only** the relay protocol. It
asks the registry which attached micro:bits are relays
(RADIORELAY/RADIOBRIDGE), locks one, and talks to it. It never enumerates,
probes or lists boards itself (brief §6).

## Scope

- **Pick a relay.** `mbrelay connect [robot[@host]]` chooses a free relay
  (by name, or any free one) through the registry and locks it.
- **Reset and normalize** on acquire and on release: BREAK/reset, `HELLO`,
  `!VER?`, then RAW250 / frag off / echo off / P7 / ch0 grp10, verified
  with `?`, then `!DEFAULTS` on release. Port this from
  `microbit-radio-relay/server/src/mbrelay/relay.py`.
- **Tune to a robot:** `!CG` from the name registry, `!GO`, then an
  optional `PING`.
- **`--send` / `--expect` scripting and the interactive terminal**, as in
  today's `mbrelay connect`.
- **Name registry** (robot name → channel/group, with conflict reporting).
  It needs a home under the one-daemon rule, probably a table in the
  registry database (brief §9.6).
- **robot-console compatibility.** robot-console relies on the
  `_mbrelay._tcp` pool port with reset-by-reconnect, TXT `registry=`, and
  `GET /names/<n>`. Decide between a compatibility endpoint hosted by the
  registry, migrating robot-console, or a transition period (brief §8 and
  §9.6). Breaking this silently mistunes moved robots.

## Port from

`microbit-radio-relay/server/src/mbrelay`: `relay.py`, `session.py`
(acquire/release and sniffing only), `transport.SerialChannel`,
`registry.py`, `naming.py`, `client.py`. Do **not** port `inventory.py`,
`firmware.py`, `admin.py`, or the advertiser half of `mdns.py`.
