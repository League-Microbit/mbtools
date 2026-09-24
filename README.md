# mbtools

Tools for finding, flashing and talking to BBC micro:bits across a fleet of hosts.

| Program | Role |
|---|---|
| `mbregistry` | The one daemon: watches USB, identifies micro:bits, keeps the device database, grants exclusive locks, peers with other hosts |
| `mbdeploy` | Flash firmware to a micro:bit by name |
| `mbserial` | Raw serial connection to a micro:bit, local or remote |
| `mbrelay` | Talk to a micro:bit radio relay |

All four programs are implemented as of sprint 004. See
[docs/brief.md](docs/brief.md) for the original design brief,
[docs/design/](docs/design/) for the current architecture and API docs, and
`clasi/issues/`/`clasi/sprints/` for the work queue.

Successor to `mbdeploy` (Busboombot/mbdeploy) and the `mbrelay` server in
`microbit-radio-relay` — see [docs/migration.md](docs/migration.md) for the
fleet migration runbook that retires them.

## Usage

Install with [`uv`](https://docs.astral.sh/uv/) (`uv sync`, see
[Development](#development) below) or `pip install .` from a checkout.

On Linux, run `mbregistry install-service` once, as root, before running
`mbregistry` as a real daemon — it writes the systemd unit and a udev rule
(so a non-root user in the `plugdev` group can access the boards) and
prints the `systemctl`/`udevadm`/`usermod` follow-up commands; see
`mbregistry install-service --help`. On macOS, `mbregistry run` is only
ever run in the foreground — there is no `install-service` support there.
On Windows, `mbregistry install-service` registers it with the Service
Control Manager (`mbregistry run --windows-service` is what that service
invokes); Windows support has not been verified on real hardware.

### The four programs

Each program prints full usage with `--help`; below is each one's most
common invocation.

- `mbregistry list [--json]` — list every micro:bit the local daemon (and
  its peers) know about.
- `mbdeploy deploy <name> --repo OWNER/REPO[@TAG]` — flash the named
  device from a GitHub release (`--hex FILE` flashes a local `.hex` file
  instead).
- `mbserial <name>` — open an interactive serial session to the named
  device (add one or more words after `<name>` to send a single one-shot
  command instead of opening a session; `--reset` deliberately resets the
  board as part of connecting, which plain connecting does not do).
- `mbrelay connect <robot>[@host]` — connect to a robot over any free
  radio relay (local or, with `@host`, a specific peer's).

### Peering and remote access

`mbregistry` instances discover each other over mDNS and share their
device lists; `mbregistry run --peer HOST[:PORT]` adds an explicit peer
for a network mDNS can't reach. Once peered, `mbdeploy` and `mbserial`
resolve a device by name and automatically talk to whichever host's
registry owns it — no extra syntax is needed on those two. `mbrelay
connect` is the one place a host is named explicitly: `<robot>@<host>`
pins the session to a relay owned by that peer, instead of picking any
free relay.

### Ports

| Port | Purpose |
|---|---|
| (local API) | device query/control — a Unix socket on Linux/macOS, a named pipe on Windows |
| 7440 | remote (TCP) API — how a peer, or `mbdeploy`/`mbserial` talking to a remote device, reaches this host's registry |
| 7442 | peering ZeroMQ PUB (event bus) |
| 7443 | peering ZeroMQ snapshot REQ/REP |
| 7444 | robot-console-compatibility relay pool (`_mbrelay._tcp`) |
| 7445 | robot-console-compatibility names API |

`mbregistry` advertises itself over mDNS as `_mbregistry._tcp`; the
robot-console-compatibility relay pool advertises separately as
`_mbrelay._tcp`, for legacy `mbrelay`/robot-console clients that don't
speak the registry protocol.

An optional shared secret (`--auth-token`, or `$MBREGISTRY_TOKEN`) can be
required of remote-API and peering connections; it is unset (no auth) by
default fleet-wide.

## Development

Managed with [`uv`](https://docs.astral.sh/uv/); `pytest` is the test runner.

```sh
uv sync       # create/update the .venv and install mbtools (editable) + dev deps
uv run pytest  # run the test suite
```

`mbregistry`, `mbdeploy`, `mbserial`, and `mbrelay` are console scripts
registered in `pyproject.toml` and resolve via `uv run <name>` (or directly,
once `uv sync` has put `.venv/bin` on `PATH`). Only `mbregistry` is under
active development this sprint — the other three currently print a
"not yet implemented" message and exit non-zero.
