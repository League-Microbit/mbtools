---
id: '003'
title: Relay protocol core
status: open
use-cases: [SUC-001]
depends-on: []
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Relay protocol core

## Description

Port the relay command-plane protocol (reset/normalize/tune/restore)
from `microbit-radio-relay/server/src/mbrelay/relay.py`, near-verbatim,
against a `ByteChannel` Protocol ported from that repo's `transport.py`.
This is pure logic with no `mbregistry`/socket/CLI knowledge (sprint
architecture Decision 3) — the seam that makes ticket 004's adapters the
only new code needed to run it against a registry-owned connection.

**Approach**
- Port `transport.py`'s `ByteChannel`/`ChannelFactory` Protocol
  definitions as-is (interface only — not `SerialChannel`, which is
  legacy-repo-specific and superseded by ticket 004's adapters).
- Port `relay.py`'s `BannerInfo`, `Reader`, and `RelayControl` (`hello`,
  `normalize`, `query`, `reset_and_normalize`, `clear_stored_config`,
  `firmware_version`, `robot_version`, `probe`) unchanged in logic,
  relocated into `src/mbtools/relay/protocol.py`. Keep
  `NORMALIZE_STEPS`'s one-command-at-a-time sending (not a single burst —
  `!MODE` disables the radio peripheral and a burst behind it gets
  dropped, per the ported code's own documented reasoning) and the
  standalone post-normalize `query()` verification (not scanning setting
  replies, since `!P` also emits a config line that would false-negative
  a scan).
- Replace the one `inventory.py` import (`BANNER_RE`/`IDENTITY_RE`
  regexes) with a local copy — no other `inventory.py`/`firmware.py`/
  `admin.py`/mDNS-advertiser coupling exists in the ported files.

**Files to create/modify**
- `src/mbtools/relay/protocol.py` (new — `ByteChannel`, `ChannelFactory`,
  `BannerInfo`, `Reader`, `RelayControl`, `NORMALIZE_STEPS`,
  `DEFAULT_CFG`).
- `tests/relay/test_protocol.py` (new).

**Documentation updates**: none required by this ticket alone.

## Acceptance Criteria

- [ ] `RelayControl.reset_and_normalize()` against a fake in-memory
      `ByteChannel` runs BREAK/reset, `HELLO` (with the documented
      BREAK-fallback-after-retries behavior), `!VER?`, then
      `!MODE RAW250` / `!FRAG OFF` / `!ECHO OFF` / `!P 7` / `!C 0` in that
      order, one command at a time, then verifies via a standalone `?`.
- [ ] `clear_stored_config()` sends `!DEFAULTS`.
- [ ] `hello()` retries per the ported `hello_attempts` config and falls
      back to a BREAK if nothing answers.
- [ ] `normalize()` raises `RelayError` if verification doesn't confirm
      `DEFAULT_CFG` after retries.
- [ ] `BannerInfo.parse()` correctly parses both announcement dialects
      relevant to relay boards (`DEVICE:RADIOBRIDGE:...` and the older
      `RADIORELAY` role).
- [ ] No import of `inventory.py`, `firmware.py`, `admin.py`, or any
      mDNS-advertiser code exists anywhere in the new module.

## Testing

- **Existing tests to run**: none yet exist for this new module (no
  regression risk to existing code — this ticket adds a self-contained
  module).
- **New tests to write**: `tests/relay/test_protocol.py` using a fake
  in-memory `ByteChannel` (no serial hardware, no registry) — cover
  hello/normalize/query/reset_and_normalize/clear_stored_config, the
  BREAK-fallback path, and both banner dialects.
- **Verification command**: `uv run pytest tests/relay/test_protocol.py`
