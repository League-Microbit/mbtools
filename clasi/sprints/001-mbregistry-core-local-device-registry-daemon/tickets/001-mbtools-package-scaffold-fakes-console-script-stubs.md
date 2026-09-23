---
id: '001'
title: 'mbtools package scaffold, fakes, console-script stubs'
status: open
use-cases: []
depends-on: []
github-issue: ''
issue: mbregistry-device-registry-daemon.md
completes_issue: false
---
<!-- CLASI: Before changing code or making plans, review the SE process in CLAUDE.md -->

# mbtools package scaffold, fakes, console-script stubs

## Description

Stand up the `mbtools` repo as an installable Python project before any
registry code exists. This is pure infrastructure — no dedicated issue
covers it, but every other ticket in this sprint (and every later
sprint) depends on the layout, dependency management, and test-fakes
this ticket creates.

Per sprint.md's Architecture → Design Rationale ("Package layout"), this
is a **single `src/mbtools/` package**, not four separate installable
packages: `mbtools.registry` (this sprint's whole daemon),
`mbtools.common` (shared DTOs — announcement/identity types now; the
registry wire-protocol types once sprint 002 needs to speak them),
`mbtools.testing` (fakes, below), and stub packages `mbtools.deploy`,
`mbtools.serial`, `mbtools.relay` for the other three programs (empty
except for a stub `main()`).

Managed with `uv` (per this project's setup with dotconfig/CLASI). Test
runner is `pytest`. Console scripts registered in `pyproject.toml`:
`mbregistry = mbtools.registry.cli:main` (not implemented until ticket
009 — a placeholder `main()` that exits non-zero with "not yet
implemented" is fine for this ticket), and `mbdeploy`, `mbserial`,
`mbrelay` each pointed at a stub `main()` in their respective stub
package that prints "not yet implemented — see mbtools sprint 002/004"
to stderr and exits with a non-zero status (reuse one shared stub
implementation rather than three copies — see `mbtools.common` or a
small `mbtools._stub` helper).

Also builds the **hardware-free test-fakes** every later ticket in this
sprint (and sprint 002+) needs, per sprint.md's Test Strategy:

- `mbtools.testing.fakes.FakeUSBSource` — scripts a sequence of
  `comports()`-shaped attach/detach snapshots for the `PortWatcher`
  interface (ticket 003) to consume, without touching real USB.
- `mbtools.testing.fakes.FakeSerial` — a pyserial `Serial`-shaped fake
  (open/close, `dtr`/`rts` properties, `write`, `readline`,
  `reset_input_buffer`) that can script a scripted announcement line,
  silence (timeout), or a "port busy" `OSError` on open, for the
  identity probe (ticket 002) to consume without a real board.

These two fakes are the actual behavioral contract this ticket has to
get right — everything else here is scaffolding.

## Acceptance Criteria

- [ ] `pyproject.toml` exists at repo root, managed with `uv`, declaring
      `src/mbtools/` as the package root (`src` layout, not flat).
- [ ] `mbtools/registry/`, `mbtools/common/`, `mbtools/testing/`,
      `mbtools/deploy/`, `mbtools/serial/`, `mbtools/relay/` all exist
      as importable subpackages (empty `__init__.py` where there's
      nothing to put yet).
- [ ] Console scripts `mbregistry`, `mbdeploy`, `mbserial`, `mbrelay`
      are all registered in `pyproject.toml` and resolve after `uv sync`
      / `uv run`; the three stub scripts print a clear "not yet
      implemented" message naming the sprint that will implement them,
      and exit non-zero.
- [ ] `mbtools.testing.fakes.FakeUSBSource` implements the same shape
      `PortWatcher.scan()` will consume (a `{uid: PortInfo}`-like
      mapping) and supports scripting at least: an empty scan, a scan
      with one matching device, a scan with one non-matching (wrong
      VID:PID) device, and a scripted sequence across multiple calls
      (to simulate attach-then-detach).
  - [ ] `mbtools.testing.fakes.FakeSerial` supports scripting: a
      returned announcement line (either dialect), silence until
      timeout, and raising on `open()` to simulate a busy port.
- [ ] `pytest` is configured (`pyproject.toml` or `pytest.ini`) and
      `uv run pytest` runs (zero tests collected is fine at this
      ticket — the fakes' own tests count).
- [ ] A minimal `README.md` or `CONTRIBUTING` note (a few lines is
      enough) documents `uv sync` / `uv run pytest` as the way to set
      up and test the repo, so ticket 002 onward doesn't have to
      rediscover it.

## Implementation Notes

- Port reference for `FakeSerial`'s scripted-line behavior: the shape of
  what it needs to produce is `mbdeploy/src/mbdeploy/devices.py`'s
  `probe_type()` (both the `DEVICE:...` and `device ...` dialect
  examples in its module docstring), not the function itself — this
  ticket only builds the fake serial port, not the parser (ticket 002).
- Port reference for `FakeUSBSource`: the shape it needs to produce is
  `mbdeploy`'s `port_serial_map()` return value (`{uid: port_path}`)
  filtered to VID:PID `0x0D28:0x0204` — again, only the fake, not the
  real polling implementation (ticket 003).
- Keep `mbtools.common` empty (just the package directory) in this
  ticket unless a genuinely shared type is needed to make the fakes
  typecheck cleanly — don't speculatively populate it.

## Testing

- **Existing tests to run**: none — this is the first ticket.
- **New tests to write**: unit tests for `FakeUSBSource` and
  `FakeSerial` themselves (they script correctly, raise when scripted
  to, etc.) — these are test-support code, but they need their own
  tests since every later ticket's test suite depends on them behaving
  exactly as scripted.
- **Verification command**: `uv run pytest`
