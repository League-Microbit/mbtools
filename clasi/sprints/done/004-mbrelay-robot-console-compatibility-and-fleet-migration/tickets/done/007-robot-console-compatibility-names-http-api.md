---
id: '007'
title: 'robot-console compatibility: names HTTP API'
status: done
use-cases:
- SUC-003
depends-on:
- '001'
github-issue: ''
issue: mbrelay-relay-protocol-client-over-mbregistry.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# robot-console compatibility: names HTTP API

## Description

Host the `GET/PUT/DELETE /names/<name>` contract inside `mbregistry`
(architecture Decision 1), backed by ticket 001's `Store` name-registry
methods. robot-console only ever calls `GET` (Part 1 research confirmed
this as an enforced policy in `mbrelayRegistry.ts`), but the issue asks
for full CRUD to preserve parity with the original contract for
admin/tooling use (`mbrelay names set/clear`, ticket 005).

**Approach**
- New module `registry.console_compat.names_api`: a small HTTP listener
  (new default port, distinct from legacy 8761 — Decision 6).
- `GET /names/<name>` → `Store.resolve(name)` — derives and **persists**
  a `source="derived"` entry on first ask; this write-on-read semantic is
  intentional and matches robot-console's own documented understanding
  (`mbrelayRegistry.ts`'s "write-on-read trap" comment, confirmed in Part
  1 research: robot-console never treats a 200 as proof the registry
  "knew" the mapping, precisely because of this).
- `PUT /names/<name>` with body `{"channel": N, "group": N}` →
  `Store.set(name, channel, group)`.
- `DELETE /names/<name>` → `Store.clear(name)`.
- Response shape for all three: `{"channel": int, "group": int,
  "source": "derived"|"registry"}` — the exact field names and types
  `mbrelayRegistry.ts` parses (confirmed in Part 1 research: `channel`/
  `group`/`source` as a flat object, `source` a string).
- Malformed name / out-of-range channel or group on `PUT`: `400`, not a
  silent clamp.

**Files to create/modify**
- `src/mbtools/registry/console_compat/names_api.py` (new).
- `src/mbtools/registry/cli.py` (extended — assembles `names_api`
  alongside `relay_pool`, sharing the port advertised in `relay_pool`'s
  TXT record).
- `tests/registry/console_compat/test_names_api.py` (new).

**Documentation updates**: `docs/wiki/` gets a short note alongside
ticket 006's (no machine specifics — port numbers/hosts stay off the
public wiki per this project's docs rule).

## Acceptance Criteria

- [x] `GET /names/<unseen-name>` returns `200` with a derived
      `(channel, group)` and `source: "derived"`, and the entry is now
      persisted (a repeat `GET` returns the identical value, still
      `"derived"`, not recomputed).
- [x] `PUT /names/<name>` with a valid body persists a `source:
      "registry"` entry; a subsequent `GET` returns it unchanged.
- [x] `DELETE /names/<name>` clears an explicit entry; a subsequent `GET`
      re-derives (`source: "derived"` again).
- [x] Response JSON shape is exactly `{"channel": int, "group": int,
      "source": string}` — cross-checked field-by-field against
      `mbrelayRegistry.ts`'s parsing code, not assumed from the brief
      alone.
- [x] `GET` is idempotent-safe for repeated robot-console polling within
      its documented cache TTL — no unbounded growth or side effect
      beyond the first derive.
- [x] This listener is exempt from the `--auth-token` check, matching
      ticket 006 and legacy `mbrelay`'s own no-auth posture for this
      surface.

## Testing

- **Existing tests to run**: `uv run pytest
  tests/registry/test_store_name_registry.py` (ticket 001 — confirm the
  HTTP layer doesn't duplicate or diverge from `Store`'s own semantics).
- **New tests to write**:
  `tests/registry/console_compat/test_names_api.py` — GET/PUT/DELETE
  against a real loopback HTTP server, response-shape assertions matched
  field-by-field to the documented robot-console contract, malformed-body
  400 case.
- **Verification command**: `uv run pytest
  tests/registry/console_compat/test_names_api.py`

## Implementation Notes

Interface decisions later tickets (011 real-hardware acceptance
especially) depend on:

- **Module/class**: `mbtools.registry.console_compat.names_api.NamesAPI`
  — built with `http.server.ThreadingHTTPServer`/`BaseHTTPRequestHandler`
  (stdlib only, no new dependency, consistent with this project's own
  minimal-dependency posture), not a hand-rolled byte-level parser like
  legacy `httpapi.py`'s. `start()`/`stop()`/`bound_port`/context-manager
  shape mirrors `RelayPool` exactly (same lifecycle contract, same
  `port=0` ephemeral-port test seam).
