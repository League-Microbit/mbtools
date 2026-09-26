# Fleet migration runbook: retiring `mbdeploy serve`/`mbrelay.service`

This is the stakeholder's own operator runbook for retiring the old
`mbdeploy serve` daemon (`mbdeploy.service`) and the legacy relay server
(`mbrelay.service`, from `League-Robotics/microbit-radio-relay`) on the
rest of the fleet, and bringing each host up on `mbtools`' `mbregistry`
instead. It is executed by the stakeholder, on their own schedule — no
ticket in sprint 005 runs any step below against a production host except
where explicitly marked "already done" (`torture`, below).

## What this sprint did and did not do

- **Did**: build the idempotent per-host deployment tooling
  (`scripts/deploy-host.sh`, ticket 007) this runbook uses as its
  mechanism, exercise it against the five dedicated hardware test hosts
  (`meili`, `loki`, `hodr`, `magni`, `braeburn`), fix a real
  ownership-race defect the tooling's own hardware pass exposed (ticket
  011), and — as an explicit, stakeholder-authorized scope exception,
  not an application of this runbook — cut `torture` over from the
  legacy `mbrelay.service` to `mbregistry` (ticket 007's own session; see
  "`torture`: already migrated" below for the exact story and why it's
  recorded here rather than left to sprint history).
- **Did not**: execute this runbook against any other production host.
  Every host below `torture` in the fleet — any remaining node still
  running `mbdeploy serve` and/or legacy `mbrelay.service` — is
  untouched by this sprint. Running the steps below against them is the
  stakeholder's own action, on their own schedule, using this document.
- **Did not**: modify `Busboombot/mbdeploy` (its Ansible roles, its
  golden Pi Zero image, or its published docs), `League-Robotics/
  microbit-radio-relay`, or `robot-console` — see "Other repositories
  to update" below for what needs to change there, described but not
  executed.

## Prerequisites

Before cutting over **any** host, sprint 004's `mbrelay`/robot-console-
compatibility work must already be running and verified on at least one
host — this is sprint 004's own stated dependency for this whole
migration, restated here so this document is self-contained. That
condition is already satisfied: `docs/acceptance/004-hardware.md`
Scenario 4 verified the compatibility contract (mDNS `_mbrelay._tcp`
advertisement with TXT `registry=<port>`, pool-port reset-by-reconnect,
`/names` REST API) directly against `robot-console`'s own source on all
five hardware test hosts, and sprint 005 ticket 011 additionally
hardware-verified it on `torture`'s real relay pool (see below). Nothing
further needs to be re-verified before starting; the robot-console
verification checklist later in this document is the *per-host* re-check
for each newly migrated node, not a one-time gate.

## Cut-over order

**Ordinary (non-relay) nodes may be migrated in any order.** The one
ordering rule that matters: **a relay host — a node whose USB-attached
boards include the fleet's radio relays and that currently runs legacy
`mbrelay.service` — is migrated last**, and only after robot-console has
been directly verified against an already-migrated node's compatibility
pool (see "Robot-console verification" below). This is because
robot-console's relay discovery is the one piece of this migration with
a live external consumer that must keep working uninterrupted; ordinary
nodes have no such consumer.

`torture` was the fleet's one relay host at the start of sprint 005.
**It is already migrated** — see the dedicated section below. If any
other relay host exists elsewhere in the fleet (outside the six hosts
this project currently knows about), apply the same "last, after
verification" rule to it, using `torture`'s own cutover as the worked
example.

### `torture`: already migrated

`torture` (Ubuntu 24.04 x86_64) held all four of the fleet's RADIOBRIDGE
relays under the legacy `mbrelay.service` (`/usr/local/bin/mbrelay`,
ports 8760/8761) at the start of sprint 005. It has since been cut over
to `mbregistry`, with the compatibility pool enabled:

- Legacy `mbrelay.service` is **stopped and disabled** — not deleted.
  Its unit file and `/usr/local/bin/mbrelay` binary are both still on
  disk, for rollback (see Rollback below).
- `mbregistry.service` is running, enabled, with the robot-console
  compatibility pool on port **7444** and the `/names` API on port
  **7445** (both defaults — `mbregistry service run` only disables the pool with
  `--no-relay-pool`, which is not passed here).
