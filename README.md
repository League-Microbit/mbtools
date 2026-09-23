# mbtools

Tools for finding, flashing and talking to BBC micro:bits across a fleet of hosts.

| Program | Role |
|---|---|
| `mbregistry` | The one daemon: watches USB, identifies micro:bits, keeps the device database, grants exclusive locks, peers with other hosts |
| `mbdeploy` | Flash firmware to a micro:bit by name |
| `mbserial` | Raw serial connection to a micro:bit, local or remote |
| `mbrelay` | Talk to a micro:bit radio relay |

Status: early planning. See [docs/brief.md](docs/brief.md) for the design brief and
`clasi/issues/` for the work queue.

Successor to `mbdeploy` (Busboombot/mbdeploy) and the `mbrelay` server in
`microbit-radio-relay`.
