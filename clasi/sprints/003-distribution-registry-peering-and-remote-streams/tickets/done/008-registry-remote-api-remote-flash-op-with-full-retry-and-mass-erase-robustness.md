---
id: 008
title: 'registry.remote_api: remote flash op with full retry and mass-erase robustness'
status: done
use-cases:
- SUC-003
depends-on:
- '003'
- '006'
github-issue: ''
issue: mbdeploy-flash-by-name-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# registry.remote_api: remote flash op with full retry and mass-erase robustness

## Description

Per sprint.md's Architecture (Decision 4, module `registry.remote_api`
responsibility 5), add a `flash` op to `registry.remote_api` distinct
from — and more capable than — both the local Unix socket's minimal
`flash` op (`registry.flash.FlashOp`, sprint 1, deliberately never used
in production) and the JSON-op set ticket 006 added. This op runs
`registry.flashlogic.flash_hex` (ticket 003) server-side, streaming log
lines back exactly like the existing local streaming pattern, then
increments `store.flash_count` itself and triggers the existing
flash-release re-probe hook — no separate `mark_flashed` round-trip is
needed on this path, unlike local `mbdeploy`'s two-call pattern, because
here the registry both flashes and knows it happened in one op.

## Acceptance Criteria

- [x] `{"op": "flash", "uid": "...", "hex_path": "..."}` (hex file bytes
      are sent by the client beforehand or alongside — implementer's
      choice of exact wire shape, e.g. a prior `{"op": "send_hex", ...}`
      streaming the file, or requiring the client to have already placed
      it somewhere the registry can read; document whichever is chosen
      in `docs/design/registry-api.md`) requires a `flash`-kind lock
      already held by this connection's own `HolderRef` — same
      precondition as `api.py`'s existing `_op_flash`, generalized via
      ticket 002.

      Implemented as a two-op sequence: `{"op": "send_hex", "data":
      "<base64>"}` (capped at `remote_api.MAX_HEX_PAYLOAD_BYTES`, 8 MiB)
      stages the decoded bytes into a server-side temp file and returns
      `{"ok": true, "hex_path": "<staged path>"}`; `flash`'s own
      `hex_path` must be exactly that staged path from *this same
      connection's* own prior `send_hex` call (refused as
      `invalid_request` otherwise) — a remote client has no filesystem
      this process can read directly, so `hex_path` is never an
      arbitrary path a client names. Documented in
      `docs/design/registry-api.md`'s new "Remote flash and hex staging"
      section.
- [x] Streams `{"type": "log", "line": ...}` lines exactly like the
      existing local `flash` op's framing (`docs/design/registry-api.md`'s
      existing table), followed by one terminal `{"type": "result", ...}`
      line — reusing that exact response shape so a client library
      (ticket 011) can share parsing code with `registry.client`'s own
      streaming-flash handling if the implementer judges that
      worthwhile.
- [x] Internally calls `registry.flashlogic.flash_hex` (not
      `registry.flash.FlashOp`), getting the same transient-retry/
      mass-erase/blank-board-reporting behavior a local `mbdeploy deploy`
      gets.
- [x] On completion (success or failure), releases the `flash`-kind lock
      unconditionally — same "always release, this is what triggers
      re-probe" guarantee `api.py`'s existing `_op_flash` documents — and
      calls `store.increment_flash_count(uid)` directly on success (this
      registry already owns `store`; no client-facing `mark_flashed` op
      exists on this path).
- [x] A blank-board outcome (mass erase succeeded, reflash still failed)
      is reported through the streamed log lines exactly as
      `flashlogic.flash_hex`'s existing message already does — ticket
      012's client-side `mbdeploy` reporting (the same "blank_board"
      detection `deploy.cli._run_deploy` already does by scanning log
      lines for "no firmware") works unchanged against this remote path
      because the message text is identical by construction (same
      function).

## Testing

- **Existing tests to run**: `tests/registry/flash/` (the sprint-1
  minimal `FlashOp` suite) — unaffected, this ticket doesn't touch it.
- **New tests to write**:
  - Remote `flash` op end-to-end against a fake `pyocd`/subprocess
    (mirrors `flashlogic`'s own existing test fixtures) over a real
    loopback TCP connection: success path increments `flash_count` and
    releases the lock.
  - Transient-retry and mass-erase-recovery paths exercised through the
    remote op, proving the *same* function (not a reimplementation) is
    what's running — e.g. by asserting the exact log line text sprint 1's
    local-path tests already assert on.
  - `flash` without a prior `lock` call is refused (`not_locked`).
- **Verification command**: `uv run pytest tests/registry/remote_api/`
