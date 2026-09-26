---
status: pending
---

# Move `mbregistry run` under `service` and add `service start`/`stop`

## Description

The daemon-lifecycle commands are split across two places in the
`mbregistry` CLI: `mbregistry run` (foreground daemon) is top-level,
while install/uninstall/status live under `mbregistry service`. Put all
of the daemon-lifecycle commands under `service`:

- `mbregistry service run`: the current `mbregistry run`, moved as-is
  with all its flags (`--socket`, `--remote-port`, `--peer-pub-port`,
  `--peer-snapshot-port`, `--peer`, etc.).
- `mbregistry service start`: start the installed service (launchd
  `bootstrap`/`kickstart`, `systemctl start`, Windows service start).
- `mbregistry service stop`: stop the installed service without
  uninstalling it.
- `mbregistry service status`: **already exists** (`cmd_service_status`,
  `src/mbtools/registry/cli.py`). Keep it and check that it fits with
  start/stop.

The top-level `mbregistry run` goes away. Decide during planning whether
to drop it outright or keep it as a hidden, deprecated alias for one
release, as `install-service` was handled in sprint 006 ticket 004.

## Things to update

- `src/mbtools/registry/cli.py`: parser restructure; new `cmd_service_start`/
  `cmd_service_stop`. `start`/`stop` probably need the same
  `--user`/`--system` scope choice as `install`/`uninstall`, or they could
  detect the scope that is installed.
- `src/mbtools/registry/service.py` / `service_windows.py`: the generated
  unit/plist/Windows service `ExecStart` currently invokes
  `mbregistry run ...`. Change it to `mbregistry service run ...`. Hosts
  that are already installed need a reinstall (or the alias) so their
  existing unit files keep working.
- `scripts/deploy-host.sh`, `scripts/deploy-test-host.sh`: these call
  `mbregistry run` and stop/start the service by hand
  (`systemctl`/`launchctl`). Switch them to the new commands.
- Docs: `README.md`, `docs/service.md`, `docs/migration.md`, and the
  `mbregistry run` references in the project `CLAUDE.md`.
- Tests: `tests/registry/cli/test_cli_run_peering.py` and any other test
  that parses `["run", ...]`.
- Check for other callers that spawn `mbregistry run` (for example
  robot-console's spawn/system-instance path).
