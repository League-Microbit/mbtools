---
id: '011'
title: Real-hardware acceptance
status: open
use-cases: [SUC-001, SUC-002, SUC-003, SUC-004, SUC-005, SUC-006]
depends-on: ['005', '006', '007', '008', '009', '010']
github-issue: ''
issue:
- mbrelay-relay-protocol-client-over-mbregistry.md
- non-root-usb-access-and-pyocd-permission-hang.md
- macos-registry-mdns-discovery-and-outbound-peering-fail-on-braeburn.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Real-hardware acceptance

## Description

Final sprint ticket: verify every preceding ticket's claims on real
hardware, per this project's established acceptance-doc pattern
(`docs/acceptance/001-003-hardware.md`). Unit/integration tests give
confidence in isolation; this ticket is what actually confirms the
robot-console compatibility contract, cross-host relay behavior, and the
two bugfixes against the real fleet. Confirm with the stakeholder which
of the two `robot-console` checkouts on this machine is authoritative
before treating either as the spec of record (Open Question from the
sprint architecture doc); default to
`/Volumes/Proj/proj/robot-projects/robot-console` if not otherwise told.

**Approach**
1. **Firmware**: flash the newest versioned release of the radio-relay
   firmware (`League-Robotics/microbit-radio-relay`, `gh release
   download -R League-Robotics/microbit-radio-relay -p MICROBIT.hex` —
   ignore the moving `latest` tag; needs `--force-relay` per this
   project's existing flash-by-role override) onto one host's spare
   board, and `nezha-robot-template`'s firmware onto the robot boards on
   other hosts. Use a spare board with no announcing firmware (e.g.
   `togov`) — never a robot in active use.
2. **`mbrelay connect` end to end**: `mbrelay connect <robot>@<relayhost>`
   from a different host tunes correctly and gets a `PING`→`pong`
   response from the robot board on the *other* host — confirming the
   robot firmware actually answers over radio (check
   `nezha-robot-template`'s own docs for its radio protocol/PING
   convention if the response shape is unclear). Confirm remote relay
   reset (`mbrelay`'s release-time `!DEFAULTS`, or a fresh reconnect)
   actually resets the relay over the remote stream (ticket 004's
   `RemoteRelayChannel.send_break()`).
3. **robot-console compatibility, against robot-console's own source as
   the spec**: verify the `_mbrelay._tcp` advertisement, TXT
   `registry=<port>`, pool-port reset-by-reconnect behavior (ticket 006),
   and `GET/PUT/DELETE /names/<name>` response shape (ticket 007) exactly
   match what `mbrelayRegistry.ts`/`mdnsDiscovery.ts`/
   `watchers/mdnsWatcher.ts`/`connect/relayBridger.ts` expect. If
   robot-console can be run headless against the new endpoints (Part 1
   research found every relevant module built around fake-injectable
   seams, suggesting this may be feasible), do so; if not feasible in
   this ticket's timebox, verify by directly exercising the endpoints the
   way that source code does (same requests, same expected shapes) and
   document why a full headless run wasn't done.
4. **Non-root access**: confirm `mbdeploy deploy`/`debug` and `mbserial`
   run without `sudo` on a Nolanet node after ticket 008's udev-rule
   install, including the idempotent-re-run case on an already-in-service
   host. Confirm ticket 009's fail-fast by simulating (or, if safe,
   inducing) a permission failure and confirming it reports in seconds.
5. **braeburn**: confirm whether ticket 010's fix resolved mDNS
   discovery/outbound peering, or confirm the `--peer braeburn:7440`
   workaround still works if not — record whichever outcome actually
   occurred, honestly, per architecture Decision 10.
6. **Write up results** in `docs/acceptance/004-hardware.md`, following
   the format of `001`-`003` (what was tested, what host, what worked,
   what didn't, what's still open).

**Files to create/modify**
- `docs/acceptance/004-hardware.md` (new).

**Documentation updates**: this ticket *is* the documentation-update
step for real-hardware findings; if it surfaces anything the public
`docs/wiki/` or architecture doc got wrong, fix those too (see this
project's CLAUDE.md rule on keeping docs current with behavior changes).

## Acceptance Criteria

- [ ] Relay and robot firmware flashed onto the designated test boards
      (spare board only — no robot in active use disturbed).
- [ ] `mbrelay connect <robot>@<host>` succeeds cross-host, with a
      confirmed `PING`→`pong` round trip over radio.
- [ ] Remote relay reset confirmed working over the remote stream.
- [ ] robot-console's actual compatibility endpoints (mDNS/TXT, pool
      port, `/names`) verified against robot-console's own source as the
      spec, with the outcome (including which of the two checkouts was
      used, per the confirmed-or-defaulted decision) recorded.
- [ ] Non-root `mbdeploy`/`mbserial` access confirmed on a Nolanet node,
      including the idempotent-reinstall case.
- [ ] pyOCD fail-fast confirmed to report within seconds under a real or
      simulated permission failure.
- [ ] braeburn's peering outcome (fixed, or workaround re-confirmed) is
      recorded accurately — not claimed fixed unless actually observed
      working on real hardware.
- [ ] `docs/acceptance/004-hardware.md` written, following the `001`-`003`
      format.

## Testing

- **Existing tests to run**: full sprint 004 test suite (`uv run pytest
  tests/relay/ tests/registry/console_compat/
  tests/registry/test_peering.py tests/registry/test_flashlogic.py`) as
  a pre-flight check before spending hardware time.
- **New tests to write**: none required beyond the acceptance write-up
  itself — this ticket is verification, not new unit-testable code.
- **Verification command**: `uv run pytest` (full suite — this ticket
  closes the sprint, and per this project's testing rule the full suite
  runs once per sprint inside `close_sprint`, not per-ticket; this
  ticket's own pre-flight run is a convenience check before hardware
  time, not a substitute for that gate).
