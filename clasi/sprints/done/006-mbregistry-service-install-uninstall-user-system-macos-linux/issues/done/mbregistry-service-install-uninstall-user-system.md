---
status: done
sprint: '006'
tickets:
- 006-001
- 006-003
- 006-004
- 006-002
---

# mbregistry service install/uninstall with --user / --system (macOS + Linux)

## Summary

Replace `mbregistry install-service` with a `service` command group that
actually installs, starts, and cleanly removes the daemon, at either user
or system scope, on macOS and Linux.

```
mbregistry service install   (--user | --system) [--dry-run]
mbregistry service uninstall (--user | --system) [--purge]
mbregistry service status
```

`--user` / `--system` is a required, mutually exclusive choice — there is
no default scope.

## Behaviour per platform

| | `--user` | `--system` (needs sudo) |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/org.jointheleague.mbregistry.plist`, `launchctl bootstrap gui/$UID` (starts at login) | LaunchDaemon `/Library/LaunchDaemons/org.jointheleague.mbregistry.plist`, `launchctl bootstrap system` (starts at boot) |
| Linux | systemd user unit in `~/.config/systemd/user/`, `systemctl --user enable --now`, `loginctl enable-linger` | unit in `/etc/systemd/system/`, udev rule, `daemon-reload`, `enable --now`, add the operator (`$SUDO_USER`) to `plugdev` |

- **install** writes the files *and* loads/starts the service (a change
  from today's `install-service`, which only writes files and prints
  commands). `--dry-run` prints what would be written and run, touching
  nothing. Re-running install is idempotent (rewrites files, reloads).
- The plist content comes from what docs/service.md §7 documents by hand
  today; that section becomes "run `mbregistry service install`".
- **Linux `--user` and USB access:** a lingering user service has no
  logind seat, so the udev `uaccess` tag grants nothing — the user must be
  in `plugdev` and the udev rule must exist. Both need root. If either is
  missing, `install --user` prints the exact sudo commands and stops
  rather than installing a daemon that can't open the boards.
- **Linux `--system`:** add the operator to `plugdev` automatically (the
  daemon runs as root, but this lets the operator use mbdeploy/mbserial
  directly too); print a note that it takes effect on next login.

## Uninstall

- Stops and unloads the service, removes the plist/unit, the udev rule
  (system scope), the socket and log files, and runs `daemon-reload`
  where applicable. Does not remove the operator from `plugdev`.
- **Keeps `devices.db`** (it holds the name registry) unless `--purge`
  is given, in which case the state directory is removed as well.
- If nothing is installed at the requested scope, it says so and exits
  0 (safe to run repeatedly, e.g. from Ansible). If an install exists at
  the *other* scope, it says so ("not installed for user; a system
  install exists — use --system").

## status

Reports, for both scopes, whether the service files exist, whether the
service is loaded/running, and the paths in use.

## Windows

Out of scope. `mbregistry service ...` on Windows exits with a clear
"not supported on Windows" error. Existing Windows code
(`service_windows.py`, `run --windows-service`) is left as-is for now.

## Migration

Remove `install-service` or keep it as a hidden alias for one release that
maps to `service install --system --dry-run` behaviour. Update
docs/service.md and README accordingly.

## Testing

Unit tests render plist/unit content and mock `launchctl`/`systemctl`/
`usermod` calls; no real service manager in the automated suite.
Hardware/host check: install/uninstall both scopes on a Mac and a Linux
node by hand.
