# Sprint 010 — `mbregistry rescan` command: hardware acceptance on `loki`/`magni`

Sprint 010 (`mbregistry rescan command`), ticket
`004-hardware-acceptance-rescan-on-two-nolanet-nodes`. Run 2026-09-28
against `loki` and `magni` (both Nolanet test nodes, Debian 13 aarch64,
dedicated test boards `togov`/`fe9a0254` and `vitut`/`8939f0a5`,
`RADIOBRIDGE/relay`/`NEZHA2/robot` respectively), per project
`CLAUDE.md`'s hardware-testing table — deliberately not `torture`, per
the same table's standing guidance and this ticket's own instruction.
`meili` was used throughout as a third, untouched bystander host to
confirm `rescan`'s local-only scope. This validates tickets 001-003's
`Store.candidates_for_purge()`/`Store.purge()`, the local `rescan` API
op (forced poll + peer resync), and the `mbregistry rescan [--dry-run]
[--json]` CLI against real USB unplug/replug and real peer
stop/restart — the one thing no unit/integration test in tickets
001-003 exercises.

**The known `/opt/mbtools` stale-symlink quirk (`CLAUDE.md`, ticket
007-007) recurred on both hosts** — `head -1 $(which mbregistry)`
printed `#!/opt/mbtools/bin/python3` (build `0.20260925.3`) on both
`loki` and `magni` immediately after `scripts/deploy-host.sh` finished
(the daemon itself was already on the fresh build, per its
`ExecStart=` pointing at the venv `deploy-host.sh` just built — only
the bare client command was stale). Fixed on both hosts exactly as
ticket 007-007 did on `meili`/`loki`: re-`ln -sf`-ing
`/usr/local/bin/{mbregistry,mbdeploy,mbserial,mbrelay}` to the fresh
venv (`~/mbtools-venv` on `loki`; `/tmp/mbtools-venv-eric` on `magni`,
per `deploy-host.sh`'s own low-disk tmpfs fallback — `magni`'s `$HOME`
had only 155808KB free). Confirmed afterward: `mbregistry --version`
on both hosts reports `0.20260928.1`, the branch build. Left pointed at
the fresh venv at session end (not reverted to `/opt/mbtools`) — this
is the same permanent fix ticket 007-007 applied, not a temporary
workaround.

Both hosts arrived with substantial **real, pre-existing purge-eligible
cruft** already in their local registry views before this session
touched anything — several `gone` (disconnected) mirrors of `hodr`'s
and `braeburn`'s devices that had gone stale since those peers'
`reachable` flag flipped back to `true` (`Store.candidates_for_purge`'s
third category, "reachable peer's stale disconnected mirror" — stale
since sprint 005 ticket 011, per that method's own docstring), plus
several devices owned by two peers (`torture`, `feldman`) already
showing `peer unreachable`, and a third peer (`denning`) with no
currently-visible device row but still present in the `peer` table as
unreachable. None of this was created by this session; it is exactly
the kind of accumulated cruft `rescan` exists to clear, and Scenario 1
below cleared it as a side effect of the very first real `rescan` call
— documented there rather than treated as a defect.

## Pre-existing state (before this session touched anything)

`loki`'s own `mbregistry list` at session start (`sudo`, matching every
other host in `CLAUDE.md`'s table — `eric` needs `plugdev`/root for
local socket and USB access on these hosts):

