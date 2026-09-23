---
id: '012'
title: 'mbdeploy: flash a peer-owned device by name (remote transport)'
status: open
use-cases: [SUC-003]
depends-on: ['010', '011']
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

- [ ] `_run_deploy`'s resolve step branches on `device.get("host")`
      immediately after resolving `<name>`; every step after that
      (relay guard, `--hex`/`--repo` hex resolution, lock, flash, report)
      is shared code, not duplicated per branch, except for which client
      object (`RegistryClient` vs. `RemoteRegistryClient`) and which
      flash call (local `flash_hex` + `mark_flashed` vs. remote `flash`
      op, which already increments `flash_count` server-side per ticket
      008) is used.
- [ ] The relay guard (`_is_relay`, `--force-relay`) runs identically for
      a remote target — refuses *before* locking or flashing anything,
      exactly as the local flow's existing acceptance criterion states.
- [ ] `--hex`/`--repo` hex resolution is unchanged — the hex file is
      resolved locally (on the machine running `mbdeploy`) either way;
      for the remote path, the resolved hex's bytes/path are handed to
      `remote_client.flash()` per whatever wire shape ticket 008 defined
      for getting hex bytes to the owning registry.
- [ ] `DeviceLockedError`'s message formatting
      (`f"{name} is locked for {holder.get('kind')} by pid
      {holder.get('pid')}"`) is extended to also show `holder.get('host')`
      when present (a remote holder's `pid` is `None` per ticket 006's
      response shape — the message must read sensibly for that case,
      e.g. naming the host instead of a null pid).
- [ ] Wait-for-reprobe (`_wait_for_reprobe`) works against the remote
      client the same way it does locally — polls `find()` (now against
      the remote client) until `last_probe` advances and `state ==
      "connected"`, or times out, identical logic reused, not
      reimplemented.
- [ ] Owning-host-unreachable mid-flash (UC-009's error flow): a
      `RegistryUnavailable` raised by the remote client at any point
      during the flow is caught and reported cleanly (not a stack
      trace), matching the existing `EXIT_NO_DAEMON`-style handling
      `cmd_deploy` already has for the local case.
- [ ] `mbdeploy list` (unaffected by this ticket — already ticket 010's
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
