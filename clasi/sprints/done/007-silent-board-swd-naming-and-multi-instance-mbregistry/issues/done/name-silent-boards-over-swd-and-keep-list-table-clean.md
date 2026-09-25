---
status: done
sprint: '007'
tickets:
- 007-001
- 007-003
- 007-006
- 007-007
---

# Name silent boards over SWD, and show "no announcement" in the STATE column

## Description

Observed on `gala` (macOS):

```
$ mbregistry list --socket /tmp/registry.sock
STATE        NAME   UID       FIRMWARE           PORT
-----------  -----  --------  -----------------  -----------------------
gone         guvov  52f41cc6  RADIOBRIDGE/relay  /dev/cu.usbmodem2221202
no-firmware  -      ed09e98c  no firmware        /dev/cu.usbmodem2221202
  ed09e98c: no announcement received during probe
```

There are two problems.

### 1. A board that doesn't announce still has a name, and we should show it

At the moment NAME is filled only from the firmware's announcement, so a
blank board, or one running a student program that doesn't announce, shows `-`.

The five-letter name does **not** come from the USB/DAPLink UID. That UID
belongs to the interface chip. The name is `FICR.DEVICEID[1]` of the
target nRF, encoded with CODAL's `microbit_friendly_name()` codebook
(five base-5 digits). The announcement's `<serial>` field is that same
word in decimal. We can read it **over SWD through the probe**, with no
firmware and no serial port, by attaching without halting or resetting
(`connect_mode="attach"`, `auto_unlock=False`, `FICR_DEVICEID1` works on
both nRF51 and nRF52).

Old mbdeploy already does this: `read_device_id()` / `friendly_name()` /
`read_board_name()` in `Busboombot/mbdeploy` `src/mbdeploy/devices.py`.
mbtools never ported it. `identity.py`'s docstring mentions the old SWD
path, but nothing in `src/mbtools` reads FICR now.

What to do:
- When a probe gets no announcement, read DEVICEID over SWD, then store
  the name and the decimal serial.
- DEVICEID is a fixed property of the chip, so once we know it for a UID
  we can keep it across reflashes and failed probes, and never need to
  read it again.
- If the SWD read fails (probe busy mid-flash, part locked), fall back
  to `-`, as it does now.
- The daemon runs as root, so the SWD read works there even on hosts
  where client-side pyOCD needs `sudo`.

### 2. The table must stay a table: no free-text note lines under it

`render.render_table` prints `error_note` as an indented line after the
table. Drop that line, and show the condition inside the table.

- "No announcement" is not the same as "no firmware". A board running a
  program that doesn't announce has firmware; we just don't know which.
  Give it its own STATE value, e.g. `silent`, or `no-announce` (pick one
  that fits the column).
  FIRMWARE should then read `unknown`, not `no firmware`.
  Use "no firmware" only when we actually know the board is blank,
  e.g. right after a mass erase with no successful reflash (see also the
  related finding: after a failed flash mass-erased `togov`, `list` still
  showed its old `JOYSTICK/joystick` firmware).
- `--json` should carry the same information in a structured field,
  not only as prose in `error_note`.
- `mbdeploy list` shares `registry.render`, so it gets the fix too.

## Acceptance

- A board with no announcing firmware shows its real five-letter name in
  NAME whenever the SWD read succeeds.
- `mbregistry list` / `mbdeploy list` print only the header, the rule,
  and one row per device; nothing is printed after the table.
- The STATE column tells apart: announced, didn't announce, and known
  blank.