- The stakeholder removed one relay (`getez`) from `torture` during this
  sprint; it now has three relays, not four. Don't expect a fourth
  anywhere.
- Hardware-verified end to end, including the peer-sync ownership bug
  this cutover itself exposed and sprint 005 ticket 011 then fixed:
  `torture`'s pool now hands out all three relays on connect (a fourth
  concurrent connection correctly gets "3 devices, 3 in use"), and every
  host's `mbregistry list` shows the three relay uids
  (`4f02a351`/`52f41cc6`/`f92f913d`) as `host=torture` (or `host=local`
  on `torture` itself), not stuck under a stale remote owner.

**Provenance note, for accuracy:** sprint 005's own `sprint.md` (Problem
section, mid-sprint amendment) describes this cutover as having happened
"outside this sprint, by the stakeholder's own action." The more
detailed, hardware-verified record is ticket 007's own Implementation
Notes: `scripts/deploy-host.sh torture`'s first run is what actually
stopped and disabled the legacy service and then installed and started
`mbregistry` — performed during sprint 005 itself, under an explicit
stakeholder-authorized scope exception (see `sprint.md`'s Problem-section
amendment and ticket 007's "Scope change: six hosts, not five" note),
not by the stakeholder running commands by hand beforehand. This
document follows ticket 007's own record as the ground truth, since it's
the one with command-level, hardware-confirmed detail. Either way, the
practical fact for this runbook is the same: **`torture` needs no further
action from this runbook** — it's recorded here, with its own rollback
recipe, so a future reader doesn't have to reconstruct any of this from
sprint history, per this ticket's own purpose.

**`torture`'s own rollback**, if ever needed:

```bash
ssh torture
sudo systemctl stop mbregistry.service
sudo systemctl disable mbregistry.service
sudo systemctl enable --now mbrelay.service
systemctl status mbrelay.service   # confirm active
```

The legacy binary and unit are untouched on disk, so this is a clean
re-enable, not a reinstall.

## Per-host procedure (the rest of the fleet)

Run this against one host at a time. Do not batch multiple hosts in
parallel the first time you run this against a given host — confirm each
one healthy before moving to the next.

### 1. Retire the old `mbdeploy serve`

The exact commands already used and hardware-verified for this step, on
the four Nolanet test nodes, in `docs/acceptance/001-hardware.md`'s "Old
`mbdeploy` retirement" section:

```bash
ssh <host>
sudo systemctl stop mbdeploy.service
sudo systemctl disable mbdeploy.service
```

**Generalization from that precedent, deliberate, not an oversight:**
`docs/acceptance/001-hardware.md` also removed the unit file and ran
`sudo rm -rf /home/jtl/mbdeploy` at this point — appropriate for four
low-risk dedicated test hosts with no rollback need. This runbook's own
Rollback section (below) requires the old units still be re-enable-able
until the new install is confirmed healthy, so for the *remaining* fleet:
**stop and disable only; do not remove the unit file or
`/home/jtl/mbdeploy` yet.** Delete them later, as an optional cleanup
step, once the host has run `mbregistry` healthily for however long you
want to satisfy yourself it's solid — that's a judgment call for the
stakeholder, not a step this runbook requires.

### 2. Retire legacy `mbrelay.service` (relay hosts only)

Only applies to a host currently running the legacy relay server. Same
stop/disable-not-delete pattern already used on `torture` (ticket 007):

```bash
sudo systemctl stop mbrelay.service
sudo systemctl disable mbrelay.service
# leave /usr/local/bin/mbrelay and its unit file in place
```

If the check comes back "unit not found" or already inactive/disabled,
there's nothing to do here — move on.

### 3. Bring the host up on `mbtools`

`scripts/deploy-host.sh` (ticket 007) is this runbook's per-host
mechanism: it builds the current wheel, installs it, runs `mbregistry
install-service` (systemd unit + udev rule + USB-access group
membership), and enables/starts the service — idempotent, safe to
re-run. It ships with a **fixed host list** (`ALLOWED_HOSTS` near the top
of the script) that today reads exactly `meili loki hodr magni braeburn
torture` — by design, so it can't be pointed at an unlisted host by
accident (ticket 007's own acceptance criteria). To use it against a new
host:

```bash
# in the mbtools repo, on the machine you run this from (not on <host>)
$EDITOR scripts/deploy-host.sh   # add <host> to the ALLOWED_HOSTS array
scripts/deploy-host.sh <host>
```

Commit that one-line addition to `ALLOWED_HOSTS` (or keep it as a local,
uncommitted edit if you'd rather not grow the list permanently in the
repo — implementer's/stakeholder's call; the script itself doesn't
require a commit to run).

For a relay host (one that had `mbrelay.service` in step 2), the relay
pool comes up enabled by default — no extra flag needed.

Confirm before moving on:

```bash
ssh <host> 'systemctl status mbregistry.service'   # active, enabled
ssh <host> 'mbregistry list'                        # boards visible
```

### 4. Robot-console verification (relay hosts only, before cutting over the last relay host)

Only required once, against the *first* relay host you migrate (or
already satisfied — see Prerequisites — if you're doing an ordinary
node). This is the operator-facing version of
`docs/acceptance/004-hardware.md` Scenario 4's own direct-endpoint
checks, which exercised the exact wire contract `robot-console`'s
`mdnsDiscovery.ts`/`mbrelayRegistry.ts`/`relayBridger.ts` use — see that
section for the full detail; this is the condensed checklist to re-run
per host, not a restatement of it:

1. **mDNS advertisement.** From a *different* host: `avahi-browse -rt
   _mbrelay._tcp` — the migrated host should appear, instance name =
   its hostname, port `7444`, TXT `registry=7445`.
2. **Pool-port reset-by-reconnect.** `nc <host> 7444`, send `?`, confirm
   a fresh `DEVICE:RADIOBRIDGE:relay:...` banner and factory-default
   tuning (`# channel: 0 group: ... mode: RAW250`) on every fresh
   connect — this is the behavior `relayBridger.ts` depends on instead of
   a `BREAK` (which pool connections don't support).
3. **`/names` API.** `curl` against `<host>:7445/names/<name>` (`GET`,
   `PUT` with a JSON body, `DELETE`) — response shape `{channel:
   number, group: number, source: string}`, matching
   `mbrelayRegistry.ts`'s `parseResolvedAddress`.
4. If feasible, run `robot-console` itself against the migrated host and
   confirm it discovers and connects through the relay normally — a full
   headless run wasn't done even in Scenario 4 (judged out of that
   ticket's proportion; the direct-endpoint checks above exercise the
   same contract), so this step is a nice-to-have, not a hard gate.

Once this passes against one migrated relay host, later relay hosts
don't need to repeat it — the contract doesn't vary per host.

## Rollback

If a cut-over host doesn't come up cleanly:

```bash
ssh <host>
sudo systemctl stop mbregistry.service
sudo systemctl disable mbregistry.service    # if it got that far

# whichever of these existed on this host before cutover:
sudo systemctl enable --now mbdeploy.service
sudo systemctl enable --now mbrelay.service
```

**Precondition this depends on**: step 1/2 above must not have deleted
the old units before this point — that's exactly why this runbook has
you stop-and-disable rather than delete-immediately, unlike the original
sprint 001 precedent on the four Nolanet nodes (which had no rollback
need). Only delete the old `mbdeploy.service`/`mbrelay.service` units,
`/home/jtl/mbdeploy`, and/or `/usr/local/bin/mbrelay` once the new
`mbregistry` install has been confirmed healthy for as long as you want
to be sure — and even then, it's optional cleanup, not required by this
runbook.

## Other repositories to update

None of these are edited by this sprint — described here so the
stakeholder knows what else needs attention, and doesn't have to
rediscover it from scratch.

- **`Busboombot/mbdeploy`'s Ansible roles and golden Pi Zero image.**
  Per that project's own design docs (referenced from
  `docs/design/specification.md`'s "Migration" section in this repo),
  the fleet's Pi nodes were provisioned from a golden image and/or
  Ansible roles that assume `mbdeploy serve`/`mbrelay.service` and bake
  them in. Once a host is cut over by this runbook, its live state no
  longer matches what that image/those roles would (re-)install — so a
  future re-image or Ansible re-run against a cut-over host would
  silently regress it back to the legacy daemons unless that repo's own
  tooling is updated to stop shipping `mbdeploy.service`/
  `mbrelay.service` and instead provision `mbtools` (e.g. calling this
  repo's own `scripts/deploy-host.sh`, or an equivalent Ansible role
  built the same way). This is a change to a different repository,
  outside this project's write scope — flagged here, not made here.
- **`robot-console`'s cached link state.** `robot-console` discovers
  relays purely over mDNS at runtime (`_mbrelay._tcp` for the legacy
  server, same service type `mbregistry`'s compatibility pool also
  advertises) — there is no static `:8760`/`:8761` configuration file in
  its current source to edit; those ports only appear in its test
  fixtures as example legacy addresses. What *can* go stale is its own
  local device/link store (`packages/host/src/store/`), which persists
  the `{host, port}` it last successfully connected through for a given
  relay link. In the normal case this self-heals: the next mDNS
  discovery cycle after a host's cutover re-resolves the same instance
  name at the new port and the store updates. If a robot-console
  instance was running continuously through a host's cutover and its
  relay link looks stuck pointing at the old port, restart
  `robot-console` (forcing a fresh mDNS discovery pass) before assuming
  anything is broken.
- **Both wikis** — exact paste-ready text below.

## Wiki text to paste

This sprint does not edit either wiki (out of scope) — paste the
relevant block yourself once a host is migrated, updating the bracketed
placeholders.

### Robot Garage wiki (internal — `http://robot-garage.home/doku.php?id=mbdeploy`, "Current status" table, Daemon column)

Following the same pattern `docs/acceptance/001-hardware.md` already
used for the four Nolanet nodes ("mbdeploy retired `<date>`; mbregistry
(mbtools) active, enabled"):

For `torture` (already migrated — paste this now):

```
mbrelay.service retired 2026-09-24 (unit file + binary kept on disk for
rollback); mbregistry (mbtools) active, enabled -- relay pool (7444) and
/names API (7445) serving robot-console. 3 relays (getez removed by
stakeholder decision).
```

For any other host, once migrated (fill in `<hostname>` and `<date>`):

```
mbdeploy retired <date>; mbregistry (mbtools) active, enabled.
```

...or, for a relay host:

```
mbrelay.service retired <date> (unit file + binary kept on disk for
rollback); mbregistry (mbtools) active, enabled -- relay pool (7444) and
/names API (7445) serving robot-console.
```

### Public docs (`robots.jointheleague.org/subsystems/mbdeploy/`, published from `Busboombot/mbdeploy`'s `docs/wiki/`)

No machine specifics (hostnames, IPs, install paths) belong on this
page — it's world-readable. Paste-ready, generic text for wherever that
manual currently describes `mbdeploy serve`/fleet daemon operation:

```
mbdeploy's `serve` daemon and the standalone `mbrelay` relay server are
being retired fleet-wide, replaced by `mbtools`' `mbregistry` daemon
(https://github.com/League-Microbit/mbtools). `mbregistry` is a drop-in
replacement from a client's point of view: boards are still discovered
over mDNS, and the relay-server wire contract (`_mbrelay._tcp`
advertisement, the pool port, the `/names` API) is unchanged, so
existing clients such as robot-console need no configuration changes.
See mbtools' own documentation for current install and usage
instructions.
```

## Summary checklist

- [ ] Prerequisite already satisfied: compatibility contract verified
      (sprint 004, `docs/acceptance/004-hardware.md` Scenario 4; sprint
      005 ticket 011 re-verified it hardware-side on `torture`).
- [ ] `torture`: already migrated — nothing to do; rollback recipe above
      if ever needed.
- [ ] For each remaining host: retire `mbdeploy.service` (stop+disable,
      don't delete yet), retire `mbrelay.service` if present (same),
      add it to `scripts/deploy-host.sh`'s `ALLOWED_HOSTS` and run it,
      confirm `mbregistry.service` active and `mbregistry list` shows
      its board(s).
- [ ] If it's a relay host and no relay host has been verified yet, run
      the robot-console checklist above before relying on it in
      production.
- [ ] Paste the appropriate wiki text (both wikis, per host) once
      confirmed healthy.
- [ ] Optional cleanup, once confident: delete the old unit files and
      `/home/jtl/mbdeploy`/`/usr/local/bin/mbrelay`.
