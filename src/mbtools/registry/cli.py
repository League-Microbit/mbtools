"""``mbregistry`` — CLI entry point for the device-registry daemon.

Per sprint.md's Architecture (module "cli"), this is a client of the API
socket for ``list`` (per spec §3.5, "other programs consult it through a
service" — this module never reads ``store``/``locks`` directly, even
though it runs on the same host as the daemon it's talking to), the
process that constructs and runs the daemon for ``run``, and a
systemd-unit-file and non-root-USB-access-udev-rule writer for
``install-service`` (ticket 008).

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

**This is also where ticket 009 makes the new subsystems reachable at
all.** Tickets 004–008 built and unit-tested
:mod:`mbtools.registry.peering` (mDNS + ZeroMQ) and
:mod:`mbtools.registry.remote_api` (the TCP control/data plane) in
isolation; :func:`assemble_registry` is the one place they are wired
into a real ``mbregistry run`` alongside ``daemon``/``api``, all four
sharing the same ``threading.RLock`` above. ``--peer HOST[:PORT]``,
``--remote-port``, ``--peer-pub-port``, ``--peer-snapshot-port``, and
``--auth-token``/``$MBREGISTRY_TOKEN`` (Decisions 6/7) are this module's
own new flags for that assembly — see :func:`cmd_run`'s own docstring.

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
import getpass
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from mbtools.common import EXIT_ERROR, EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import DEFAULT_SOCKET_PATH, RegistryAPIServer
from mbtools.registry.api_windows import WindowsPipeAPIServer
from mbtools.registry.client import (
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.client import SOCKET_ENV_VAR as _SOCKET_ENV_VAR
from mbtools.registry.client import resolve_local_api_address
from mbtools.registry.console_compat.names_api import NamesAPI
from mbtools.registry.console_compat.relay_pool import (
    DEFAULT_NAMES_API_PORT,
    DEFAULT_POOL_PORT,
    RelayPool,
)
from mbtools.registry.daemon import DEFAULT_INTERVAL_S, Daemon
from mbtools.registry.flash import FlashOp
from mbtools.registry.peering import (
    DEFAULT_PUB_PORT,
    DEFAULT_SNAPSHOT_PORT,
    PeerDiscovery,
)
from mbtools.registry.remote_api import DEFAULT_REMOTE_PORT, RemoteAPIServer
from mbtools.registry.render import render_json, render_table
from mbtools.registry.service_windows import (
    cmd_install_service_windows,
    run_as_windows_service,
)
from mbtools.registry.store import DEFAULT_DB_PATH, Store
from mbtools.registry.usbwatch import PollingPortWatcher, PortWatcher

__all__ = [
    "main",
    "build_parser",
    "cmd_list",
    "cmd_run",
    "cmd_install_service",
    "assemble_daemon_and_api",
    "assemble_registry",
    "assemble_relay_pool",
    "assemble_names_api",
    "render_systemd_unit",
    "render_udev_rule",
    "DEFAULT_UNIT_PATH",
    "DEFAULT_UDEV_RULE_PATH",
]

#: Standard systemd system-unit install location -- ``install-service``'s
#: default target, overridable with ``--output`` (every test uses that
#: override; writing to the real path needs root, per the ticket's own
#: "installing into a real systemd is not part of this ticket's automated
#: tests" scoping).
DEFAULT_UNIT_PATH = Path("/etc/systemd/system/mbregistry.service")

#: Standard ``udev`` rules.d install location -- ``install-service``'s
#: default target for ticket 008's non-root-USB-access rule, overridable
#: with ``--udev-output`` (every test uses that override, mirroring
#: ``DEFAULT_UNIT_PATH``'s own "writing to the real path needs root"
#: scoping). ``99-`` sorts after every distro-shipped rule so this one's
#: ``GROUP=``/``MODE=``/``TAG+=`` assignments win if anything else also
#: matches this VID:PID.
DEFAULT_UDEV_RULE_PATH = Path("/etc/udev/rules.d/99-mbregistry-cmsis-dap.rules")

#: The micro:bit DAPLink interface's fixed VID:PID (sprint.md SUC-005,
#: Decision 9; confirmed on the Nolanet nodes -- see CLAUDE.md's hardware
#: test hosts table and ``docs/acceptance/002-hardware.md``). Shared
#: between the tty/usb/hidraw match rules in :func:`render_udev_rule` so
#: they can never drift apart from each other.
_USB_VENDOR_ID = "0d28"
_USB_PRODUCT_ID = "0204"

#: The group :func:`render_udev_rule`'s rules grant access via, alongside
#: ``TAG+="uaccess"`` -- see that function's own docstring for why both
#: mechanisms are written into the same rule rather than picking just one.
_UDEV_GROUP = "plugdev"

_DB_ENV_VAR = "MBREGISTRY_DB"

#: Ticket 009's own three port env vars (Decision 7) and the auth-token
#: env var (Decision 6) -- same flag > env var > default precedence as
#: ``--socket``/``--db`` already establish, resolved by
#: ``_resolve_int``/``_resolve_token`` below.
_REMOTE_PORT_ENV_VAR = "MBREGISTRY_REMOTE_PORT"
_PEER_PUB_PORT_ENV_VAR = "MBREGISTRY_PEER_PUB_PORT"
_PEER_SNAPSHOT_PORT_ENV_VAR = "MBREGISTRY_PEER_SNAPSHOT_PORT"
_TOKEN_ENV_VAR = "MBREGISTRY_TOKEN"


# ---------------------------------------------------------------------------
# path/value resolution -- flag > env var > module default (ticket 009's own
# "socket/DB paths overridable by flags or env" acceptance criterion,
# extended by ticket 009 itself to the new port/token flags below).
# Socket-path precedence itself lives in ``registry.client`` (ticket
# 001's extraction as ``resolve_socket_path``, and ticket 006's
# ``resolve_local_api_address``, imported above), since that module is
# also what ``deploy.cli``/``serial.cli``/``relay.cli`` use to resolve it
# identically; ``_resolve_path`` stays here only for the db path, which
# is this daemon's own concern, not the client library's.
# ---------------------------------------------------------------------------


def _resolve_path(flag_value: str | None, env_var: str, default: Path) -> Path:
    """``flag_value`` wins if given; else ``$env_var`` if set; else
    ``default``. Shared by every ``mbregistry`` subcommand that takes a
    db path.
    """
    if flag_value:
        return Path(flag_value)
    env_value = os.environ.get(env_var)
    if env_value:
        return Path(env_value)
    return default


#: Sprint 005 ticket 005's platform dispatch for the local query/control
#: API address -- shared by :func:`cmd_run` and :func:`cmd_list` (the two
#: ``mbregistry`` subcommands that need to know the daemon's own
#: local-API address). Ticket 006 moved the actual logic to
#: :func:`mbtools.registry.client.resolve_local_api_address` so
#: ``deploy.cli``/``serial.cli``/``relay.cli`` can share it too (each of
#: those three was still calling ``resolve_socket_path`` directly and
#: raising ``TypeError`` on Windows -- see that function's docstring for
#: the full story). This name stays as a thin alias, unchanged in
#: signature and behavior, so every existing caller/test here
#: (``tests/registry/cli/test_cli_run_windows.py``) needs no changes.
_resolve_local_api_address = resolve_local_api_address


def _resolve_int(flag_value: int | None, env_var: str, default: int) -> int:
    """Same precedence as :func:`_resolve_path`, for the port flags
    ``--remote-port``/``--peer-pub-port``/``--peer-snapshot-port``
    (argparse already parses a given flag to ``int``; ``None`` means "not
    given on the command line").
    """
    if flag_value is not None:
        return flag_value
    env_value = os.environ.get(env_var)
    if env_value:
        return int(env_value)
    return default


def _resolve_operating_user(flag_value: str | None) -> str:
    """``--user`` (ticket 008's own escape hatch for :func:`cmd_install_service`)
    wins if given; else ``$SUDO_USER``; else ``$USER``; else
    :func:`getpass.getuser`. No env-var-name parameter like the other
    ``_resolve_*`` helpers here, since this one checks two env vars in a
    fixed order rather than one.

    ``install-service`` is typically invoked with ``sudo`` (writing
    ``DEFAULT_UNIT_PATH``/``DEFAULT_UDEV_RULE_PATH`` needs root), so
    ``getpass.getuser()``/``$USER`` alone would report ``"root"`` --
    ``$SUDO_USER`` (set by ``sudo`` itself to the *invoking* user) is
    checked first so the ``usermod -aG plugdev <user>`` line
    :func:`cmd_install_service` prints names the actual operator, not
    ``root``, in the common case. Falls through to ``$USER``/
    :func:`getpass.getuser` for a non-``sudo`` invocation (e.g. a
    root-shell host, or a test).
    """
    if flag_value:
        return flag_value
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        return sudo_user
    user = os.environ.get("USER")
    if user:
        return user
    return getpass.getuser()


def _resolve_token(flag_value: str | None, env_var: str) -> str | None:
    """Same precedence as :func:`_resolve_path`, for ``--auth-token`` --
    but with no third ``default`` argument, since Decision 6's default is
    simply "unset" (``None``), never a real value.
    """
    if flag_value:
        return flag_value
    return os.environ.get(env_var) or None


def _parse_peer_spec(spec: str) -> tuple[str, int]:
    """``"HOST[:PORT]"`` -> ``(host, port)`` -- ``--peer``'s own argument
    shape. ``PORT``, if omitted, defaults to :data:`DEFAULT_REMOTE_PORT`
    (the project-wide fixed default, sprint.md Decision 7) rather than
    this run's own ``--remote-port`` value -- a bare ``--peer HOST``
    assumes the peer itself is running on the project's default remote
    port, independent of what this host happens to be configured to
    listen on for its *own* remote API.

    Raises :class:`ValueError` (caught by :func:`cmd_run`, printed and
    turned into an ``EXIT_ERROR`` exit rather than a traceback) for an
    empty host or a non-numeric port.
    """
    host, sep, port_str = spec.partition(":")
    if not host:
        raise ValueError(f"--peer: empty host in {spec!r}")
    if not sep:
        return host, DEFAULT_REMOTE_PORT
    try:
        port = int(port_str)
    except ValueError as exc:
        raise ValueError(f"--peer: invalid port in {spec!r}") from exc
    return host, port


# ---------------------------------------------------------------------------
# list -- a client of the api socket, per spec section 3.5
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    """``mbregistry list [--json]`` -- connect to the api socket, ask for
    every device, render it. Never touches ``store``/``locks`` directly
    (see module docstring). Ticket 001's extraction: the socket
    connect/framing/JSON that used to live inline here now lives in
    :mod:`mbtools.registry.client`, so this is that module's first
    caller rather than a parallel implementation of the same protocol.
    Ticket 002's extraction: the table/JSON rendering that used to live
    inline here (``_table``/``_state_cell``/``_firmware_cell``) now lives
    in :mod:`mbtools.registry.render`, so this is that module's first
    caller too -- ``mbdeploy list`` (ticket 008) is its second, sharing
    the same functions rather than a parallel rendering implementation
    (spec §4.3, SUC-003).

    Sprint 005 ticket 005: :func:`_resolve_local_api_address` resolves
    to the Windows named pipe (a plain ``str``) on ``sys.platform ==
    "win32"``, so this is also the first Windows-aware caller of
    :class:`~mbtools.registry.client.RegistryClient`'s own new pipe
    transport -- everywhere else, unchanged.
    """
    socket_path = _resolve_local_api_address(args.socket, _SOCKET_ENV_VAR)

    try:
        with RegistryClient(socket_path) as client:
            devices = client.list()
    except RegistryUnavailable as exc:
        print(f"mbregistry: {exc}", file=sys.stderr)
        print(
            "mbregistry: is the daemon running? start it with 'mbregistry run'",
            file=sys.stderr,
        )
        return EXIT_NO_DAEMON
    except RegistryClientError as exc:
        print(f"mbregistry: {exc.message}", file=sys.stderr)
        return exc.exit_code

    if args.json:
        print(json.dumps(render_json(devices), indent=2))
        return EXIT_OK

    print(render_table(devices))
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
    lock: threading.RLock | None = None,
    event_callback: Any = None,
    lock_display_callback: Any = None,
    name_set_callback: Any = None,
    name_clear_callback: Any = None,
) -> tuple[Daemon, RegistryAPIServer | WindowsPipeAPIServer]:
    """Build one :class:`Daemon` and one local-API server that share a
    single ``threading.RLock`` -- ticket 009's fix for the cross-module
    concurrency gap ``docs/design/registry-api.md`` flagged (see the
    module docstring). This is the one place production code
    (:func:`assemble_registry`, and through it :func:`cmd_run`) and tests
    (the run-then-list smoke test) construct that pairing, so the two can
    never drift apart -- a test never hand-builds its own
    ``Daemon``/local-API-server pair with two separate locks by mistake.

    **Sprint 005 ticket 005**: the local-API server is
    :class:`RegistryAPIServer` (a Unix socket) on every platform except
    ``sys.platform == "win32"``, where it is
    :class:`~mbtools.registry.api_windows.WindowsPipeAPIServer`
    (a named pipe) instead -- the one piece of this function's assembly
    that differs on Windows; ``Daemon``, its shared ``threading.RLock``,
    and every other parameter here are built identically on every
    platform. ``socket_path`` doubles as the pipe name on Windows
    (:func:`cmd_run`/:func:`assemble_registry` resolve it via
    :func:`_resolve_local_api_address`, which already returns a plain
    ``str`` pipe name there -- see that function's own docstring).
    :class:`WindowsPipeAPIServer` has no ``flash`` op (ticket 003's own
    scope -- ``flash`` is ``RegistryAPIServer``'s own addition), so no
    :class:`~mbtools.registry.flash.FlashOp` is constructed for it.

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

    ``lock`` (ticket 009), if given, is used as the shared
    ``threading.RLock`` instead of a freshly constructed one -- this is
    what lets :func:`assemble_registry` extend the same lock to
    ``registry.remote_api``/``registry.peering`` too, per this function's
    own "one shared lock" contract. Every pre-ticket-009 caller/test that
    omits it (every one that existed before this ticket) is unaffected --
    a fresh ``RLock`` is still built exactly as before.
    ``event_callback``/``lock_display_callback`` (ticket 009), if given,
    are forwarded verbatim to :class:`Daemon` (which forwards
    ``lock_display_callback`` on into its own ``LockManager`` -- see
    ``daemon.py``'s own docstring); :func:`assemble_registry` passes
    ``registry.peering.PeerDiscovery.publish_daemon_event``/
    ``publish_lock_event`` here. Both default to ``None`` (no-op),
    unaffected for every pre-ticket-009 caller/test.

    ``name_set_callback``/``name_clear_callback`` (sprint 004, ticket
    005) are forwarded verbatim to :class:`RegistryAPIServer` (which
    fires them itself, from ``BaseAPIServer._op_names_set``/
    ``_op_names_clear`` -- see that module's own docstring for why
    ``Store`` doesn't own this hook itself, the same "Daemon/LockManager,
    not Store, own the callback" reasoning as ``event_callback``/
    ``lock_display_callback`` above); :func:`assemble_registry` passes
    ``PeerDiscovery.publish_name_set``/``publish_name_clear`` here. Both
    default to ``None`` (no-op), unaffected for every pre-ticket-005
    caller/test.
    """
    shared_lock = lock if lock is not None else threading.RLock()
    daemon = Daemon(
        usbwatch=usbwatch,
        store=store,
        serial_factory=serial_factory,
        settle_s=settle_s,
        probe_timeout_s=probe_timeout_s,
        lock=shared_lock,
        event_callback=event_callback,
        lock_display_callback=lock_display_callback,
    )
    api: RegistryAPIServer | WindowsPipeAPIServer
    if sys.platform == "win32":
        api = WindowsPipeAPIServer(
            pipe_name=str(socket_path),
            store=store,
            locks=daemon.locks,
            peer_pid_fn=peer_pid_fn,
            lock=shared_lock,
            name_set_callback=name_set_callback,
            name_clear_callback=name_clear_callback,
        )
    else:
        flash_op = FlashOp(locks=daemon.locks, store=store, runner=flash_runner)
        api = RegistryAPIServer(
            socket_path=socket_path,
            store=store,
            locks=daemon.locks,
            flash_op=flash_op,
            peer_pid_fn=peer_pid_fn,
            lock=shared_lock,
            name_set_callback=name_set_callback,
            name_clear_callback=name_clear_callback,
        )
    return daemon, api


def assemble_registry(
    *,
    store: Store,
    usbwatch: PortWatcher,
    socket_path: str | Path,
    serial_factory: Any = None,
    settle_s: float | None = None,
    probe_timeout_s: float = 1.6,
    peer_pid_fn: Any = None,
    flash_runner: Any = None,
    remote_host: str = "0.0.0.0",
    remote_port: int = DEFAULT_REMOTE_PORT,
    peer_pub_port: int = DEFAULT_PUB_PORT,
    peer_snapshot_port: int = DEFAULT_SNAPSHOT_PORT,
    auth_token: str | None = None,
    peering_host: str | None = None,
    advertise_address: str | None = None,
    zeroconf: Any = None,
    zmq: Any = None,
    stream_serial_factory: Any = None,
    lock: threading.RLock | None = None,
) -> tuple[Daemon, RegistryAPIServer | WindowsPipeAPIServer, RemoteAPIServer, PeerDiscovery]:
    """Ticket 009's real assembly: everything :func:`assemble_daemon_and_api`
    already builds, *plus* a :class:`~mbtools.registry.remote_api.RemoteAPIServer`
    and a :class:`~mbtools.registry.peering.PeerDiscovery`, all four
    sharing one ``threading.RLock`` -- the acceptance criterion this
    ticket adds on top of ticket 008's own "``daemon``/``api`` share a
    lock" (see :func:`assemble_daemon_and_api`'s own docstring). This is
    what :func:`cmd_run` actually calls; ``assemble_daemon_and_api``
    remains independently callable (and is: this function calls it) for
    any caller/test that only wants the pre-ticket-009 daemon+local-api
    pairing -- see that function's own "Existing tests... must keep
    passing" note.

    **Sprint 005 ticket 005**: the local-API server's own Windows branch
    (:class:`~mbtools.registry.api_windows.WindowsPipeAPIServer` in
    place of :class:`RegistryAPIServer`) lives entirely inside
    :func:`assemble_daemon_and_api` above, which this function calls
    unchanged -- ``remote_api``/``peering``/``console_compat.*`` are
    TCP-based already and need no platform branch of their own here
    (sprint.md's Impact section).

    Construction order: :class:`PeerDiscovery` is built first (but never
    started here -- see below), so its
    ``publish_daemon_event``/``publish_lock_event`` adapters exist to
    hand to :func:`assemble_daemon_and_api` as
    ``event_callback``/``lock_display_callback``. This ordering is safe
    even though ``peer_discovery`` has no PUB socket bound yet --
    :meth:`~mbtools.registry.peering.PeerDiscovery.publish_event` is a
    documented no-op until :meth:`~mbtools.registry.peering.PeerDiscovery.start`
    runs, so a daemon/lock event fired before this module's own
    ``cmd_run`` gets around to calling ``peering.start()`` is simply
    dropped, exactly like every pre-ticket-009 caller that never wired a
    callback at all.

    ``remote_port`` is used both as ``RemoteAPIServer``'s own listening
    port and as the port :class:`PeerDiscovery` advertises in its own
    TXT record (``TXT_REMOTE_PORT``) -- the same TCP port either way, per
    sprint.md's Decision 7 (an mDNS-discovered peer's ``endpoint`` in
    ``store``'s ``peer`` table is precisely "that peer's remote API
    port"). ``peer_pub_port``/``peer_snapshot_port`` are
    ``PeerDiscovery``'s own ZMQ PUB/snapshot ports, forwarded verbatim.
    ``auth_token`` (Decision 6) is forwarded to both
    ``RemoteAPIServer`` and ``PeerDiscovery`` -- see each module's own
    "Auth"/"Peering handshake auth" docstring note for what each does
    with it. ``peering_host``/``advertise_address``/``zeroconf``/``zmq``
    are ``PeerDiscovery``'s own constructor escape hatches, forwarded
    verbatim (a test overrides ``zeroconf``/``advertise_address`` the
    same way ``tests/registry/peering`` already does).
    ``stream_serial_factory`` is ``RemoteAPIServer``'s own ``serial_factory``
    test seam for its ``stream`` op (ticket 007) -- named distinctly here
    so it can never be confused with this function's own ``serial_factory``
    (the daemon's probe-path seam, an entirely different concern).

    ``lock`` (sprint 004, ticket 006), if given, is used as the shared
    ``threading.RLock`` instead of one freshly constructed here -- this is
    what lets :func:`cmd_run` extend the exact same lock to a
    ``console_compat.relay_pool.RelayPool`` too (via
    :func:`assemble_relay_pool`), matching every other component this
    function already shares one lock across. Every pre-ticket-006 caller/
    test that omits it is unaffected -- a fresh ``RLock`` is still built
    exactly as before, and this function's own return value/arity is
    unchanged (a ``RelayPool`` is assembled and returned separately, by
    :func:`assemble_relay_pool`, not folded into this tuple -- changing
    this function's own 4-tuple return would break every existing caller
    that unpacks it).

    Callers do not start or stop any of the four returned objects --
    that stays their own responsibility, matching
    :func:`assemble_daemon_and_api`'s own convention. Shutdown order
    matters (this ticket's own acceptance criterion, "never leaves a
    socket bound after the process should have exited") -- see
    :func:`cmd_run`'s own ``finally`` block for the order this project
    uses: ``peering.stop()``, then ``remote_api.stop()``, then
    ``api.stop()``, then ``store.close()`` last, since nothing may still
    be touching ``store`` by the time it closes.
    """
    shared_lock = lock if lock is not None else threading.RLock()

    peer_discovery = PeerDiscovery(
        store=store,
        host=peering_host,
        advertise_address=advertise_address,
        remote_port=remote_port,
        pub_port=peer_pub_port,
        snapshot_port=peer_snapshot_port,
        auth_token=auth_token,
        lock=shared_lock,
        zeroconf=zeroconf,
        zmq=zmq,
    )
    daemon, api = assemble_daemon_and_api(
        store=store,
        usbwatch=usbwatch,
        socket_path=socket_path,
        serial_factory=serial_factory,
        settle_s=settle_s,
        probe_timeout_s=probe_timeout_s,
        peer_pid_fn=peer_pid_fn,
        flash_runner=flash_runner,
        lock=shared_lock,
        event_callback=peer_discovery.publish_daemon_event,
        lock_display_callback=peer_discovery.publish_lock_event,
        name_set_callback=peer_discovery.publish_name_set,
        name_clear_callback=peer_discovery.publish_name_clear,
    )
    remote_api = RemoteAPIServer(
        host=remote_host,
        port=remote_port,
        store=store,
        locks=daemon.locks,
        auth_token=auth_token,
        lock=shared_lock,
        serial_factory=stream_serial_factory,
    )
    return daemon, api, remote_api, peer_discovery


def assemble_relay_pool(
    *,
    store: Store,
    locks: Any,
    lock: threading.RLock,
    host: str = "0.0.0.0",
    port: int = DEFAULT_POOL_PORT,
    names_api_port: int = DEFAULT_NAMES_API_PORT,
    instance_host: str | None = None,
    advertise_address: str | None = None,
    zeroconf: Any = None,
) -> RelayPool:
    """Sprint 004 ticket 006: build the ``console_compat.relay_pool.
    RelayPool`` that gives robot-console (unmodified) a freshly-reset,
    normalized local relay per TCP connection -- see that module's own
    docstring for the full contract.

    A separate assembly function, not folded into :func:`assemble_registry`
    itself, so that function's own return value/arity stays exactly as
    every pre-ticket-006 caller/test already unpacks it (see its own
    ``lock`` parameter docstring note). :func:`cmd_run` calls this
    function with ``store``/``locks=daemon.locks``/the same ``lock`` it
    passed to :func:`assemble_registry`, so a ``relay``-kind lock acquired
    here is checked against, and visible to, every other component that
    shares the one ``LockManager`` table (``LockManager`` has no internal
    lock of its own -- every access to it, from any component, must go
    through this same ``threading.RLock``, per ``registry.locks``'s own
    module docstring).

    ``store``/``locks`` are injected, never constructed here, mirroring
    every other ``assemble_*`` function in this module. ``host``/``port``/
    ``names_api_port``/``instance_host``/``advertise_address``/``zeroconf``
    are forwarded verbatim to :class:`RelayPool` -- see its own
    constructor docstring for each one's meaning and default.
    """
    return RelayPool(
        store=store,
        locks=locks,
        host=host,
        port=port,
        names_api_port=names_api_port,
        instance_host=instance_host,
        advertise_address=advertise_address,
        lock=lock,
        zeroconf=zeroconf,
    )


def assemble_names_api(
    *,
    store: Store,
    lock: threading.RLock,
    host: str = "0.0.0.0",
    port: int = DEFAULT_NAMES_API_PORT,
    name_set_callback: Any = None,
    name_clear_callback: Any = None,
) -> NamesAPI:
    """Sprint 004 ticket 007: build the ``console_compat.names_api.
    NamesAPI`` that serves the ``GET/PUT/DELETE /names/<name>`` HTTP
    contract robot-console already expects -- see that module's own
    docstring for the full contract.

    A separate assembly function, not folded into :func:`assemble_registry`
    itself, for the same reason :func:`assemble_relay_pool` isn't --
    that function's own 4-tuple return stays exactly as every existing
    caller/test unpacks it. :func:`cmd_run` calls this function with
    ``store`` and the same ``lock`` it passed to
    :func:`assemble_registry`/:func:`assemble_relay_pool`, so a write this
    listener makes is serialized against every other component sharing
    that one ``threading.RLock``.

    ``port`` defaults to :data:`~mbtools.registry.console_compat.
    relay_pool.DEFAULT_NAMES_API_PORT` -- the exact port
    :func:`assemble_relay_pool`'s own ``names_api_port`` (also defaulted
    from the same constant) advertises in its mDNS TXT ``registry=`` key,
    so the two never drift apart as long as neither caller overrides one
    without the other (:func:`cmd_run` doesn't -- there is no
    ``--names-api-port`` flag yet, deliberately out of this ticket's own
    scope, same as :func:`assemble_relay_pool`'s own "no
    ``--relay-pool-port``/``--names-api-port`` flags" note).

    ``name_set_callback``/``name_clear_callback`` are forwarded verbatim
    to :class:`~mbtools.registry.console_compat.names_api.NamesAPI` --
    :func:`cmd_run` passes ``PeerDiscovery.publish_name_set``/
    ``publish_name_clear`` here, the exact same two callables
    :func:`assemble_registry` already hands to
    :class:`~mbtools.registry.api.RegistryAPIServer` for ticket 005's
    local-socket ``names_set``/``names_clear`` ops -- one write, from
    either surface, always replicates the same way.
    """
    return NamesAPI(
        store=store,
        host=host,
        port=port,
        lock=lock,
        name_set_callback=name_set_callback,
        name_clear_callback=name_clear_callback,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """``mbregistry run`` -- the daemon's actual entry point. Dispatches
    to :func:`_run_registry` (the real production pipeline), either
    directly -- run in the foreground until a POSIX signal asks it to
    stop, exactly as before this ticket -- or, when ``--windows-service``
    is given, through
    :func:`~mbtools.registry.service_windows.run_as_windows_service`
    (sprint 005 ticket 005).

    **``--windows-service`` (ticket 005)**: this is the flag the
    ``sc.exe create`` command :func:`~mbtools.registry.service_windows
    .render_windows_service_install` renders actually invokes (see
    :func:`cmd_install_service`'s own Windows branch) -- *not* meant for
    interactive use. ``service_windows.py``'s own module docstring, "A
    plain console app is not a real service", is why this indirection
    exists at all: the Windows SCM kills a plain console process that
    never calls ``StartServiceCtrlDispatcherW``, so the SCM-launched
    entry point must go through :func:`run_as_windows_service`, which
    calls :func:`_run_registry` as its own ``main(stop_event)`` --
    ``run_as_windows_service`` sets that ``stop_event`` when the SCM
    delivers ``SERVICE_CONTROL_STOP``, the exact same "poll loop notices
    a stop signal" shape the non-service branch below gives
    :func:`_run_registry` via a POSIX-signal-set ``threading.Event``.

    Per this ticket's own Testing note, peering (mDNS advertise/browse +
    the ZeroMQ event bus) is always started, never opt-in -- matching
    ticket 004/005's own "each ``mbregistry`` advertises and browses"
    architecture text, which describes this as inherent behavior, not a
    flag-gated extra. ``--peer`` only adds *explicit* peers on top of
    whatever mDNS already finds; it never replaces mDNS discovery.
    """
    if args.windows_service:
        return run_as_windows_service(lambda stop_event: _run_registry(args, stop_event))

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

    return _run_registry(args, stop_event)


def _run_registry(args: argparse.Namespace, stop_event: threading.Event) -> int:
    """The real production pipeline behind ``mbregistry run``: constructs
    (:class:`~mbtools.registry.usbwatch.PollingPortWatcher`, a real
    :class:`Store`) via :func:`assemble_registry`, starts the local api
    (a Unix socket, or a named pipe on ``sys.platform == "win32"`` --
    see :func:`assemble_daemon_and_api`'s own docstring), the TCP remote
    api, and mDNS/ZeroMQ peering (ticket 009), connects any explicit
    ``--peer`` targets, and runs the daemon's poll loop until
    ``stop_event`` is set. This is what a systemd ``ExecStart=`` invokes
    (see :func:`render_systemd_unit`), what a developer runs directly on
    macOS (module docstring, "macOS foreground dev use"), and (sprint
    005 ticket 005) what
    :func:`~mbtools.registry.service_windows.run_as_windows_service`
    calls as its own ``main`` on Windows -- peering/remote_api need no
    elevated privilege beyond what ``--socket``/``--db`` already
    require, since every new port (7440/7442/7443 by default) is a
    plain unprivileged TCP/UDP port.

    Split out of :func:`cmd_run` itself by this ticket so that function
    can call this same pipeline either directly (the POSIX-signal-driven
    foreground path, unchanged from before this ticket) or wrapped in
    :func:`run_as_windows_service` -- ``stop_event`` is owned by
    whichever of those two callers constructs it, not by this function.
    """
    socket_path = _resolve_local_api_address(args.socket, _SOCKET_ENV_VAR)
    db_path = _resolve_path(args.db, _DB_ENV_VAR, DEFAULT_DB_PATH)
    remote_port = _resolve_int(args.remote_port, _REMOTE_PORT_ENV_VAR, DEFAULT_REMOTE_PORT)
    peer_pub_port = _resolve_int(args.peer_pub_port, _PEER_PUB_PORT_ENV_VAR, DEFAULT_PUB_PORT)
    peer_snapshot_port = _resolve_int(
        args.peer_snapshot_port, _PEER_SNAPSHOT_PORT_ENV_VAR, DEFAULT_SNAPSHOT_PORT
    )
    auth_token = _resolve_token(args.auth_token, _TOKEN_ENV_VAR)

    try:
        peer_specs = [_parse_peer_spec(spec) for spec in (args.peer or [])]
    except ValueError as exc:
        print(f"mbregistry: {exc}", file=sys.stderr)
        return EXIT_ERROR

    store = Store(db_path)
    shared_lock = threading.RLock()
    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=PollingPortWatcher(),
        socket_path=socket_path,
        remote_port=remote_port,
        peer_pub_port=peer_pub_port,
        peer_snapshot_port=peer_snapshot_port,
        auth_token=auth_token,
        lock=shared_lock,
    )

    # Sprint 004 ticket 006: the robot-console-compatibility pool port,
    # alongside daemon/api/remote_api/peering above, sharing the same
    # shared_lock -- see assemble_relay_pool's own docstring. --no-relay-
    # pool is this ticket's own "disable it if no local relay hardware is
    # expected on a host" escape hatch (the ticket's own Files-to-modify
    # note); every other host still gets it by default, matching
    # peering's own "always started, never opt-in" precedent above.
    relay_pool: RelayPool | None = None
    if not args.no_relay_pool:
        relay_pool = assemble_relay_pool(
            store=store,
            locks=daemon.locks,
            lock=shared_lock,
        )

    # Sprint 004 ticket 007: the robot-console-compatibility /names HTTP
    # listener, sharing relay_pool's own DEFAULT_NAMES_API_PORT (the port
    # relay_pool already advertises in its TXT registry= key, whether or
    # not relay_pool itself is running on this host) and the same
    # shared_lock every other component here uses. Unlike relay_pool,
    # this listener has no local-hardware dependency to opt out of -- it
    # only ever touches store -- so it is always started, matching
    # peering's own "always started, never opt-in" precedent rather than
    # relay_pool's own --no-relay-pool escape hatch.
    names_api = assemble_names_api(
        store=store,
        lock=shared_lock,
        name_set_callback=peering.publish_name_set,
        name_clear_callback=peering.publish_name_clear,
    )

    api.start()
    remote_api.start()
    peering.start()
    if relay_pool is not None:
        relay_pool.start()
    names_api.start()

    # --peer HOST[:PORT] (repeatable): explicit peers on top of whatever
    # mDNS finds on its own. This host's own resolved
    # peer_pub_port/peer_snapshot_port are assumed to also be that peer's
    # ports -- a bare "--peer HOST" only works out of the box when the
    # fleet is uniformly configured (every host left at the same
    # defaults, or every host given the same --peer-pub-port/
    # --peer-snapshot-port); a peer running non-matching ports needs mDNS
    # (which learns its real ports from its own TXT record) rather than
    # --peer. Flagged in the ticket's Implementation Notes as a documented
    # limitation, not solved here -- sprint.md leaves the exact mechanism
    # an implementer's choice ("defaults, or query the peer's remote
    # API/TXT-equivalent over TCP").
    for host, port in peer_specs:
        peering.connect_peer(host, host, peer_pub_port, peer_snapshot_port, remote_port=port)

    peer_note = f", {len(peer_specs)} explicit peer(s)" if peer_specs else ""
    pool_note = (
        f", relay pool on {relay_pool.bound_port}" if relay_pool is not None else ""
    )
    names_api_note = f", names API on {names_api.bound_port}"
    print(
        f"mbregistry: listening on {socket_path} (local api), "
        f"{remote_api.bound_port} (remote api), store at {db_path}, "
        f"peering active (pub {peer_pub_port}, snapshot {peer_snapshot_port}"
        f"{peer_note}){pool_note}{names_api_note}",
        file=sys.stderr,
    )
    try:
        daemon.run(interval_s=args.interval, stop=stop_event.is_set)
    finally:
        # Stop order matters (this ticket's own acceptance criterion):
        # peering, remote_api, the relay pool, and the names API can each
        # still touch store/locks up until they're stopped, so all four
        # must be stopped -- and every thread they own joined -- before
        # api.stop() and, last of all, store.close(). api.stop() already
        # joins its own handler threads (unchanged from before this
        # ticket); peering.stop()/remote_api.stop()/relay_pool.stop()/
        # names_api.stop() do the same for their own threads (see each
        # class's own "stoppable, every thread joined" docstring note).
        names_api.stop()
        if relay_pool is not None:
            relay_pool.stop()
        peering.stop()
        remote_api.stop()
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


# ---------------------------------------------------------------------------
# install-service -- write the non-root-USB-access udev rule (ticket 008)
# ---------------------------------------------------------------------------

#: Three match rules, one per device node ``mbserial``/pyOCD open for the
#: micro:bit DAPLink interface (sprint.md SUC-005):
#:
#: 1. The CDC-ACM ``tty`` device node -- what ``mbserial`` and pyOCD's own
#:    serial transport use (matches the legacy
#:    ``microbit-radio-relay/server/packaging/99-microbit-relay.rules``
#:    shape: ``SUBSYSTEM=="tty", SUBSYSTEMS=="usb"``).
#: 2. The raw ``usb`` device node -- what pyOCD's CMSIS-DAP v2 (WinUSB)
#:    transport opens *directly*, bypassing the tty layer entirely (pyOCD's
#:    own udev guidance ships a ``SUBSYSTEM=="usb"`` rule for exactly this
#:    reason -- the tty-only legacy rule above does not cover it).
#: 3. Any ``hidraw*`` device node -- what pyOCD's CMSIS-DAP v1 (HID)
#:    transport opens instead, on DAPLink firmware that negotiates HID
#:    rather than WinUSB (also called out in pyOCD's own udev guidance).
#:
#: Covering all three means the same rule works regardless of which
#: transport a given board's DAPLink firmware happens to negotiate, without
#: this module needing to know that ahead of time.
#:
#: ``GROUP="plugdev"``/``MODE="0660"`` *and* ``TAG+="uaccess"`` are both
#: written into every rule (not one or the other) -- see
#: :func:`render_udev_rule`'s own docstring, "Decision: GROUP *and*
#: TAG+=uaccess, not either alone", for why.
_UDEV_RULE_TEMPLATE = """\
# mbregistry -- non-root access to the micro:bit DAPLink interface
# (VID:PID {vendor_id}:{product_id}). Written by `mbregistry install-service`
# (ticket 008) -- re-running it overwrites this file with identical
# content, so re-running install-service is idempotent. See
# mbtools.registry.cli.render_udev_rule()'s docstring for why each rule
# below grants access via both the {group} group and uaccess rather than
# just one of the two.

# CDC-ACM tty device node (mbserial, and pyOCD's DAPLink serial transport)
SUBSYSTEM=="tty", SUBSYSTEMS=="usb", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"

# Raw USB device node (CMSIS-DAP v2 / WinUSB transport, opened directly by pyOCD)
SUBSYSTEM=="usb", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"

# hidraw device node (CMSIS-DAP v1 / HID transport)
KERNEL=="hidraw*", ATTRS{{idVendor}}=="{vendor_id}", ATTRS{{idProduct}}=="{product_id}", GROUP="{group}", MODE="0660", TAG+="uaccess"
"""


def render_udev_rule() -> str:
    """The udev rule text :func:`cmd_install_service` writes to
    :data:`DEFAULT_UDEV_RULE_PATH` (or ``--udev-output``) to grant the
    operating (non-root) user access to the micro:bit DAPLink interface
    (VID:PID ``0d28:0204``) -- sprint.md SUC-005/Decision 9, closing out
    the issue this ticket completes
    (``non-root-usb-access-and-pyocd-permission-hang.md``). See
    :data:`_UDEV_RULE_TEMPLATE`'s own comment for what each of the three
    match rules (tty/usb/hidraw) covers and why all three are needed.

    **Decision: grant access via both ``GROUP="plugdev"`` and
    ``TAG+="uaccess"``, not just one of the two** (the ticket's Description
    frames this as a choice between the two -- this implementation makes
    both, deliberately, rather than picking one):

    - ``TAG+="uaccess"`` is systemd-logind's ACL mechanism: it grants the
      user of the *active session on a seat* access the moment the rule is
      (re-)applied, with no group-membership or relogin step at all. It's
      the cleaner fit *when it works* -- but it is scoped to a seat's
      active session, and the four Nolanet nodes this ticket targets
      (CLAUDE.md's hardware test hosts table) are reached **only over
      SSH**, with no local console/graphical seat for logind to track.  An
      SSH session is not reliably assigned a seat, so relying on
      ``uaccess`` alone risks silently granting nothing at all on exactly
      the hosts this ticket exists for.
    - ``GROUP="plugdev"``/``MODE="0660"`` has no seat/session dependency:
      any process the operating user starts, in any session (including
      SSH) opened *after* the group membership below takes effect, gets
      access -- deterministic and reliable on a headless host. The one
      cost is that a *newly added* group membership needs a fresh login
      session to take effect in that session (standard Linux behavior for
      any group-based grant, not specific to this rule) -- documented in
      :func:`cmd_install_service`'s own printed follow-up instructions.

    Writing both into the same rule costs nothing (they're independent
    grant mechanisms; a udev rule can assign both) and means access works
    immediately wherever ``uaccess`` happens to apply (e.g. a future
    graphical/console login), while still working reliably on the
    SSH-only hosts this ticket was written for once the printed
    ``usermod``/relogin step is done.
    """
    return _UDEV_RULE_TEMPLATE.format(
        vendor_id=_USB_VENDOR_ID, product_id=_USB_PRODUCT_ID, group=_UDEV_GROUP
    )


def cmd_install_service(args: argparse.Namespace) -> int:
    """``mbregistry install-service`` -- write the systemd unit file and
    the non-root-USB-access udev rule (ticket 008), then print (not run)
    the ``systemctl``/``udevadm``/``usermod`` commands the operator needs.
    Printing rather than running them is this ticket's implementer
    choice (its Description explicitly allows either, matching the
    pre-existing systemd-unit precedent below) -- it keeps this command
    safe to exercise without root or a real systemd/udev, matching
    sprint.md's Test Strategy ("installing it into a real systemd is out
    of scope for automated tests").

    **``sys.platform == "win32"`` (sprint 005 ticket 005)**: dispatches
    to :func:`~mbtools.registry.service_windows.cmd_install_service_windows`
    instead -- the ``sc.exe create``/``sc.exe failure`` commands (no
    udev/systemd at all on Windows), with ``exec_path`` explicitly
    including ``--windows-service`` so the rendered ``binPath=`` invokes
    exactly the SCM-aware entry point :func:`cmd_run`'s own
    ``--windows-service`` branch provides (see that function's own
    docstring, "A plain console app is not a real service") -- never the
    bare ``mbregistry run`` :func:`~mbtools.registry.service_windows
    .render_windows_service_install`'s own *default* still renders for
    ticket 004's own (unchanged) golden-file tests. The systemd-unit/
    udev-rule code below this branch is never reached on Windows.

    Both files are written unconditionally on every call, each to its own
    fixed default path (:data:`DEFAULT_UNIT_PATH`/
    :data:`DEFAULT_UDEV_RULE_PATH`, each independently overridable via
    ``--output``/``--udev-output``) with deterministic content -- so
    re-running this command on a host that already has both installed
    simply rewrites each file with the same bytes (this ticket's own
    "idempotent... no duplicate rule file, no service disruption"
    acceptance criterion): neither write depends on, or touches, whether
    ``mbregistry.service`` is currently running, and this function never
    itself starts, stops, or restarts it.
    """
    if sys.platform == "win32":
        exec_path = f"{sys.executable} -m mbtools.registry.cli run --windows-service"
        return cmd_install_service_windows(exec_path)

    unit_path = Path(args.output) if args.output else DEFAULT_UNIT_PATH
    unit_text = render_systemd_unit()

    try:
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_path.write_text(unit_text)
    except OSError as exc:
        print(f"mbregistry: could not write {unit_path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"mbregistry: wrote {unit_path}", file=sys.stderr)

    udev_path = Path(args.udev_output) if args.udev_output else DEFAULT_UDEV_RULE_PATH
    udev_text = render_udev_rule()

    try:
        udev_path.parent.mkdir(parents=True, exist_ok=True)
        udev_path.write_text(udev_text)
    except OSError as exc:
        print(f"mbregistry: could not write {udev_path}: {exc}", file=sys.stderr)
        return EXIT_ERROR

    print(f"mbregistry: wrote {udev_path}", file=sys.stderr)

    operating_user = _resolve_operating_user(args.user)

    print("Run as root to enable and start the service:", file=sys.stderr)
    print("  systemctl daemon-reload", file=sys.stderr)
    print("  systemctl enable --now mbregistry.service", file=sys.stderr)
    print(
        "Run as root to activate the udev rule for an already-attached "
        "device (a fresh plug-in picks it up automatically):",
        file=sys.stderr,
    )
    print("  udevadm control --reload-rules", file=sys.stderr)
    print("  udevadm trigger", file=sys.stderr)
    print(
        f"Run as root to grant {operating_user!r} non-root USB access "
        "(new login session -- e.g. a new SSH connection -- needed for "
        "this to take effect):",
        file=sys.stderr,
    )
    print(f"  usermod -aG {_UDEV_GROUP} {operating_user}", file=sys.stderr)
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
    run_p.add_argument(
        "--peer",
        action="append",
        metavar="HOST[:PORT]",
        help=(
            "connect directly to a peer registry's remote API, in addition to "
            f"mDNS discovery (repeatable); PORT defaults to {DEFAULT_REMOTE_PORT}"
        ),
    )
    run_p.add_argument(
        "--remote-port",
        type=int,
        help=(
            f"remote (TCP) api port (default {DEFAULT_REMOTE_PORT}, "
            f"or ${_REMOTE_PORT_ENV_VAR})"
        ),
    )
    run_p.add_argument(
        "--peer-pub-port",
        type=int,
        help=(
            f"peering ZeroMQ PUB port (default {DEFAULT_PUB_PORT}, "
            f"or ${_PEER_PUB_PORT_ENV_VAR})"
        ),
    )
    run_p.add_argument(
        "--peer-snapshot-port",
        type=int,
        help=(
            f"peering ZeroMQ snapshot REQ/REP port (default {DEFAULT_SNAPSHOT_PORT}, "
            f"or ${_PEER_SNAPSHOT_PORT_ENV_VAR})"
        ),
    )
    run_p.add_argument(
        "--auth-token",
        help=(
            "shared secret required of remote-api/peering connections "
            f"(default unset -- no auth, or ${_TOKEN_ENV_VAR})"
        ),
    )
    run_p.add_argument(
        "--windows-service",
        action="store_true",
        help=(
            "run under the Windows Service Control Manager "
            "(StartServiceCtrlDispatcher) -- set by the sc.exe create "
            "binPath= mbregistry install-service renders on Windows; "
            "not for interactive use"
        ),
    )
    run_p.add_argument(
        "--no-relay-pool",
        action="store_true",
        help=(
            "disable the robot-console-compatibility relay pool "
            "(_mbrelay._tcp) -- set this on a host with no local relay "
            "hardware"
        ),
    )
    run_p.set_defaults(func=cmd_run)

    install_p = sub.add_parser(
        "install-service",
        help="write the mbregistry systemd unit and non-root-USB-access udev rule",
    )
    install_p.add_argument(
        "--output",
        help=f"where to write the unit file (default {DEFAULT_UNIT_PATH})",
    )
    install_p.add_argument(
        "--udev-output",
        help=(
            "where to write the udev rule (default "
            f"{DEFAULT_UDEV_RULE_PATH})"
        ),
    )
    install_p.add_argument(
        "--user",
        help=(
            "operating user named in the printed usermod command "
            "(default $SUDO_USER, then $USER, then the current user)"
        ),
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
