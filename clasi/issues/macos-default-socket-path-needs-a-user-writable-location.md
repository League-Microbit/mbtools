---
status: pending
---

# macOS: the default socket path is /run/mbregistry/api.sock, which doesn't exist

## Description

Found in sprint 005 hardware acceptance (`docs/acceptance/005-hardware.md`).

macOS has no `/run` directory, so `registry.paths.default_socket_path()` returns
a path that can never exist there. Every client command on a Mac (such as
braeburn) needs an explicit `--socket` or `$MBREGISTRY_SOCKET`.

## Decision needed first

Is macOS a supported daemon platform? This is spec open question #7.

- **If yes:** choose macOS defaults. For example, the socket and database
  under `~/Library/Application Support/mbtools/`, or `/usr/local/var/run` for
  a system-wide daemon. Add a launchd plist to `install-service`, the
  equivalent of the systemd unit.
- **If macOS is only a development platform:** still give it a sensible
  per-user default path, so clients work without flags.
