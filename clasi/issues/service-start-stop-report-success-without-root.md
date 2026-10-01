---
status: pending
---

# `mbregistry service stop`/`start` report success without doing anything when not root

## Description

Found on `gala` (macOS, system LaunchDaemon `org.jointheleague.mbregistry`),
2026-09-28. Run as `eric`, without `sudo`:

```
$ mbregistry service restart
mbregistry: service restart failed: launchctl kickstart -k system/org.jointheleague.mbregistry exited 1
$ mbregistry service stop
mbregistry: stopped the system service.
$ mbregistry service start
mbregistry: started the system service.
```

The daemon was never touched: `launchctl print system/org.jointheleague.mbregistry`
afterwards still showed the original pid, started three days earlier, and
the new `rescan` op kept failing with `unknown op 'rescan'`. `stop` and
`start` claimed success anyway. `restart` failed, but only with a bare
`exited 1` that doesn't say root is needed.

## Expected

- `service stop`/`start`/`restart` for the **system** scope, run without
  root, exit non-zero with a clear message: "controlling the system
  service needs root: run `sudo mbregistry service restart`".
  Check the actual result of the `launchctl`/`systemctl` call (exit
  code, and on macOS whether the job's pid changed or it's gone), and
  never print success on the strength of the command merely running.
- Include `launchctl`'s stderr in any failure message.
- Same audit on Linux (`systemctl`) and Windows.

## Related finding (same session)

`gala`'s daemon venv `/opt/mbtools/venv` had lost its `mbtools` package:
only root-owned `__pycache__` directories were left, with no `.py` files and no
`dist-info`. An earlier `uv pip install --python /opt/mbtools/venv/bin/python .`
most likely removed the old files, then failed on the root-owned
`__pycache__` the running-as-root daemon had written, and never installed
the new build. The running daemon kept working from memory, but any
restart would have failed. Worth either making the documented update
step (`docs/service.md` §7.3, CLAUDE.md) clear root-owned `__pycache__`
first (`sudo find /opt/mbtools/venv -name __pycache__ -user root -exec rm -rf {} +`)
or running the daemon with `PYTHONDONTWRITEBYTECODE=1`.
