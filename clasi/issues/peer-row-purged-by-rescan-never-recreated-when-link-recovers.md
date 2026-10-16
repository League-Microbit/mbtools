---
status: pending
---

# Peer row purged by rescan is never recreated when its link recovers

## Description

Found on 2026-10-06. `gala`'s `mbregistry list` showed `gopiv`
(NEZHA2 robot on `nada`) as `peer unreachable`, though the link to
`nada` was up and `gopiv`'s row was current. `mbregistry list --json`
showed `endpoint: null` and `peer_reachable: false` for that row. That
means `gala`'s store had **no `peer` row for `nada` at all**. The
network was fine: from `gala`, ports 7440, 7442 and 7443 on `nada` were
all open, and `dns-sd -L nada _mbregistry._tcp` resolved it.

### Sequence (from `/Library/Logs/mbregistry.log` on `gala`)

1. `link to nada (tcp://192.168.4.53:7442) dropped`: the link went down,
   and the peer row was marked `reachable = 0`. The `_PeerLink` kept
   retrying (many `snapshot request to nada ... timed out` lines).
2. A `rescan` ran while `nada` was unreachable. `BaseAPIServer._op_rescan`
   (`src/mbtools/registry/_api_base.py`, ~L636-661) calls
   `Store.purge(...)`, which deletes unreachable peer rows
   (`DELETE FROM peer WHERE host = ? AND reachable = 0`, `store.py`
   ~L1290). It does **not** stop or remove the peer's `_PeerLink` in
   `PeerDiscovery._peer_links`, so the link survives with no row behind
   it. (`rescan` writes nothing to the log. This step is inferred,
   because `purge` is the only code path that deletes a peer row.)
3. `nada` came back. The surviving link reconnected, and its snapshot
   re-populated `gopiv` with `host=nada`. Then
   `PeerDiscovery._on_peer_reachable` (`peering.py` ~L1974) called
   `store.mark_peer_reachable("nada")`, which raised `KeyError`. That
   was caught and only logged:
   `reachable callback for unknown peer nada` (four times).
4. Nothing recreated the row. The mDNS path normally calls
   `record_peer_seen` before `connect_peer`, but on `gala` the
   re-resolve timed out:
   `nada._mbregistry._tcp.local. did not resolve within timeout; skipping`.
5. Result: `gopiv` has no endpoint, so `_api_base` (~L396-397) reports
   `peer_reachable: False` and the table shows `peer unreachable`
   until the daemon is restarted. The device is also unusable for
   remote flash and serial, because there is no endpoint.

This is the same "link connects, snapshot syncs, but no `peer` row to
mark" failure that `connect_peer`'s `remote_port` docstring warns about
for `--peer`. Here `rescan` reaches it through a different path.

### Suggested fix (pick one or both)

- **Self-heal in `_on_peer_reachable`:** have `connect_peer` remember
  each host's endpoint (`address:remote_port`, from the TXT record or
  `--peer`). On `KeyError`, call `store.record_peer_seen(host, endpoint)`
  instead of only logging. Apply the same handling to
  `_on_peer_unreachable`.
- **Make `rescan` consistent with the live links:** skip deleting a peer
  row whose `_PeerLink` is still tracked, or stop and remove that link
  when its row is purged, so that mDNS rediscovery starts clean.

Add a regression test: purge an unreachable peer while its link is
tracked, fire the reachable callback, and assert that the peer row
exists again with `reachable = 1` and that the device's `endpoint` is
not `None`.

### Workaround

`sudo mbregistry service restart` on the affected host rebuilds the
peer table from mDNS.

### Related observation (not the cause)

`gala` is on two interfaces with overlapping subnets: `en0` is
`192.168.1.211/21`, which already covers `en1`'s `192.168.4.240/23`.
`nada` is advertised only on `en1`. This may be why python-zeroconf's
resolve of `nada` timed out, which blocked the normal recovery path.
