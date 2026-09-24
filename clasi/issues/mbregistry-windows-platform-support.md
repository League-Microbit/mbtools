---
status: pending
split_from: mbregistry-device-registry-daemon.md
sprint: '005'
---

# mbregistry: Windows platform support (USB event watch + service install)

# mbregistry: Windows platform support (USB event watch + service install)

## Description

Bring `mbregistry` up on Windows, split out of
`mbregistry-device-registry-daemon.md` because the core daemon (sprint 1)
is scoped Linux-first, with the Windows event source and Windows service
install deferred to this issue. Both pieces are required before
`mbregistry` is a genuine second supported daemon platform (brief §3.1,
spec §"Platforms" open decision).

## Scope

- **USB watch (Windows).** A real Windows attach/detach event source,
  behind the same interface the Linux polling/event implementation uses,
  so `mbregistry`'s core identification and database logic is unchanged.
- **Service install (Windows).** Register `mbregistry` as a Windows
  service (Service Control Manager), with restart-on-failure equivalent to
  the systemd `Restart=` policy used on Linux.
- **Named pipe query service.** The Windows equivalent of the Unix socket
  local query API (brief §3.5 / spec §3.5), so `mbregistry list` and other
  local clients work unchanged on Windows.
- File paths under the Windows equivalent of `/var/lib` and `/run` (see
  brief §9.2 — `ProgramData` or similar), as an ASSUMPTION for stakeholder
  confirmation.

## Port from

N/A — new platform code; no existing Windows implementation to port from
either source repo.

## Depends on

`mbregistry-device-registry-daemon.md` (sprint 1) — this issue extends
that daemon's interface rather than replacing it.
