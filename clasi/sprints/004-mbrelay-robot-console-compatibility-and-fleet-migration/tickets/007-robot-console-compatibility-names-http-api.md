---
id: '007'
title: 'robot-console compatibility: names HTTP API'
status: open
use-cases: [SUC-003]
depends-on: ['001']
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

- [ ] `GET /names/<unseen-name>` returns `200` with a derived
      `(channel, group)` and `source: "derived"`, and the entry is now
      persisted (a repeat `GET` returns the identical value, still
      `"derived"`, not recomputed).
- [ ] `PUT /names/<name>` with a valid body persists a `source:
      "registry"` entry; a subsequent `GET` returns it unchanged.
- [ ] `DELETE /names/<name>` clears an explicit entry; a subsequent `GET`
      re-derives (`source: "derived"` again).
- [ ] Response JSON shape is exactly `{"channel": int, "group": int,
      "source": string}` — cross-checked field-by-field against
      `mbrelayRegistry.ts`'s parsing code, not assumed from the brief
      alone.
- [ ] `GET` is idempotent-safe for repeated robot-console polling within
      its documented cache TTL — no unbounded growth or side effect
      beyond the first derive.
- [ ] This listener is exempt from the `--auth-token` check, matching
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
