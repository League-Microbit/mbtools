---
id: '005'
title: mbrelay CLI
status: done
use-cases:
- SUC-001
depends-on:
- '001'
- '004'
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

- [x] `mbrelay connect <robot>` against a local relay resolves, locks,
      normalizes, tunes, and enters an interactive session, then restores
      defaults and unlocks on exit.
- [x] `mbrelay connect <robot>@<host>` against a peer-owned relay does the
      same over the remote path, with no local port ever opened.
- [x] `--send`/`--expect` scripting mode sends each line and optionally
      waits for a regex match, returning a shell-usable exit code.
- [x] No free relay: reported distinctly from "robot not in the name
      registry" — two different, correctly-labeled error messages.
- [x] `mbrelay names get/set/clear/list` round-trip against
      `registry.store`'s methods from ticket 001.
- [x] `mbrelay connect` never enumerates or probes devices itself — every
      device access goes through a registry `find`/lock call (verified by
      asserting on the registry client mock in tests, not just by
      inspection).

## Implementation Notes

Interface decisions later tickets (006 relay pool, 007 names HTTP API)
should know about:

- **Never calls `RelayControl.reset_and_normalize()` for the connect
  session itself** — a deliberate deviation from this ticket's own
  Approach text, which names that method for "on acquire". That
  convenience wrapper (ticket 003) opens the channel, does its work, and
  *unconditionally closes it again* in its own `finally`. For
  `LocalRelayChannel` that's harmless (open/close cycles freely, ticket
  004's own notes), but `RemoteRelayChannel` is single-use — closing it
  tears down the underlying `RemoteStream`'s TCP connection for good, and
  `remote_api`'s own "lock release on connection close" contract means
  that close *also drops this session's relay lock*, mid-flow, before
  `!CG`/`!GO`/the interactive session ever run. `relay.cli` instead calls
  `RelayControl`'s smaller building blocks directly (`hello`,
  `firmware_version`, `normalize`) against one channel opened once and
  closed once for the whole session (acquire through release). Release
  mirrors the ticket's Approach exactly: `clear_stored_config` (
  `!DEFAULTS`) only, no second normalize — the *next* acquire's own
  `normalize()` already forces the board back to defaults
  unconditionally. One subtlety this cost an hour to find: the
  interactive/script phase re-points the channel's read callback from
  the acquire-time `Reader` to a plain `queue.Queue` (`channel.
  start_reading` *replaces*, not adds to, `on_data`/`on_error` — both
  adapters' own contract), so release's own `clear_stored_config` call
  must re-point the channel back at the original `Reader` first, or its
  `!DEFAULTS` reply silently lands in the now-unread data-plane queue
  and the call just burns its own timeout for nothing. `_run_session`'s
  `finally` block does this explicitly, with a comment — worth grepping
  for if a future ticket touches this handoff.
- **Name-registry replication wiring (ticket 002's own gap) lands on the
  registry daemon's local API, not in this CLI.** Ticket 002's
  Implementation Notes named this ticket as one of two intended callers
  of `PeerDiscovery.publish_name_set`/`publish_name_clear`, phrased as
  "after their own `Store.set()`/`Store.resolve()`/`Store.clear()`
  calls" — i.e. implying the CLI would touch `Store` directly. This
  ticket does **not** do that: `mbtools.relay.cli` never imports
  `registry.store`/`registry.peering` at all. Instead, four new ops
  (`names_get`/`names_set`/`names_clear`/`names_list`) were added to
  `registry._api_base.BaseAPIServer` (shared by `registry.api.
  RegistryAPIServer`; **not** wired into `registry.remote_api.
  RemoteAPIServer`'s dispatch — see below), backed by two new optional
  `RegistryAPIServer` constructor callbacks (`name_set_callback`/
  `name_clear_callback`, mirroring `LockManager`'s own
  `lock_display_callback` shape: fired by the component that owns the
  write, right after it commits, never by `Store` itself). `registry.cli
  .assemble_registry` wires these to `PeerDiscovery.publish_name_set`/
  `publish_name_clear`, exactly like `event_callback`/
  `lock_display_callback` are already wired for daemon/lock events. This
  keeps the "a client talks to its registry, never opens `devices.db`
  itself" rule (`registry.api`'s own module docstring) intact for
  `mbrelay`, the same as every other client tool in this project, and
  means the daemon (which already owns both `Store` and `PeerDiscovery`)
  is the one and only place a `name_registry` write gets published from,
  regardless of which client (`mbrelay names set`, `mbrelay connect`'s
  own derive-free lookup — actually non-mutating, see below — or
  ticket 007's HTTP endpoint) triggered it. `names_get` is deliberately
  the **non-creating** lookup (`Store.get_name`), not `Store.resolve`
  — ticket 007's `GET /names/<name>` (robot-console's write-on-read
  contract) will need `Store.resolve`'s derive-and-persist semantics
  instead, which is a **different** op this ticket did not add (no
  caller needs it yet); ticket 007 should add its own `names_resolve` op
  (or equivalent) rather than repurposing `names_get`.
- **Not wired into `remote_api.RemoteAPIServer`'s dispatch.** `mbrelay
  connect`'s name lookup always goes through the *local* registry
  connection, even when the relay itself is remote — ticket 002's
  replication already keeps every peer's `name_registry` converged, so
  there's no reason to open a second connection to a remote registry to
  ask the same question. A future ticket adding a remote caller extends
  `BaseAPIServer` (already shared) into `remote_api.py`'s own dispatch,
  not a re-port.
- **`mbrelay connect <robot>` resolves a *robot* name, not a relay
  device token** (unlike `mbserial <target>`, which names the device
  directly). The relay is picked automatically via the local registry
  client's `list` op, filtered client-side for `role` containing
  `RELAY`/`BRIDGE` and `lock_kind is None`, optionally scoped to
  `@host`; a local relay sorts first when `@host` isn't given. This is a
  `list`-based scan, not a new server-side filtered-`find` op — the
  acceptance criterion's "never enumerates or probes devices itself"
  guards against raw hardware access, not against calling `list()`
  (itself a registry op, proven in tests by a `FakeRegistryClient` whose
  `find()` raises if ever called, and whose `list()`/`lock()`/`unlock()`
  calls are asserted directly).
- **Two distinct errors, distinct exit codes**: "no free relay" is
  `EXIT_NO_DEVICE` (device-not-available class); "robot not in the name
  registry" is `EXIT_ERROR` (a data/config problem, not a device
  problem) — chosen so a caller can branch on exit code alone, not just
  message text, even though the acceptance criterion only requires the
  latter.
- **`docs/wiki/`** does not exist in this repository (unlike the
  `mbdeploy` repo the ticket's Documentation-updates note was modeled
  on) — there is no `mbdeploy`/`mbserial` usage page here to match the
  style of. Documented the finished CLI shape instead in
  `docs/design/specification.md` §6 (already had a forward-looking
  `mbrelay` section from pre-sprint planning; refined it to match what
  was actually built) and `docs/design/registry-api.md` (new "Name-
  registry ops" section for the four new wire ops). If this project
  gains a `docs/wiki/` publishing setup later, an `mbrelay` usage page
  should be added there too, matching whatever `mbdeploy`/`mbserial`
  pages exist by then.

## Testing

- **Existing tests to run**: `uv run pytest tests/serial/` (confirm the
  local/remote dispatch pattern this ticket mirrors is unaffected).
- **New tests to write**: `tests/relay/test_cli.py` — local and remote
  connect flows against fake registry clients and fake channels
  (ticket 004), `--send`/`--expect` scripting, `names` subcommands.
- **Verification command**: `uv run pytest tests/relay/`
