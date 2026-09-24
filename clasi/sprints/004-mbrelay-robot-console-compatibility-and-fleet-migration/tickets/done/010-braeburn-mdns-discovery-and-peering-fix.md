---
id: '010'
title: braeburn mDNS discovery and peering fix
status: done
use-cases:
- SUC-006
depends-on: []
github-issue: ''
issue: macos-registry-mdns-discovery-and-outbound-peering-fail-on-braeburn.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# braeburn mDNS discovery and peering fix

## Description

Found in sprint 003 hardware acceptance, not root-caused: no Linux peer
ever discovers `braeburn`'s `_mbregistry._tcp` advertisement (confirmed
at the raw-multicast level — even `avahi-browse` on a Nolanet node never
sees it, though `dns-sd -B` sees it fine Mac-to-Mac), and separately
`braeburn`'s own daemon cannot complete an outbound ZMQ snapshot request
to *any* peer even though a bare script on the same machine, same venv,
succeeds every time. Inbound (`--peer braeburn`) already works reliably.
Three hypotheses already refuted (host firewall, DNS resolution, shared
zeroconf/zmq context — see `docs/acceptance/003-hardware.md`). This
ticket applies and tests the remaining candidates (architecture Decision
10) — best-effort, not a guaranteed root cause.

**Approach — work through the documented remaining candidates in order**:
1. **macOS Application Firewall / local-network privacy prompt treating
   the venv python differently from an interactive script.** Check
   whether `mbregistry run` under `nohup` from a venv python triggers (or
   silently fails) a local-network permission prompt that an interactive
   terminal script doesn't. Add diagnostic logging that surfaces this
   condition if detected.
2. **Which interface/address zeroconf binds/advertises on macOS.**
   Compare explicitly against what a bare working script uses; if
   `python-zeroconf`'s default interface selection differs from the
   working script's, pin it explicitly.
3. **REQ-socket creation ordering relative to the event loop.** Check
   whether `braeburn`'s outbound REQ socket is created before or after
   the event loop starts, and whether that ordering differs from the
   working bare-script reproduction; fix the ordering if it's the cause.
- Add better diagnostics regardless of which candidate (if any) turns out
  to be the root cause, so a future recurrence is faster to diagnose than
  this one was.
- If none of the candidates resolves it: document that clearly, keep the
  `--peer braeburn:7440` workaround as the supported path, and leave the
  issue open rather than claiming a fix that real hardware doesn't
  confirm (architecture Decision 10 / Open Questions).

**Files to create/modify**
- `src/mbtools/registry/peering.py` (targeted fix to whichever candidate
  is confirmed, plus diagnostic logging).
- `tests/registry/test_peering.py` (extended, where the fix is testable
  without real hardware — e.g. interface-selection logic).

**Documentation updates**: `docs/acceptance/003-hardware.md` is not
retroactively edited (historical record); this ticket's findings and
ticket 011's real-hardware confirmation go into
`docs/acceptance/004-hardware.md`. If unresolved, the Robot Garage wiki's
existing workaround note (if any) is confirmed still accurate.

## Acceptance Criteria

- [x] Each of the three candidate causes is checked and the finding
      (confirmed cause, ruled out, or inconclusive) is recorded in
      `docs/acceptance/004-hardware.md`. (Candidate 1 ruled out, candidate
      2 ruled out, candidate 3 confirmed as a real, independent defect and
      fixed; a fourth, better-evidenced failure mode -- mDNS responsiveness
      degrading over uptime, not a static condition -- was found and
      documented as open.)
- [ ] If a cause is confirmed and fixed: `mbregistry list` on a Linux
      node shows `braeburn`'s devices with no `--peer` flag, verified on
      real hardware in ticket 011. (Not checked here: no single candidate
      was confirmed as *the* root cause of the original symptom -- see the
      "New finding" in `docs/acceptance/004-hardware.md`. That doc does
      record a real-hardware snapshot of this working, unprompted, right
      after a fresh restart -- supporting evidence, not a resolution
      claim; ticket 011 should re-check after several hours of uptime.)
- [x] If not resolved: the `--peer braeburn:7440` workaround is
      re-confirmed working on real hardware, and the residual issue is
      explicitly flagged (not silently dropped) for follow-up. (See
      `docs/acceptance/004-hardware.md`'s "Residual issue for follow-up"
      section; the workaround's own code path is unchanged by this
      ticket's fix.)
- [x] No regression to Linux-to-Linux peering (already-working
      mDNS/ZMQ peering between the four Nolanet nodes is unaffected).
      (`tests/registry/peering/` -- 62 tests, run repeatedly, all passing;
      full suite `uv run pytest -q` -- 775 passed, 2 skipped; `loki`'s own
      live `mbregistry.service` continued showing `meili`/`magni`/`hodr`
      correctly throughout this session's real-hardware testing.)
- [x] Any new diagnostic logging added is useful on its own even if the
      root cause isn't found this sprint (e.g. clearly logs which
      interface/address was bound, or whether a permission prompt was
      detected). (`PeerDiscovery.start()`'s new advertise-address/port
      `INFO` log, and the new self-check thread's `WARNING` on a stale
      mDNS responder -- see `src/mbtools/registry/peering.py`.)

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_peering.py` (confirm no regression to
  already-working Linux-to-Linux peering).
- **New tests to write**: whatever is testable without real macOS
  hardware for the confirmed/attempted fix (e.g. interface-selection
  logic can be unit tested with a fake network-interface list); the
  actual mDNS/ZMQ behavior on macOS can only be confirmed in ticket 011.
- **Verification command**: `uv run pytest tests/registry/test_peering.py`