- **Port**: `NamesAPI`'s own `DEFAULT_PORT` is
  `relay_pool.DEFAULT_NAMES_API_PORT` (`7445`) re-exported, not
  redeclared — per ticket 006's own Implementation Notes instruction.
  `registry.cli.assemble_names_api` forwards it unchanged; there is no
  `--names-api-port` flag (same "out of this ticket's scope" call ticket
  006 made for `--relay-pool-port`).
- **Always started, no opt-out flag** — unlike `relay_pool`'s
  `--no-relay-pool` (which exists because a host may have no local relay
  *hardware*), `names_api` only ever touches `store`, so there is no
  host-specific reason to disable it. Wired into `cmd_run` right after
  `relay_pool`, started after it, stopped before it in the `finally`
  block (order between the two doesn't matter — neither depends on the
  other — but both must stop before `store.close()`).
- **Replication is asymmetric between the three verbs**: `PUT`/`DELETE`
  always publish (an explicit write is always a real change); `GET`
  publishes **only** when it actually derived-and-persisted a new row
  this call — detected with a `Store.get_name()` check immediately
  before the `Store.resolve()` call, both under the shared lock. A repeat
  `GET` for an already-known name has zero side effect, replication
  included, matching this ticket's own "no unbounded growth or side
  effect beyond the first derive" acceptance criterion. This is a
  deliberate difference from the local-socket `names_get` op, which never
  publishes at all (it isn't creating — it's `Store.get_name`, the
  non-mutating lookup, per `registry-api.md`).
- **`DELETE` re-derives and returns the fresh value**, matching legacy
  `mbrelay`'s own `NameRegistry.clear() -> self.resolve(name)`
  (`microbit-radio-relay/server/src/mbrelay/registry.py`) — chosen so
  "Response shape for all three" (the ticket's own Approach text) has a
  real entry to report even right after a clear, rather than returning
  nothing to shape. This means a `DELETE` publishes *two* events (a
  clear, then a set for the immediate re-derive) — both fired outside the
  lock. The acceptance criterion itself ("a subsequent GET re-derives")
  holds either way this was implemented; this module's choice is
  legacy-parity plus response-shape completeness, not something the
  tests force one way or the other.
- **Channel/group range validation is new to this module, ported
  conceptually from legacy `mbrelay`'s `registry.validate_pair`**
  (`CHANNEL_MIN, CHANNEL_MAX = 0, 83`; `GROUP_MIN, GROUP_MAX = 0, 255` —
  "what `!CG <ch> <group>` accepts (RadioRelay.cpp)", per that module's
  own comment). Nothing in `mbtools` had ported this check before this
  ticket (`mbrelay names set`'s own CLI path has no range check either,
  confirmed by reading `relay/cli.py`'s `cmd_names_set` — out of this
  ticket's scope to fix, flagged here in case a later ticket wants
  parity there too). Deliberately **not**
  `mbtools.relay.naming.CHANNEL_MIN`/`CHANNEL_MAX`/`GROUP_MIN`/
  `GROUP_MAX` (`11-83`/`15-255`) — that module's constants describe only
  the *derived* bijection's own narrower subrange; a registry override
  may use anything `!CG` accepts, per legacy `registry.py`'s own module
  docstring, so this module's own, wider range applies only to `PUT`.
- **Malformed name is `400` on all three verbs**, not just `PUT` as the
  acceptance criteria's own wording emphasizes — `Store`'s
  `get_name`/`resolve`/`set`/`clear` all raise `ValueError` via
  `mbtools.relay.naming.validate` for a name that isn't a well-formed
  five-letter micro:bit name, and this module maps that uniformly to
  `400` rather than letting it surface as a `500`.
- **Deviation from the ticket's own path for "Existing tests to run"**:
  the ticket lists `tests/registry/test_store_name_registry.py`, but
  ticket 001 actually placed that file at
  `tests/registry/store/test_store_name_registry.py`. Run (and passing):
  `uv run pytest tests/registry/store/test_store_name_registry.py
  tests/registry/console_compat/test_names_api.py` (78 tests), plus the
  full `tests/registry/` directory (492 passed, 1 skipped) and the whole
  suite (735 passed, 2 skipped).
- **`docs/wiki/` still does not exist in this repository** (same
  situation ticket 006 already documented) — updated
  `docs/design/specification.md` §6.6/§6-open-decisions and
  `docs/design/registry-api.md` instead, describing the concrete
  `GET/PUT/DELETE /names/<name>` contract this module now serves
  alongside the pre-existing Unix-socket `names_*` ops documentation.
