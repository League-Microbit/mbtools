# mbtools — overview

`mbtools` (`League-Microbit/mbtools`) is a rewrite of two competing tools
into one coherent system for finding, identifying, flashing and talking to
micro:bit boards. It replaces the `Busboombot/mbdeploy` repo and the
`microbit-radio-relay/server` program.

## The problem it fixes

`mbdeploy` (flashing + a per-board serial/flash daemon, `mbdeploy serve`)
and `mbrelay` (a pool of radio-relay boards + a name registry) each
independently enumerate, probe and reset every micro:bit on a host. Run
both on one machine and they fight: they compete for serial ports, ignore
each other's locks, reset boards out from under each other, and write
into live sessions they don't own. The fix is architectural, not a bigger
retry loop: **enumeration, identification and exclusive access belong to
exactly one daemon.** Everything else is a client of it.

## The one-daemon principle

> "I prefer it if we could avoid having any but one server here, one
> daemon... all the other tools were primarily local in that they operated
> on remote things by connecting to a single common remote server, which
> would be MB Registry."

There is no `mbdeploy serve` and no `mbrelay serve` in `mbtools`. A tool
acting on a *remote* board connects to **that remote host's own
`mbregistry`** — not to a peer-to-peer protocol of its own, and not by
opening the device directly.

## The four programs

| Program | Kind | Job |
|---|---|---|
| **`mbregistry`** | The only daemon (systemd / Windows service, restart on failure) | Watches USB attach/detach, identifies micro:bits with the least intrusive probe that works, keeps the device database, grants exclusive per-device locks, and is the one network endpoint every other host talks to. Peers with other `mbregistry` instances over mDNS + ZeroMQ so a device on any host is visible everywhere. |
| **`mbdeploy`** | Client tool | Flashes firmware to a micro:bit by name — resolves the name through the registry (local or remote), locks the device, flashes, verifies, and waits for the registry's post-flash re-probe. Owns all the "fancy" parts: pyOCD diagnostics, retries, build integration, the CLI/UX. |
| **`mbserial`** | Client tool | Hands the caller a raw serial connection to a named board, local or remote, without rebooting it, after taking the registry's lock. |
| **`mbrelay`** | Client tool | Connects to a RADIORELAY/RADIOBRIDGE board through the registry (which one is a relay, is it free) and speaks the existing relay protocol — send/receive radio messages — exactly as today's `mbrelay` does. Does no enumeration, probing or listing of its own. |

## How they relate

```
        mbdeploy      mbserial      mbrelay
            \             |            /
             \            |           /
              v           v          v
                     mbregistry  (local daemon)
                     - USB watch, identify
                     - device database
                     - exclusive locks
                     - minimal flash op
                          |
                          | mDNS discovery + ZeroMQ event bus
                          v
                 mbregistry on peer host  <-- another host's
                 (same shape)                 mbdeploy/mbserial/mbrelay
                                               talk to it, not to the
                                               remote board directly
```

Every client tool takes the same path: resolve the target board through
*some* `mbregistry` (its own, or a peer's over the network), take an
exclusive lock scoped to that operation, do its work, release. No client
opens a serial port or SWD connection to a board that isn't local to the
`mbregistry` it's talking to.

## What it replaces

- **`Busboombot/mbdeploy`** — its device layer, pyOCD flash logic,
  registry concept, mDNS advertising and console/relay code are ported
  into `mbregistry`, `mbdeploy` and `mbserial`. `mbdeploy serve` is
  retired; its job moves into `mbregistry`.
- **`microbit-radio-relay/server`** — its relay protocol client, session
  normalization, and name registry are ported into `mbrelay` and (for the
  name table) `mbregistry`. The relay *firmware* source (`source/relay/`)
  stays in `microbit-radio-relay`; only the server/client side moves.

## Target platforms

- **`mbregistry` (the daemon):** Linux and Windows are required — USB
  watch, service install (systemd / Windows service), and the fleet's
  actual deployment targets (Pi Zero nodes, Windows dev boxes). macOS
  support is a development convenience the stakeholder has not committed
  to as a supported daemon platform (see Open Decisions in
  `specification.md`).
- **Client tools (`mbdeploy`, `mbserial`, `mbrelay`):** run wherever
  `mbregistry` runs, plus macOS for local development, since they can
  operate purely as clients of a registry (local or remote) without
  needing a service install of their own.

## Where to go next

- `specification.md` — the full spec, organized by program plus
  cross-cutting concerns (identity, announcements, locking, reset
  semantics, relay protocol, robot-console compatibility, migration), and
  the open decisions the stakeholder has not yet made.
- `usecases.md` — numbered use cases (UC-001…) covering attach/detach,
  locking, flashing, remote serial, relay connect, peer discovery, and
  service install.
