---
status: pending
split_from: robot-console-on-mbregistry-multi-instance-and-spawn-support.md
---

# Default the mbregistry compatibility shims off once robot-console is native

## Description

Split on 2026-09-24 from
`robot-console-on-mbregistry-multi-instance-and-spawn-support.md` (item 9;
`docs/design/robot-console-integration.md` §5 item 9).

Once robot-console speaks the native mbregistry protocol (watch, local
`stream`, `names_get`/`names_set`), default the compatibility shims — the
relay pool (7444) and HTTP `/names` (7445) — to **off**, and eventually
remove them.

**Blocked on** the robot-console repo shipping its mbregistry client. Not
schedulable until then.