```
STATE             LOCKED  NAME   UID       FIRMWARE           HOST      PORT
----------------  ------  -----  --------  -----------------  --------  -----------------------
peer unreachable  -       zapig  07d057b7  NEZHA2/robot       feldman   /dev/ttyACM0
gone              -       zugit  0f0a31a9  NEZHA2/robot       hodr      /dev/ttyACM1
gone              -       getez  17449eac  RADIOBRIDGE/relay  braeburn  /dev/cu.usbmodem14602
free              -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr      /dev/ttyACM0
gone              -       tigez  3b43773c  NEZHA2/robot       hodr      /dev/ttyACM1
peer unreachable  -       gozop  4f02a351  RADIOBRIDGE/relay  torture   /dev/ttyACM3
peer unreachable  -       guvov  52f41cc6  RADIOBRIDGE/relay  torture   /dev/ttyACM1
free              -       gitev  5e042b04  NEZHA2/robot       meili     /dev/ttyACM0
free              -       vitut  8939f0a5  NEZHA2/robot       magni     /dev/ttyACM0
gone              -       tovez  a8fdb5e4  NEZHA2/robot       magni     /dev/ttyACM1
peer unreachable  -       -      b6d2685f  -                  feldman   /dev/ttyACM0
peer unreachable  -       vevov  b8e12372  NEZHA2/robot       feldman   /dev/ttyACM0
peer unreachable  -       vutev  ed09e98c  JOYSTICK/joystick  gala      /dev/cu.usbmodem2221402
peer unreachable  -       zetog  f92f913d  RADIOBRIDGE/relay  torture   /dev/ttyACM0
free              -       togov  fe9a0254  RADIOBRIDGE/relay  local     /dev/ttyACM0
```

