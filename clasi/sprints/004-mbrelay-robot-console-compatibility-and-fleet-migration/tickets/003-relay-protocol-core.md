---
id: '003'
title: Relay protocol core
status: in-progress
use-cases:
- SUC-001
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

- [x] `RelayControl.reset_and_normalize()` against a fake in-memory
      `ByteChannel` runs BREAK/reset, `HELLO` (with the documented
      BREAK-fallback-after-retries behavior), `!VER?`, then
      `!MODE RAW250` / `!FRAG OFF` / `!ECHO OFF` / `!P 7` / `!C 0` in that
      order, one command at a time, then verifies via a standalone `?`.
- [x] `clear_stored_config()` sends `!DEFAULTS`.
- [x] `hello()` retries per the ported `hello_attempts` config and falls
      back to a BREAK if nothing answers.
- [x] `normalize()` raises `RelayError` if verification doesn't confirm
      `DEFAULT_CFG` after retries.
- [x] `BannerInfo.parse()` correctly parses both announcement dialects
      relevant to relay boards (`DEVICE:RADIOBRIDGE:...` and the older
      `RADIORELAY` role).
- [x] No import of `inventory.py`, `firmware.py`, `admin.py`, or any
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

## Implementation Notes

Interface decisions later tickets (004 channel adapters, 005 mbrelay CLI,
006/007 console-compat) depend on:

- **Synchronous, not asyncio.** The legacy `relay.py`/`transport.py` were
  built on an asyncio event loop (`add_reader`, `asyncio.Event`). Nothing
  else in `mbtools` uses asyncio (`serial.connect` is plain
  blocking/threaded; `registry.store` documents itself as thread-safe via
  an internal `RLock`), so `relay.protocol` is the blocking/threaded
  equivalent: `ByteChannel`'s methods are all synchronous (`open`/`close`/
  `send_break`/`drain` are plain calls, not coroutines), and
  `Reader.wait_for` uses `threading.Event`/`time.monotonic` instead of
  `asyncio.Event`/the event loop clock. Ticket 004's `LocalRelayChannel`/
  `RemoteRelayChannel` must implement the sync `ByteChannel` Protocol
  accordingly — no `async def` anywhere in this seam.
- **`reset_and_normalize(channel, clear_stored=True)` takes an
  already-constructed `ByteChannel` directly** — no `(factory, port)`
  pair like the legacy `SerialChannelFactory.open(port)` pattern.
  `ChannelFactory` is still ported (interface only, per the ticket's
  Approach) but nothing in `relay.protocol` calls it; ticket 004's
  adapters are each constructed already knowing what they open (a local
  port path, or a registry-obtained remote stream), so there is no
  separate factory step to fit in. Same for `probe(channel)`.
- **`reset_and_normalize()` now also queries `!VER?`**, between `HELLO`
  and the `NORMALIZE_STEPS` batch, so the returned `BannerInfo.firmware`
  is always populated on acquire — this is a deliberate deviation from
  the legacy `relay.py`, where only `probe()` queried firmware version
  and `reset_and_normalize()` did not. Made to match sprint.md's Solution
  section ("resets and normalizes on acquire (BREAK/reset, HELLO, !VER?,
  RAW250/frag-off/echo-off/P7/ch0-grp10)") and this ticket's own
  acceptance criterion, which lists `!VER?` in the sequence.
- **`RelayControl.__init__` takes timing kwargs directly** (`open_settle`,
  `hello_timeout`, `hello_attempts`, `post_close_settle`,
  `break_duration`, `break_settle`, all in seconds, defaults matching the
  legacy repo's `SerialConfig` defaults) rather than a `cfg.serial`
  object — no `relay.config` module is planned in sprint.md's Step 3
  module table, so there is nothing to duck-type against. `post_close_settle`
  is accepted and stored but unused within this module, same as upstream
  (it exists for a caller doing its own close/reopen dance around this
  class, e.g. a future release path).
- `BANNER_RE`/`IDENTITY_RE` are local copies in `relay.protocol`, not
  imported from anywhere — this is the ticket's one explicitly-planned
  deviation from zero-duplication (the alternative was importing
  `inventory.py`, which the ticket explicitly forbids).
- `mbtools.relay.naming` (ticket 001) is unrelated to this module — no
  import needed or added; `relay.protocol` has no address-derivation
  logic of its own, matching the legacy `relay.py` (which didn't import
  `naming.py` either).
