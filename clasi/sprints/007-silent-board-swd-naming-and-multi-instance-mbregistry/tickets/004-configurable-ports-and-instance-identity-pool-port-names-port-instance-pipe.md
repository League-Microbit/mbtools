---
id: '004'
title: 'Configurable ports and instance identity (--pool-port, --names-port, --instance,
  --pipe)'
status: open
use-cases: [SUC-004, SUC-006, SUC-008]
depends-on: []
github-issue: ''
issue: robot-console-on-mbregistry-multi-instance-and-spawn-support.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# Configurable ports and instance identity (--pool-port, --names-port, --instance, --pipe)

## Description

Make every `mbregistry run` listener's port configurable and every
advertised value the one actually bound, and give each instance its own
mDNS/peer identity — the design-doc's items 1 and 4
(`docs/design/robot-console-integration.md` §5). This ticket is
independent of tickets 001-003 (it touches `cli.py`/`peering.py`
call-sites/`api_windows.py`, not the probe/claim/state-model path) and
can be implemented in either order relative to them; it's sequenced
after 002/003 in the sprint only because the plan lists the
probe/claim pairing first, not because of a real code dependency.

**Ports**: add `--pool-port`/`$MBREGISTRY_POOL_PORT` and
`--names-port`/`$MBREGISTRY_NAMES_PORT`, following the exact
flag/env/default-resolution pattern `--remote-port`/
`$MBREGISTRY_REMOTE_PORT` already uses (`_resolve_int`, a
`_POOL_PORT_ENV_VAR`/`_NAMES_PORT_ENV_VAR` constant, an argparse
`add_argument`). Thread the resolved values into `assemble_relay_pool`'s
`port`/`names_api_port` and `assemble_names_api`'s `port`, replacing
their current hardcoded-default call sites in `_run_registry`. Fix the
mDNS advertisement to use each listener's **actually-bound** port
(`relay_pool.bound_port`, `names_api.bound_port` — both already exist as
properties per the existing `cmd_run` print-line code) in the
`_mbrelay._tcp` SRV record and its `registry=` TXT key, not the
requested/default value — this matters specifically for `0` (ephemeral),
where "requested" and "bound" differ by construction.

**Instance identity**: add `--instance NAME` (default: short hostname,
matching today's `_short_hostname()` behavior) and thread it into
`PeerDiscovery(host=...)` (in `assemble_registry`'s construction) and
`assemble_relay_pool(instance_host=...)` — both parameters already exist
on their respective classes/functions (confirmed by reading
`peering.py` and `relay_pool.py`; this ticket is CLI wiring, not new
peering logic). Also read/honor `$MBREGISTRY_INSTANCE` if you choose to
give this flag an env-var form too (the design doc doesn't specify one;
match the existing pattern for consistency).

**Windows pipe**: add `--pipe NAME` (Windows only). Parameterize
`api_windows`'s pipe name — it currently reads `DEFAULT_PIPE_NAME` from
`registry.paths.default_pipe_name()` as a fixed value; thread an
optional override through from `cli.py` down to wherever the pipe is
created/opened, keeping `DEFAULT_PIPE_NAME`/`default_pipe_name()` as the
fallback when `--pipe` is omitted.

## Acceptance Criteria

- [ ] `--pool-port 0`/`--names-port 0` each bind an ephemeral port; the
      `_mbrelay._tcp` SRV port and `registry=` TXT value reflect the
      real bound port, not `0`.
- [ ] An explicit non-zero `--pool-port`/`--names-port` value is bound
      and advertised as given.
- [ ] `$MBREGISTRY_POOL_PORT`/`$MBREGISTRY_NAMES_PORT` behave
      identically to their flag counterparts; the flag wins if both are
      given (matching `--remote-port`'s existing precedent — verify
      against `_resolve_int`'s actual precedence rule rather than
      assuming).
- [ ] `--instance NAME` changes the `_mbregistry._tcp`/`_mbrelay._tcp`
      mDNS instance name and `peer.host`/`device.host`, verified with a
      mocked-zeroconf unit test asserting the `ServiceInfo` name/server
      fields use the given instance name.
- [ ] Omitting `--instance` preserves today's short-hostname default
      exactly (a regression test against the pre-ticket behavior).
- [ ] `--pipe NAME` (Windows) overrides the named-pipe transport's name;
      covered by a unit test exercising `api_windows`'s pipe-name
      resolution without real Windows hardware (matching that module's
      existing test conventions).
- [ ] Omitting `--pipe` preserves `DEFAULT_PIPE_NAME`'s current fixed
      value.

## Implementation Plan

**Approach**: Ports first (smallest, most mechanical, closest precedent
to copy), then instance identity (wiring only, since the receiving
parameters already exist), then the Windows pipe name.

**Files to modify**:
- `src/mbtools/registry/cli.py`: `--pool-port`, `--names-port`,
  `--instance`, `--pipe` argparse additions; `_run_registry`'s
  `assemble_relay_pool`/`assemble_names_api`/`assemble_registry` calls
  updated to pass the resolved values; the mDNS-advertisement print/log
  line (and any TXT/SRV construction inside `relay_pool.py`/`peering.py`
  that currently reads a passed-in port rather than a bound one) audited
  for "advertise what's bound, not what's requested."
- `src/mbtools/registry/console_compat/relay_pool.py`: only if its own
  `registry=` TXT construction needs to read `names_api`'s bound port
  rather than a passed-through configured value — check at
  implementation time; may already do the right thing once `cli.py`
  passes the resolved (not necessarily bound) `names_api_port` through,
  in which case only the *ephemeral* (`0`) case needs the fix described
  above.
- `src/mbtools/registry/api_windows.py`: parameterize the pipe name
  (constructor/call parameter), keep `DEFAULT_PIPE_NAME` as the default.
- `src/mbtools/registry/paths.py`: no change expected (`default_pipe_name`
  stays as the default-value source).

**Testing plan**:
- `tests/registry/cli/`: new tests for `--pool-port`/`--names-port`
  flag+env+default resolution, `--instance` flag+default, `--pipe`
  flag+default, following the existing `--remote-port` test file's
  pattern exactly (same file or a sibling, implementer's call).
- `tests/registry/peering/` or `tests/registry/console_compat/`: mocked-
  zeroconf test asserting `--instance`'s value reaches
  `ServiceInfo`'s name/server fields for both `_mbregistry._tcp` and
  `_mbrelay._tcp`.
- `tests/registry/api_windows/`: pipe-name override test.
- An ephemeral-port-advertises-bound-value test (mocked zeroconf,
  `port=0` given, asserting the TXT/SRV record carries the real bound
  port read from `relay_pool.bound_port`/`names_api.bound_port`, not
  `0`).
- Run: `uv run pytest tests/registry/cli/ tests/registry/peering/
  tests/registry/console_compat/ tests/registry/api_windows/ -x`.

**Documentation updates**: none in this ticket — ticket 006 covers docs
for the whole sprint.