`magni`'s own view was materially identical except `togov`/`vitut`
reversed which was `local` vs. mirrored, and it additionally showed
`tigez` as `gone`/**`LOCKED gala`** — a peer-owned row (`hodr`) whose
cached `remote_lock_kind` was non-`None` (see Scenario 3). `loki`'s own
view of the same row showed no lock at that moment — this cached
display is itself dynamic/short-lived (see Scenario 3's discussion),
not a stable fixture to rely on for testing.

`meili`'s own view (captured as the bystander baseline, re-checked
unchanged at the end — see Scenario 5) had the same shape.

## Deploy

`scripts/deploy-host.sh loki` and `scripts/deploy-host.sh magni`, per
its own usage comment. Both rebuilt from the sprint branch tree
(`mbtools-0.20260928.1`), stopped `mbregistry.service` first (the
script's own root-owned-`__pycache__` handling), reinstalled, and ran
`install-service` idempotently. `magni` fell back to the tmpfs venv
path (`/tmp/mbtools-venv-eric`) exactly as the script's own low-disk
comment documents (`$HOME` at 155808KB free). Both services came up
`active`/`enabled`; both needed the symlink fix described above before
a bare CLI command reflected the branch build.

## Scenario 1: real `gone` row via USB unbind, `--dry-run`, and real `rescan`

**PASS.** `loki`'s board is at USB path `1-1.3`
(`udevadm info -q path -n /dev/ttyACM0`). Unbound via sysfs (same
technique as sprint 009 ticket 004's `hodr` pass):

```
$ sudo mbregistry list | grep togov          # before
free   -   togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0

$ sudo sh -c 'echo -n 1-1.3 > /sys/bus/usb/drivers/usb/unbind'
$ ls /dev/ttyACM0
ls: cannot access '/dev/ttyACM0': No such file or directory
$ sudo mbregistry list | grep togov          # after unbind
gone   -   togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

A genuine device-node-level detach, exactly as sprint 009's own
Scenario (e) confirmed on `hodr` — not just a state-flag flip.

`rescan --dry-run --json`, with the board still unplugged, against
`loki`'s full pre-existing state described above:

```
$ sudo mbregistry rescan --dry-run --json
{
  "removed": {
    "devices": [
      "9906360200052820fe9a0254d8d892d9000000006e052820",
      "99063602000528204f02a3519fdba0c7000000006e052820",
      "9906360200052820f92f913d5f4e8412000000006e052820",
      "990636020005282052f41cc66121efc6000000006e052820",
      "9906360200052820b8e12372c44f4f67000000006e052820",
      "990636020005282007d057b7d6d99f53000000006e052820",
      "9906360200052820b6d2685f7104eb6a000000006e052820",
      "9906360200052820ed09e98c58c7141e000000006e052820",
      "99063602000528200f0a31a97da7074e000000006e052820",
      "990636020005282017449eac613c0332000000006e052820",
      "9906360200052820a8fdb5e413abb276000000006e052820",
      "99063602000528203b43773cab0210ea000000006e052820"
    ],
    "peers": ["torture", "feldman", "denning"]
  },
  "skipped_locked": [],
  "dry_run": true
}
$ sudo mbregistry list | grep togov          # unchanged by --dry-run
gone   -   togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

`fe9a0254` (`togov`, our freshly-disconnected local board) is in the
would-remove set, correctly grouped alongside the pre-existing
cruft described above (11 more devices: `torture`'s 3 relays,
`feldman`'s 3 devices, and 5 stale `hodr`/`braeburn`/`magni` mirrors;
3 unreachable peers: `torture`, `feldman`, and `denning` — a peer with
no currently-visible device row of its own, confirming
`candidates_for_purge`'s fourth `SELECT` finds unreachable peer rows
independent of whether they have visible children). `--dry-run`
changed nothing, confirmed via a second `list`.

Real `rescan`, board still unplugged:

```
$ sudo mbregistry rescan
removed 12 devices: fe9a0254, 4f02a351, f92f913d, 52f41cc6, b8e12372, 07d057b7, b6d2685f, ed09e98c, 0f0a31a9, 17449eac, a8fdb5e4, 3b43773c
removed 3 unreachable peers: torture, feldman, denning

STATE  LOCKED  NAME   UID       FIRMWARE           HOST   PORT
-----  ------  -----  --------  -----------------  -----  ------------
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
free   -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
free   -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
```

Exactly the set `--dry-run` predicted. `togov` (and every other row
above) is now **absent from `list` entirely** — not a lingering `gone`
row, a real delete, confirmed via both the human table and
`mbregistry list --json` (empty result for `fe9a0254`). Rebound the
board and confirmed it comes straight back via ordinary polling (not a
second `rescan`):

```
$ sudo sh -c 'echo -n 1-1.3 > /sys/bus/usb/drivers/usb/bind'
$ ls /dev/ttyACM0
/dev/ttyACM0
$ sleep 3 && sudo mbregistry list
STATE  LOCKED  NAME   UID       FIRMWARE           HOST   PORT
-----  ------  -----  --------  -----------------  -----  ------------
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
free   -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
free   -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
free   -       togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

Reappeared within the default 2s poll interval, fully reprobed
(`RADIOBRIDGE/relay`, not just "some device present").

**Bonus real-world confirmation of `rescan`'s purpose**: this single
call also cleared five sprints' worth of accumulated stale mirror rows
and three long-unreachable peers that had been sitting in `loki`'s own
view (not created by this session — see "Pre-existing state" above).
This is not a bug or an over-broad blast radius; it is `rescan`'s
documented job, and `loki`'s own state was demonstrably cluttered with
exactly the kind of cruft the sprint's own motivating issue describes.
No other host's own state was touched — only `loki`'s local cached
mirror of them (see Scenario 5).

## Scenario: forced-immediate-poll, isolated from the default 2s interval

**PASS**, but the default `--interval 2` (`daemon.DEFAULT_INTERVAL_S`)
turned out to be too short to cleanly isolate "rescan's own forced poll
did this" from "the daemon's own next scheduled poll would have caught
it anyway" — a plain unbind/rebind/rescan sequence over SSH easily
takes longer than 2 seconds end to end, so a naive attempt (see below)
proved nothing. To get an unambiguous answer, `mbregistry.service` was
stopped and a **temporary, manually-run** instance was started with
`--interval 30` against the same `--db`/`--socket` the service uses
(`/var/lib/mbregistry/devices.db`, `/run/mbregistry/api.sock`), giving
a wide, unambiguous window:

```
$ sudo systemctl stop mbregistry.service
$ sudo /home/eric/mbtools-venv/bin/mbregistry run --interval 30 \
    --db /var/lib/mbregistry/devices.db --socket /run/mbregistry/api.sock &
```

Unbound `togov`, and — since a store row only flips to `disconnected`
once an actual poll cycle notices the device missing, not
instantaneously on physical unbind — waited for that poll to actually
happen (confirmed by polling `list` every 2s; it flipped after ~22s,
consistent with a 30s cycle already in progress):

```
17:49:57  unbind
17:50:19  sudo mbregistry list | grep togov  ->  gone   -   togov  ...
```

Immediately rebound and called `rescan` within ~10 seconds — nowhere
close to the next 30s-interval tick (due at ~17:50:49):

```
17:50:24  sudo sh -c 'echo -n 1-1.3 > /sys/bus/usb/drivers/usb/bind'
          (device node present again immediately)
17:50:29  $ sudo mbregistry rescan
          removed 1 device: fe9a0254

          STATE  LOCKED  NAME   UID       FIRMWARE           HOST   PORT
          -----  ------  -----  --------  -----------------  -----  ------------
          free   -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
          free   -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
          free   -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
          free   -       togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
17:50:34
```

`rescan` reported `fe9a0254` as an actual removal (proving the DB row
was still genuinely `disconnected` at that instant — the 30s poll had
not run again), and the **same call's own printed table** already
shows `togov` back as `free`/`local`, fully reprobed — the row was
deleted (stale) and immediately rediscovered by `rescan`'s own forced
`poll_callback`, entirely inside one call, roughly 20 seconds before
the next scheduled poll would have. This is the acceptance criterion's
exact claim, confirmed unambiguously.

**Test-harness note, not a product defect**: restoring the real
`mbregistry.service` afterward initially failed
(`OSError: [Errno 98] Address already in use` on the remote-API port)
because the manual `--interval 30` process hadn't actually exited from
an earlier plain `kill` (it exits cleanly on `SIGTERM` via normal
Python shutdown, but that first `kill` attempt raced the check that
followed it). `sudo kill -9` on that one leftover PID, then
`systemctl restart mbregistry.service`, cleanly restored the real
service — an artifact of this test's own manual-foreground-process
methodology, not a daemon or `rescan` bug.

## Scenario 2 + 3: peer-unreachable purge, and a locked row surviving it

**PASS**, both halves, run as one combined flow (`loki` acting against
`magni`).

**Producing a genuinely-locked, purge-eligible row.** Per this ticket's
own note, a *local* disconnected device's lock is normally released
automatically on detect-disconnect (`daemon.py`'s detach handling), so
that combination can't be produced by unplugging a locally-owned board.
The peer-unreachable case is different: `_op_rescan`'s lock-skip check
(`_api_base.py`, `self._locks.status(uid)`) consults *this host's own*
in-memory `LockManager`, not the peer-mirrored `remote_lock_kind`
display column — and a device's `lock`/`unlock` ops are accepted by any
host's **local** socket for *any visible device, including one it
doesn't own* (`_resolve_visible`'s own docstring: "the local Unix
socket is this registry's own operator-facing view of the whole fleet,
not just what it physically owns"). So a lock taken directly against
`loki`'s own local socket for `magni`'s board is exactly the kind of
lock `rescan`'s skip-check can see, even though the device is
peer-owned. There is no standalone `mbregistry lock` CLI subcommand
(only `mbregistry unlock` exists as a manual escape hatch) and the
normal client tools (`mbserial`, `mbdeploy`) auto-route a peer-owned
target's lock through the *owning* host's own remote API
(`serial.cli`'s own "Local vs. remote transport" docstring), which
would register the lock on `magni`, not `loki` — no use here since
`magni` is about to go down. So, per the ticket's own suggestion
("whatever lock-acquisition path is easiest to drive by hand"), a tiny
throwaway script (`hold_lock.py`, using `registry.client.RegistryClient`
directly against `loki`'s own socket) held a real `serial`-kind lock on
`vitut` (`8939f0a5`, `magni`-owned) for the duration of this scenario:

```
$ ssh -f loki 'sudo /home/eric/mbtools-venv/bin/python /tmp/hold_lock.py \
    /run/mbregistry/api.sock 8939f0a5 serial 240 > /tmp/hold_lock.log 2>&1'
$ ssh loki cat /tmp/hold_lock.log
LOCKED 8939f0a5
```

**A real, notable UI/display finding**: `mbregistry list` on `loki`
still showed `vitut`'s `LOCKED` column as `-` even with this lock
genuinely held (confirmed via `list --json`: `lock_kind: null`,
`remote_lock_kind: null`). This is **deliberate, pre-existing,
documented behavior** — `_api_base.BaseAPIServer._device_dict`'s own
docstring: for a peer-owned row (`host is not None`), `lock_kind`/
`lock_pid` are always `None` ("there is no live call to make for a
device this registry doesn't own"); `registry.render` (line ~147)
renders `remote_lock_kind` instead for such a row. So a lock taken
locally against a peer-owned uid is real (as this scenario's `rescan`
result below confirms) but **invisible in `list`'s own table** — it
only ever shows the *owning* peer's own mirrored lock state, never a
lock this host itself independently holds on that uid. Not a defect
introduced by this sprint (this display logic predates ticket 010, and
its docstring already documents the tradeoff) — recorded here because
it directly answers this ticket's own open question about which real
scenario produces an observable locked-and-purge-eligible row, and the
answer is subtle enough to be worth writing down precisely.

Stopped `magni`'s service and waited for `loki` to notice:

```
17:53:41  ssh magni sudo systemctl stop mbregistry.service
17:54:02  sudo mbregistry list  ->  vitut now "peer unreachable", still HOST=magni
```

(21 seconds to detect — consistent with the existing peer-vanish
detection, unchanged by this sprint.)

`rescan --dry-run --json`, with `magni` unreachable and the lock still
held:

```json
{
  "removed": {
    "devices": [],
    "peers": ["magni"]
  },
  "skipped_locked": [
    "99063602000528208939f0a5fd47f738000000006e052820"
  ],
  "dry_run": true
}
```

`vitut`'s full uid is correctly in `skipped_locked`, not
`removed.devices`, despite qualifying as `unreachable_peer_owned`
(any state, per `candidates_for_purge`'s second category) — the
locally-held lock protected it exactly as designed. `magni`'s peer row
itself has no such protection (peer purge doesn't consult per-device
locks) and is correctly in `removed.peers`.

Real `rescan`:

```
$ sudo mbregistry rescan
removed 1 unreachable peer: magni
skipped 1 locked: 8939f0a5

STATE             LOCKED  NAME   UID       FIRMWARE           HOST   PORT
----------------  ------  -----  --------  -----------------  -----  ------------
free              -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
free              -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
peer unreachable  -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
free              -       togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

`vitut` survives, exactly as `--dry-run` predicted — still shown
`peer unreachable` (its `host` text and `peer_reachable` derivation are
unaffected by the underlying `peer` row's deletion; `_device_dict`
falls back to "unreachable" when `get_peer(host)` finds no row at all,
per that method's own docstring, so the display is unchanged even
though the `peer` table row is now gone).

Released the lock (`SIGTERM` to the holding script, which its own
handler catches to call `unlock` before exiting):

```
$ ssh loki sudo pkill -TERM -f hold_lock.py
$ ssh loki cat /tmp/hold_lock.log
LOCKED 8939f0a5
UNLOCKED 8939f0a5 (signal 15)
```

Restarted `magni`'s service and confirmed `loki`'s view recovers **on
its own**, no second `rescan`:

```
17:54:55  ssh magni sudo systemctl start mbregistry.service
17:55:08  sudo mbregistry list
STATE  LOCKED  NAME   UID       FIRMWARE           HOST   PORT
-----  ------  -----  --------  -----------------  -----  ------------
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
free   -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
free   -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
free   -       togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0
```

13 seconds to fully recover (`magni` and `vitut` both back,
`free`/`magni`) — matches sprint.md's Success Criteria "restarting the
peer brings it straight back," via ordinary reconnect/re-peering, not
an operator-run `rescan`.

**A caveat on the naturally-occurring `tigez`/`gala` row**: the
pre-existing `tigez` (`3b43773c`, `hodr`-owned) row shown `LOCKED gala`
on `magni`'s own view (see "Pre-existing state") was *not* independent
confirmation of the same finding — `loki`'s own view of that same row
showed no lock at the moment `loki`'s Scenario 1 `rescan` actually ran
(the cached `remote_lock_kind` display is itself short-lived/dynamic
across peers, evidently tied to some real client elsewhere on the LAN
whose connection came and went), so its purge in Scenario 1 isn't by
itself proof of anything beyond "an unlocked stale mirror was
correctly purged." The controlled `hold_lock.py` scenario above is the
one solid, repeatable answer to this ticket's open question.

## Scenario 4: `--json` output sanity

**PASS.** Already exercised throughout (`--dry-run --json` above,
Scenarios 1-3). One more clean example, a real (non-`dry_run`) call on
`loki`'s now-quiescent state (nothing left to remove) — confirms the
documented shape: `removed`/`skipped_locked`/`dry_run` always present,
plus a `devices` key (the fresh `list()`) **only** on a non-`dry_run`
call, matching ticket 003's own docstring ("`--dry-run` never calls
`list()`"):

```
$ sudo mbregistry rescan --json
{
  "removed": {"devices": [], "peers": []},
  "skipped_locked": [],
  "dry_run": false,
  "devices": [ /* 4 entries: vevav, gitev, vitut, togov, each with the
                  full _device_dict shape -- lock_kind/lock_pid/
                  lock_label/lock_since, remote_lock_kind/
                  remote_lock_display, endpoint, peer_reachable */ ]
}
$ sudo mbregistry rescan --dry-run --json
{"removed": {"devices": [], "peers": []}, "skipped_locked": [], "dry_run": true}
```

The `--dry-run` shape correctly omits `devices` when there is nothing
to report on top of what `list` already always shows. Matches tickets
002/003's documented response shape exactly, in every variant observed
this session (empty, dry-run-with-candidates, real-with-removals,
real-with-skips).

## Scenario 5: `rescan` on `loki` didn't affect other peers

**PASS.** `meili`'s own `mbregistry list`, re-checked after every
`rescan` call on `loki` above (including the large first one that
purged 12 devices/3 peers from `loki`'s own view), is **unchanged**
from the "Pre-existing state" capture at the top of this document —
still showing its own pre-existing stale `hodr`/`braeburn`/`magni`
mirrors, still showing `togov` as `free`/`loki` and `vitut` as
`free`/`magni` (both correctly reflecting current ground truth via
`meili`'s own independent peering, not `loki`'s). This is exactly
`rescan`'s documented scope (local-only, sprint.md's Design
Rationale) — confirmed on real hardware, not just by the unit tests
that already covered this in tickets 001-002.

## Teardown

Both hosts left running the branch build (`0.20260928.1`),
`mbregistry.service` `active`/`enabled`, both dedicated boards rebound
and correctly reprobed:

```
$ ssh loki 'systemctl is-active mbregistry; systemctl is-enabled mbregistry; sudo mbregistry list'
active
enabled
STATE  LOCKED  NAME   UID       FIRMWARE           HOST   PORT
-----  ------  -----  --------  -----------------  -----  ------------
free   -       vevav  2e78ea8f  RADIOBRIDGE/relay  hodr   /dev/ttyACM0
free   -       gitev  5e042b04  NEZHA2/robot       meili  /dev/ttyACM0
free   -       vitut  8939f0a5  NEZHA2/robot       magni  /dev/ttyACM0
free   -       togov  fe9a0254  RADIOBRIDGE/relay  local  /dev/ttyACM0

