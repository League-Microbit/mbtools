---
id: '012'
title: 'mbdeploy: flash a peer-owned device by name (remote transport)'
status: done
use-cases:
- SUC-003
depends-on:
- '010'
- '011'
github-issue: ''
issue: mbdeploy-flash-by-name-remote.md
completes_issue: true
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbdeploy: flash a peer-owned device by name (remote transport)

## Description

Per sprint.md's Architecture (module `deploy.cli`, Step 5/UC-009/SUC-003),
extend `mbdeploy deploy <name>` to reach a peer-owned device. `_run_deploy`
already resolves `<name>` via the local registry as its first step; this
ticket adds the branch on `device.get("host")`: `None` keeps today's
exact local flow untouched (sprint 002, hardware-validated); non-`None`
uses the device's `endpoint` field (ticket 010) to open a
`remote_client.RemoteRegistryClient` (ticket 011) and drive the *same*
relay-guard/lock/flash/wait-for-reprobe/report sequence against it.

## Acceptance Criteria

- [x] `_run_deploy`'s resolve step branches on `device.get("host")`
      immediately after resolving `<name>`; every step after that
      (relay guard, `--hex`/`--repo` hex resolution, lock, flash, report)
      is shared code, not duplicated per branch, except for which client
      object (`RegistryClient` vs. `RemoteRegistryClient`) and which
      flash call (local `flash_hex` + `mark_flashed` vs. remote `flash`
      op, which already increments `flash_count` server-side per ticket
      008) is used.
- [x] The relay guard (`_is_relay`, `--force-relay`) runs identically for
      a remote target — refuses *before* locking or flashing anything,
      exactly as the local flow's existing acceptance criterion states.
- [x] `--hex`/`--repo` hex resolution is unchanged — the hex file is
      resolved locally (on the machine running `mbdeploy`) either way;
      for the remote path, the resolved hex's bytes/path are handed to
      `remote_client.flash()` per whatever wire shape ticket 008 defined
      for getting hex bytes to the owning registry.
- [x] `DeviceLockedError`'s message formatting
      (`f"{name} is locked for {holder.get('kind')} by pid
      {holder.get('pid')}"`) is extended to also show `holder.get('host')`
      when present (a remote holder's `pid` is `None` per ticket 006's
      response shape — the message must read sensibly for that case,
      e.g. naming the host instead of a null pid).
- [x] Wait-for-reprobe (`_wait_for_reprobe`) works against the remote
      client the same way it does locally — polls `find()` (now against
      the remote client) until `last_probe` advances and `state ==
      "connected"`, or times out, identical logic reused, not
      reimplemented.
