"""``mbregistry`` — CLI entry point for the device-registry daemon.

Per sprint.md's Architecture (module "cli"), this is a client of the API
socket for ``list`` (per spec §3.5, "other programs consult it through a
service" — this module never reads ``store``/``locks`` directly, even
though it runs on the same host as the daemon it's talking to), the
process that constructs and runs the daemon for ``run``, and a
systemd-unit-file writer for ``install-service``.

**``mbregistry run`` is also where ticket 009 closes two gaps ticket 008
left documented, not solved, in ``docs/design/registry-api.md``'s "Known
limitations":**

1. *Cross-module concurrency.* :func:`assemble_daemon_and_api` builds
   exactly one ``threading.RLock`` and hands it to both
   :class:`~mbtools.registry.daemon.Daemon` and
   :class:`~mbtools.registry.api.RegistryAPIServer`, so every
   ``store``/``locks`` access from either side — the daemon's own poll
   loop, and the API's per-connection threads — goes through the same
   lock. See ``daemon.py``'s and ``api.py``'s own "Concurrency"/"Shared
   lock" docstring notes for what that lock does and does not cover.
2. *Flash holding the lock for the whole pyocd run.* Already fixed at
   the source in ``api.py``'s ``_op_flash`` (the shared lock now only
   guards the short bookkeeping before/after, never the streamed pyocd
   invocation itself) — this module doesn't add anything further for
   that, it just inherits the fix by using the shared-lock assembly
   above.

**macOS (braeburn) foreground dev use** (sprint.md's Open Question #3,
"runs in the foreground for local dev" is this sprint's stated bar): the
same ``mbregistry run`` that ``ExecStart=`` invokes under systemd also
runs directly in a developer's shell on macOS, unprivileged, via
:class:`~mbtools.registry.usbwatch.PollingPortWatcher` (plain
``comports()``, nothing Linux-specific). The default socket/db paths
(``/run/mbregistry/api.sock``, ``/var/lib/mbregistry/devices.db``) need
root to create on either platform; macOS has no root requirement to run
the daemon itself, but *does* need ``--socket``/``--db`` (or
``$MBREGISTRY_SOCKET``/``$MBREGISTRY_DB``) pointed somewhere the
invoking user can write, e.g. ``--socket
/tmp/mbregistry/api.sock --db ~/.local/state/mbregistry/devices.db``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Any

from mbtools.common import EXIT_ERROR, EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import DEFAULT_SOCKET_PATH, RegistryAPIServer
from mbtools.registry.daemon import DEFAULT_INTERVAL_S, Daemon
from mbtools.registry.flash import FlashOp
from mbtools.registry.store import (
    DEFAULT_DB_PATH,
    STATE_ATTACHED_UNPROBED,
    STATE_CONNECTED_NO_FIRMWARE,
    STATE_DISCONNECTED,
    Store,
)
from mbtools.registry.usbwatch import PollingPortWatcher, PortWatcher

__all__ = [
    "main",
    "build_parser",
    "cmd_list",
    "cmd_run",
    "cmd_install_service",
    "assemble_daemon_and_api",
    "render_systemd_unit",
    "DEFAULT_UNIT_PATH",
]

#: Standard systemd system-unit install location -- ``install-service``'s
#: default target, overridable with ``--output`` (every test uses that
#: override; writing to the real path needs root, per the ticket's own
#: "installing into a real systemd is not part of this ticket's automated
#: tests" scoping).
DEFAULT_UNIT_PATH = Path("/etc/systemd/system/mbregistry.service")

_SOCKET_ENV_VAR = "MBREGISTRY_SOCKET"
_DB_ENV_VAR = "MBREGISTRY_DB"


# ---------------------------------------------------------------------------
# path resolution -- flag > env var > module default (ticket 009's own
# "socket/DB paths overridable by flags or env" acceptance criterion)
# ---------------------------------------------------------------------------


def _resolve_path(flag_value: str | None, env_var: str, default: Path) -> Path:
    """``flag_value`` wins if given; else ``$env_var`` if set; else
    ``default``. Shared by every ``mbregistry`` subcommand that takes a
    socket or db path, so the precedence can't drift between them.
    """
    if flag_value:
        return Path(flag_value)
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return default


# ---------------------------------------------------------------------------
# list -- a client of the api socket, per spec section 3.5
# ---------------------------------------------------------------------------


def _connect(socket_path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(socket_path))
    return sock


def _request(sock: socket.socket, payload: dict[str, Any]) -> dict[str, Any]:
    """Send one newline-delimited-JSON request and read its one response
    line, per ``docs/design/registry-api.md``'s framing. Mirrors
    ``tests/registry/api/test_api.py``'s own ``_Client`` shape, minus
    the streaming ``flash`` op this ticket's CLI never calls.
    """
    wfile = sock.makefile("w", encoding="utf-8", newline="\n")
    rfile = sock.makefile("r", encoding="utf-8", newline="\n")
    wfile.write(json.dumps(payload))
    wfile.write("\n")
    wfile.flush()
    line = rfile.readline()
    if not line:
        raise ConnectionError("connection closed by mbregistry daemon")
    return json.loads(line)


def _state_cell(device: dict[str, Any]) -> str:
    """UC-004's STATE column: ``free`` / ``locked by <kind> pid <n>`` /
    ``no-firmware`` / ``gone``. Lock status (folded into the ``list``
    response by ``api._device_dict``) takes precedence over
    ``connected_no_firmware`` -- a device can be locked (e.g. mid-flash)
    while its last-known state is still "no firmware", and the lock is
    the more useful thing to show.
    """
    if device["state"] == STATE_DISCONNECTED:
        return "gone"
    lock_kind = device.get("lock_kind")
    if lock_kind:
        return f"locked by {lock_kind} pid {device.get('lock_pid')}"
    if device["state"] == STATE_CONNECTED_NO_FIRMWARE:
        return "no-firmware"
    return "free"


def _firmware_cell(device: dict[str, Any]) -> str:
    """The FIRMWARE/version column -- ``mbrelay``'s ``_firmware_cell``
    precedent (``server/src/mbrelay/cli.py`` around line 406) for
    distinguishing "unknown/never asked" from a real value, adapted to
    this store's fields: no dedicated firmware-version field exists here
    (see ``store.DeviceRecord``), so ``role``/``common_name`` from the
    device's own announcement stand in for it.
    """
    if device["state"] == STATE_ATTACHED_UNPROBED:
        return "(not probed yet)"
    if device["state"] == STATE_CONNECTED_NO_FIRMWARE:
        return "no firmware"
    role = device.get("role")
    common_name = device.get("common_name")
    if role and common_name:
        return f"{role}/{common_name}"
    return role or common_name or "-"


def _table(rows: list[list[str]], headers: list[str]) -> str:
    """A minimal fixed-width table renderer -- ported from ``mbrelay``'s
    own ``_table`` (``server/src/mbrelay/cli.py``), the precedent this
    ticket's Description points at for the STATE/short-uid/FIRMWARE/port
    rendering convention.
    """
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    header_line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    out = [header_line, "  ".join("-" * widths[i] for i in range(len(headers)))]
    for row in rows:
        out.append(
            "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip()
        )
    return "\n".join(out)


def cmd_list(args: argparse.Namespace) -> int:
    """``mbregistry list [--json]`` -- connect to the api socket, ask for
    every device, render it. Never touches ``store``/``locks`` directly
    (see module docstring).
    """
    socket_path = _resolve_path(args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH)

    try:
        sock = _connect(socket_path)
    except OSError as exc:
        print(
            f"mbregistry: registry unavailable at {socket_path}: {exc}",
            file=sys.stderr,
        )
        print(
            "mbregistry: is the daemon running? start it with 'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON

    try:
        try:
            resp = _request(sock, {"op": "list"})
        except (OSError, ConnectionError, json.JSONDecodeError) as exc:
            print(f"mbregistry: registry unavailable: {exc}", file=sys.stderr)
            return EXIT_NO_DAEMON
    finally:
        sock.close()

    if not resp.get("ok"):
        print(f"mbregistry: {resp.get('error', 'unknown error')}", file=sys.stderr)
        return EXIT_ERROR

    devices = sorted(
        resp.get("devices", []), key=lambda d: d.get("short_uid") or d["uid"]
    )

    if args.json:
        print(json.dumps({"devices": devices}, indent=2))
        return EXIT_OK

    if not devices:
        print("no devices known to the registry")
        return EXIT_OK

    rows = [
        [
            _state_cell(d),
            d.get("device_name") or "-",
            d.get("short_uid") or d["uid"][-8:],
            _firmware_cell(d),
            d.get("port") or "-",
        ]
        for d in devices
    ]
    print(_table(rows, ["STATE", "NAME", "UID", "FIRMWARE", "PORT"]))
    for d in devices:
        note = d.get("error_note")
        if note:
            label = d.get("device_name") or d.get("short_uid") or d["uid"]
            print(f"  {label}: {note}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# run -- construct and run the daemon + api, in the foreground
# ---------------------------------------------------------------------------


def assemble_daemon_and_api(
    *,
    store: Store,
    usbwatch: PortWatcher,
    socket_path: str | Path,
    serial_factory: Any = None,
    settle_s: float | None = None,
    probe_timeout_s: float = 1.6,
    peer_pid_fn: Any = None,
    flash_runner: Any = None,
) -> tuple[Daemon, RegistryAPIServer]:
    """Build one :class:`Daemon` and one :class:`RegistryAPIServer` that
    share a single ``threading.RLock`` -- ticket 009's fix for the
    cross-module concurrency gap ``docs/design/registry-api.md`` flagged
    (see the module docstring). This is the one place production code
    (:func:`cmd_run`) and tests (the run-then-list smoke test) construct
    that pairing, so the two can never drift apart -- a test never
    hand-builds its own ``Daemon``/``RegistryAPIServer`` pair with two
    separate locks by mistake.

    ``store``/``usbwatch`` are injected (a real :class:`Store` and
    :class:`~mbtools.registry.usbwatch.PollingPortWatcher` in production,
    a ``tmp_path`` ``Store`` and ``FakeUSBSource`` in tests, mirroring
    every other module's injection convention this sprint established).
    ``serial_factory``/``settle_s``/``probe_timeout_s`` are forwarded to
    ``Daemon`` verbatim (its own probe-testing escape hatch);
    ``peer_pid_fn``/``flash_runner`` are forwarded to
    ``RegistryAPIServer``/``FlashOp`` respectively, for the same reason.
    Callers do not start or stop either object -- that stays their own
    responsibility (:meth:`RegistryAPIServer.start`/``.stop``,
    :meth:`Daemon.run`/``.run_once``), matching how every other test in
    this sprint drives these classes.
    """
    shared_lock = threading.RLock()
    daemon = Daemon(
        usbwatch=usbwatch,
        store=store,
        serial_factory=serial_factory,
        settle_s=settle_s,
        probe_timeout_s=probe_timeout_s,
        lock=shared_lock,
    )
    flash_op = FlashOp(locks=daemon.locks, store=store, runner=flash_runner)
    api = RegistryAPIServer(
        socket_path=socket_path,
        store=store,
        locks=daemon.locks,
        flash_op=flash_op,
        peer_pid_fn=peer_pid_fn,
        lock=shared_lock,
    )
    return daemon, api


def cmd_run(args: argparse.Namespace) -> int:
    """``mbregistry run`` -- the daemon's actual entry point. Constructs
    the real production pipeline (:class:`~mbtools.registry.usbwatch.PollingPortWatcher`,
    a real :class:`Store`) via :func:`assemble_daemon_and_api`, starts the
    api socket, and runs the daemon's poll loop in the foreground until a
    signal asks it to stop. This is what a systemd ``ExecStart=`` invokes
    (see :func:`render_systemd_unit`), and also what a developer runs
    directly on macOS (module docstring, "macOS foreground dev use").
    """
    socket_path = _resolve_path(args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH)
    db_path = _resolve_path(args.db, _DB_ENV_VAR, DEFAULT_DB_PATH)

    store = Store(db_path)
    daemon, api = assemble_daemon_and_api(
        store=store, usbwatch=PollingPortWatcher(), socket_path=socket_path
    )

    stop_event = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            # Not the main thread of the main interpreter (e.g. embedded
            # in a test harness), or the platform doesn't support this
            # signal -- best-effort only; mbregistry run's real,
            # production invocation is always the main thread.
            pass

    api.start()
    print(
        f"mbregistry: listening on {socket_path}, store at {db_path}",
        file=sys.stderr,
    )
    try:
        daemon.run(interval_s=args.interval, stop=stop_event.is_set)
    finally:
        api.stop()
        store.close()
    return EXIT_OK


# ---------------------------------------------------------------------------
# install-service -- write the systemd unit
# ---------------------------------------------------------------------------

#: Rendered with ``str.format(exec_start=...)``. ``RuntimeDirectory=``/
#: ``StateDirectory=`` are systemd's own directive for "create this
#: subdirectory of /run or /var/lib, owned by this service, before
#: ExecStart runs" -- the Design Rationale's "install-service's systemd
#: unit must create both directories (or rely on RuntimeDirectory=/
#: StateDirectory= systemd directives)" satisfied without this module
#: ever shelling out to ``mkdir`` itself. Named ``mbregistry.service``,
#: distinct from the fleet's existing ``mbrelay.service``/old ``mbdeploy
#: serve`` per sprint.md's Migration Concerns -- this ticket does not
#: need to detect or retire those.
_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=mbregistry -- micro:bit device registry daemon
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=2
RuntimeDirectory=mbregistry
StateDirectory=mbregistry

[Install]
WantedBy=multi-user.target
"""


def render_systemd_unit(exec_start: str | None = None) -> str:
    """The systemd unit's text. ``exec_start`` defaults to invoking
    ``mbregistry run`` through the current interpreter
    (``{sys.executable} -m mbtools.registry.cli run``) -- the same
    "invoke through the running interpreter, not a bare PATH lookup"
    reasoning ``flash.py``'s own ``_PYOCD`` already documents: mbtools is
    typically installed into an isolated venv whose ``bin/`` directory is
    not on the ``PATH`` systemd uses for a unit's ``ExecStart=``, but the
    package is always importable through the interpreter that installed
    it.
    """
    if exec_start is None:
        exec_start = f"{sys.executable} -m mbtools.registry.cli run"
    return _SYSTEMD_UNIT_TEMPLATE.format(exec_start=exec_start)


def cmd_install_service(args: argparse.Namespace) -> int:
    """``mbregistry install-service`` -- write the systemd unit file and
    print (not run) the ``systemctl`` commands the operator needs.
    Printing rather than running them is this ticket's implementer
    choice (its Description explicitly allows either) -- it keeps this
    command safe to exercise without root or a real systemd, matching
    sprint.md's Test Strategy ("installing it into a real systemd is out
    of scope for automated tests").
    """
    unit_path = Path(args.output) if args.output else DEFAULT_UNIT_PATH
    unit_text = render_systemd_unit()

    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit_text)
    except OSError as exc:
        print(f"mbregistry: could not write {unit_path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"mbregistry: wrote {unit_path}", file=sys.stderr)
    print("Run as root to enable and start the service:", file=sys.stderr)
    print("  systemctl daemon-reload", file=sys.stderr)
    print("  systemctl enable --now mbregistry.service", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# argument parsing / entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mbregistry", description="local micro:bit device registry daemon"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list", help="list devices known to the registry")
    list_p.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    list_p.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or ${_SOCKET_ENV_VAR})",
    )
    list_p.set_defaults(func=cmd_list)

    run_p = sub.add_parser("run", help="run the registry daemon in the foreground")
    run_p.add_argument(
        "--socket",
        help=f"api socket path (default {DEFAULT_SOCKET_PATH}, or ${_SOCKET_ENV_VAR})",
    )
    run_p.add_argument(
        "--db",
        help=f"device database path (default {DEFAULT_DB_PATH}, or ${_DB_ENV_VAR})",
    )
    run_p.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=f"USB poll interval in seconds (default {DEFAULT_INTERVAL_S})",
    )
    run_p.set_defaults(func=cmd_run)

    install_p = sub.add_parser(
        "install-service", help="write the mbregistry systemd unit"
    )
    install_p.add_argument(
        "--output",
        help=f"where to write the unit file (default {DEFAULT_UNIT_PATH})",
    )
    install_p.set_defaults(func=cmd_install_service)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    # Without this guard, `python -m mbtools.registry.cli ...` -- exactly
    # what render_systemd_unit()'s ExecStart= invokes, and what this
    # module's own docstring documents as the systemd entry point -- only
    # imports the module and exits 0 without ever calling main(). Found
    # on ticket 010's real-hardware pass: `mbregistry` (the console
    # script, which calls main() directly per pyproject.toml's
    # `[project.scripts]`) worked, and every automated test in this
    # sprint drives `cli.main()` or the console script directly, so
    # nothing before this ticket exercised the `-m` invocation path that
    # production's systemd unit actually uses.
    main()