$ ssh magni 'systemctl is-active mbregistry; systemctl is-enabled mbregistry; sudo mbregistry list | grep vitut'
active
enabled
free   -   vitut  8939f0a5  NEZHA2/robot  local  /dev/ttyACM0
```

`magni`'s own remaining pre-existing cruft (`tigez`/`gala`,
`tovez`/`gone`, the `torture`/`feldman` unreachable rows, etc.) was
**left exactly as found** — this ticket's instruction was to keep
hosts other than `loki`'s own rescanned view as they are, and `magni`
was never itself the target of a `rescan` call (only stopped/restarted
as the peer under test from `loki`). Test-only leftover files
(`/tmp/hold_lock.py`, `/tmp/hold_lock.log`, `/tmp/mbregistry-slowpoll.*`)
removed from `loki`; nothing was written to `magni`'s filesystem beyond
the deploy itself. `torture`, `hodr`, `braeburn` were never touched.

## Regression check

No source defect was found — every scenario matched tickets 001-003's
documented behavior exactly, including the subtle lock-display finding
in Scenario 2/3 (which is pre-existing, already-documented behavior,
not a sprint 010 regression). No `src/`/`tests/` change was made.
Per this ticket's testing plan, the scoped test modules for
tickets 001-003 were re-run in the foreground:

```
$ uv run pytest tests/registry/store/test_store_purge.py tests/registry/api/test_api.py \
    tests/registry/cli/test_cli_rescan.py tests/registry/client/test_client.py \
    tests/registry/peering/test_peering_eventbus.py tests/registry/daemon/test_daemon.py -q