- [x] Owning-host-unreachable mid-flash (UC-009's error flow): a
      `RegistryUnavailable` raised by the remote client at any point
      during the flow is caught and reported cleanly (not a stack
      trace), matching the existing `EXIT_NO_DAEMON`-style handling
      `cmd_deploy` already has for the local case.
- [x] `mbdeploy list` (unaffected by this ticket — already ticket 010's
      job) continues to show remote devices with their HOST column so a
      user can discover the `<name>` this ticket's flow resolves.

## Testing

- **Existing tests to run**: the full existing `mbdeploy deploy` test
  suite — must pass unchanged, proving the local branch is untouched.
- **New tests to write**:
  - `mbdeploy deploy <name>` against a fake local registry that resolves
    `<name>` to a `host`-tagged device, and a fake/real
    `RemoteRegistryClient` target — full flow: relay guard, lock, flash
    (streamed), wait-for-reprobe, success report.
  - Locked-remote-device error message includes host, not a null pid.
  - Owning-host-unreachable mid-flash reports cleanly, doesn't hang or
    crash.
  - Hardware-level end-to-end coverage is ticket 014's job, not this
    ticket's — this ticket's tests use fakes/loopback only.
- **Verification command**: `uv run pytest tests/deploy/`

## Implementation Notes

- **`deploy.cli` restructuring**: `_run_deploy` now only does step 1
  (resolve) and the `device.get("host")` branch; it calls a new
  `_run_deploy_flow(client, device, args)` for steps 2-8 (relay guard
  through report), shared byte-for-byte between the local and remote
  branches. The only per-branch code is inside a new `_flash(client,
  uid, hex_path, name, log)` helper, dispatched via `isinstance(client,
  RemoteRegistryClient)`: local runs `deploy.flash.flash_hex` then calls
  `client.mark_flashed`; remote calls `client.flash(...)` (the streamed
  wire op) and never calls `mark_flashed` (it doesn't exist on
  `RemoteRegistryClient`, and would double-count `flash_count` if it
  did). `_flash` returns `(success, exit_code)`, normalizing a local
  `int` return code and a remote `FlashResult` into one shape so the
  blank-board/generic failure reporting in `_run_deploy_flow` doesn't
  need to know which transport ran.
- **Holder message formatting**: new `_format_locked(name, holder)`
  replaces the inline f-string in the one `except DeviceLockedError`
  block `_run_deploy_flow` now has (shared by both branches). `holder`
  can carry a remote origin on *either* branch — not just when flashing
  a peer-owned device, but also when a *local* device is contested by
  someone connecting to this same registry's own `remote_api` — since
  `LockManager` is one process-wide table regardless of which listener
  (Unix socket or TCP) took the lock. A `host`-bearing holder reads "is
  locked for `<kind>` by a remote session on `<host>`"; a local holder
  (`host` absent) keeps the original "by pid `<pid>`" phrasing
  byte-for-byte (asserted by the pre-existing
  `test_already_locked_fails_fast_naming_holder`, unchanged).
- **Remote branch's endpoint parsing**: `device["endpoint"]` (ticket
  010's `host:remote_api_port` string) is split with `str.rpartition(":")`
  rather than `split`, in case a future IPv6 literal host ever appears
  in it. A missing/malformed endpoint (no matching peer row yet, or a
  non-numeric port) is reported as a plain `EXIT_ERROR` message naming
  the owning host, never a stack trace — deliberately conservative since
  it shouldn't happen in steady state (ticket 010's own note on the
  same condition server-side).
- **`RegistryUnavailable` scope**: the remote branch's own
  `try/except RegistryUnavailable` wraps the entire
  `with RemoteRegistryClient(...) as remote_client: return
  _run_deploy_flow(...)` statement, so a drop at connect time, mid-lock,
  mid-flash, mid-unlock, or mid-wait-for-reprobe are all caught in the
  same place with a message naming the *owning* host — deliberately
  separate from `cmd_deploy`'s own outer `RegistryUnavailable` handler
  (local-daemon-not-running wording), so the two failure modes never get
  each other's message.
- **No `--auth-token` support added**: out of this ticket's declared
  acceptance criteria; `RemoteRegistryClient` is always constructed with
  `auth_token=None`. A peer registry configured with Decision 6's shared
  token would reject an unauthenticated `mbdeploy deploy` against it
  with `unauthorized` (`RegistryClientError`, generic message) — no
  special-casing added for that response, consistent with "no new
  scope beyond the acceptance criteria."
- **Testing**: new file `tests/deploy/test_deploy_cli_remote.py` (basename
  unique across `tests/`, per project convention) — a *lightweight*
  owning-side fixture (`RemoteAPIServer` + seeded `Store`/`LockManager`,
  no `Daemon`) for relay guard, locked-by-remote-holder message, owning-
  host-unreachable (both at connect and mid-flash, the latter via a
  monkeypatched `RemoteRegistryClient.flash`), and flash success/failure
  against a real streamed remote `flash` op (`subprocess.Popen`
  monkeypatched, mirroring `tests/registry/remote_client/
  test_remote_client.py`'s own fake-pyocd pattern); one *daemon-backed*
  owning-side fixture (`Daemon` + `RemoteAPIServer` sharing a lock,
  scripted `FakeSerial` announcements, mirroring
  `test_deploy_cli.py`'s own `_run_daemon`) for the one genuine
  end-to-end success flow, proving a real post-unlock re-probe lands
  through the remote transport. 7 new tests, all passing;
  `tests/deploy/` in full: 89 passed, 1 skipped (pre-existing platform
  skip, unrelated) — the pre-existing local-only suite is unchanged.
  Full project suite: `uv run pytest -q` — 543 passed, 2 skipped, no
  failures.
