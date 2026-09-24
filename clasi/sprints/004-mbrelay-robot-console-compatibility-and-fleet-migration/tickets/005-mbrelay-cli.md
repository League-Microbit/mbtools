---
id: '005'
title: mbrelay CLI
status: open
use-cases: [SUC-001]
depends-on: ['001', '004']
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbrelay CLI

## Description

Build the `mbrelay` command-line client: `connect [robot[@host]]`,
scripted `--send`/`--expect`, and an interactive terminal — structured
exactly like `serial.cli`/`serial.connect`'s local/remote dispatch
(mirroring the pattern confirmed in this sprint's own code research).
This is the CLI/UX layer; all protocol work delegates to ticket 003's
`relay.protocol` and all connection work to ticket 004's `relay.channel`.

**Approach**
- Resolve the target (named robot's preferred relay, or any free relay)
  via `registry.client`/`registry.remote_client`'s `find` (role contains
  `RELAY`/`BRIDGE`, free), exactly as `mbserial` resolves a device.
- Branch on the resolved device's `host` field: `None` → lock locally
  (kind `relay`) via the local API, open a `LocalRelayChannel`; non-`None`
  → connect directly to the owning peer's `remote_api`, lock there, open
  a `RemoteRelayChannel` — same shape as `serial.cli`'s existing branch.
- On acquire: `RelayControl.reset_and_normalize()` (ticket 003).
- Look up the robot's channel/group via `registry.store`'s
  `resolve` (ticket 001) — CLI path treats "robot not in the name
  registry" as a distinct, reported error (not a silent derive, unlike
  robot-console's endpoint — see SUC-001's error flow).
- Send `!CG`, `!GO`, optional `PING`; hand off to an interactive
  terminal (raw-mode, `select`-based, escape character to quit) or run
  `--send`/`--expect` scripting — ported from
  `microbit-radio-relay/server/src/mbrelay/client.py`'s socket/terminal
  helpers and `tune_to_robot` sequencing, adapted to drive
  `relay.protocol` over `relay.channel` instead of a raw socket.
- On release: `clear_stored_config()` (`!DEFAULTS`), release the lock.
- Add light `mbrelay names get/set/clear/list` subcommands as a local
  wrapper over `registry.store`'s name-registry methods (ticket 001),
  for operator use without going through the HTTP compatibility endpoint.

**Files to create/modify**
- `src/mbtools/relay/cli.py` (new — `mbrelay connect`, `mbrelay tune`,
  `mbrelay names ...`).
- `pyproject.toml` (add the `mbrelay` console-script entry point).
- `tests/relay/test_cli.py` (new).

**Documentation updates**: `docs/wiki/` gets an `mbrelay` usage page
(new subcommand set), matching the existing `mbdeploy`/`mbserial` pages'
style.

## Acceptance Criteria

- [ ] `mbrelay connect <robot>` against a local relay resolves, locks,
      normalizes, tunes, and enters an interactive session, then restores
      defaults and unlocks on exit.
- [ ] `mbrelay connect <robot>@<host>` against a peer-owned relay does the
      same over the remote path, with no local port ever opened.
- [ ] `--send`/`--expect` scripting mode sends each line and optionally
      waits for a regex match, returning a shell-usable exit code.
- [ ] No free relay: reported distinctly from "robot not in the name
      registry" — two different, correctly-labeled error messages.
- [ ] `mbrelay names get/set/clear/list` round-trip against
      `registry.store`'s methods from ticket 001.
- [ ] `mbrelay connect` never enumerates or probes devices itself — every
      device access goes through a registry `find`/lock call (verified by
      asserting on the registry client mock in tests, not just by
      inspection).

## Testing

- **Existing tests to run**: `uv run pytest tests/serial/` (confirm the
  local/remote dispatch pattern this ticket mirrors is unaffected).
- **New tests to write**: `tests/relay/test_cli.py` — local and remote
  connect flows against fake registry clients and fake channels
  (ticket 004), `--send`/`--expect` scripting, `names` subcommands.
- **Verification command**: `uv run pytest tests/relay/`