```

224 passed, 1 skipped, 1 failed on the first run
(`test_watch_fans_out_to_every_connected_watcher`, a 60s
`pytest-timeout` on a multi-connection fan-out test unrelated to any
code this ticket touched — no source change was made in this ticket).
Re-run in isolation: passed in 4.55s, confirming a scheduling/timing
flake under parallel load rather than a real regression. The full
suite runs once at `close_sprint`, per `.claude/rules/source-code.md`.

## Summary

| Scenario | Result |
|---|---|
| Deploy branch build to `loki`/`magni` (`scripts/deploy-host.sh`) | PASS — both `active`/`enabled`; `/opt/mbtools` stale-symlink quirk recurred and was fixed on both, per existing `CLAUDE.md` precedent |
| 1: real USB-unbind `gone` row, `--dry-run` preview, real `rescan` removal, rebind recovery | PASS |
| Forced-immediate-poll (isolated via a temporary `--interval 30` instance) | PASS — `rescan`'s own call purged a still-stale row and rediscovered the reattached board in the same call, ~20s ahead of the next scheduled poll |
| 2: peer-unreachable purge (`magni` service stop), peer row + non-locked device rows removed | PASS |
| 3: a genuinely locally-locked, peer-owned row survives `rescan` (manufactured via direct-socket lock, since local-disconnected+locked auto-releases and normal client tools route a peer-owned lock to the *owning* host) | PASS — plus a real display-vs-actual-lock-state finding, recorded above |
| Peer/service recovery without a second `rescan` | PASS — 13s |
| 4: `--json` output shape | PASS — matches tickets 002/003 exactly in every variant observed |
| 5: `rescan` on `loki` doesn't affect `meili`'s own view | PASS — unchanged throughout |
| Scoped regression tests (tickets 001-003 modules) | PASS (1 pre-existing flaky timeout, confirmed non-reproducing in isolation) |

**No `CLAUDE.md` update was needed.** The `/opt/mbtools` stale-symlink
quirk that recurred here is the same one ticket 007-007 already
documents (not new); the lock-display finding in Scenario 2/3 is
already fully documented in `_api_base.BaseAPIServer._device_dict`'s
own docstring and `registry.render`'s rendering logic — a confirmed,
pre-existing software design decision, not an undiscovered real-
hardware quirk. No `src/`/`tests/` correction was needed — `rescan`
behaved exactly as tickets 001-003 documented in every scenario,
against real USB and real peer connectivity.
