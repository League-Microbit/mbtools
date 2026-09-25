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
import json
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from mbtools.common import EXIT_ERROR, EXIT_LINUX_USER_PREFLIGHT, EXIT_NO_DAEMON, EXIT_OK
from mbtools.registry.api import DEFAULT_SOCKET_PATH, RegistryAPIServer
from mbtools.registry.api_windows import WindowsPipeAPIServer
from mbtools.registry.client import (
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.client import SOCKET_ENV_VAR as _SOCKET_ENV_VAR
from mbtools.registry.client import find_local_api_address, resolve_local_api_address
from mbtools.registry.console_compat.names_api import NamesAPI
from mbtools.registry.console_compat.relay_pool import (
    DEFAULT_NAMES_API_PORT,
    DEFAULT_POOL_PORT,
    RelayPool,
)
from mbtools.registry.claims import build_claim_fn
from mbtools.registry.daemon import DEFAULT_INTERVAL_S, Daemon
from mbtools.registry.flash import FlashOp
from mbtools.registry.peering import (
    DEFAULT_PUB_PORT,
    DEFAULT_SNAPSHOT_PORT,
    PeerDiscovery,
)
from mbtools.registry.remote_api import DEFAULT_REMOTE_PORT, RemoteAPIServer
from mbtools.registry.paths import LINUX_SYSTEM_UNIT_PATH, LINUX_UDEV_RULE_PATH
from mbtools.registry.render import render_json, render_table
from mbtools.registry.service import (
    DryRunCommandRunner,
    LinuxUserPreflightError,
    linux_install,
    linux_status,
    linux_uninstall,
    macos_install,
    macos_status,
    macos_uninstall,
)
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
    "cmd_service_install",
    "cmd_service_uninstall",
    "cmd_service_status",
    "assemble_daemon_and_api",
    "assemble_registry",
    "assemble_relay_pool",
    "assemble_names_api",
    "DEFAULT_UNIT_PATH",
    "DEFAULT_UDEV_RULE_PATH",
]

#: Standard systemd system-unit install location -- ticket 006-004 replaces
#: this module's own literal ``Path("/etc/systemd/system/mbregistry.service")``
#: with an alias onto :data:`~mbtools.registry.paths.LINUX_SYSTEM_UNIT_PATH`,
#: the single definition ``registry.service``'s own Linux orchestration
#: (:func:`linux_install`/:func:`linux_uninstall`/:func:`linux_status`)
#: already uses, so the two can never drift apart (this is what let
#: ``tests/registry/paths/test_paths.py``'s cross-check tests be deleted
#: rather than kept). Kept as a name here only for backward-compatible
#: imports (``from mbtools.registry.cli import DEFAULT_UNIT_PATH``) --
#: nothing in this module computes a path from it anymore, since the
#: deprecated :func:`cmd_install_service` now delegates entirely to
#: :func:`linux_install`/:func:`macos_install`.
DEFAULT_UNIT_PATH = LINUX_SYSTEM_UNIT_PATH

#: Standard ``udev`` rules.d install location -- same "alias, not a second
#: definition" treatment as :data:`DEFAULT_UNIT_PATH` above, onto
#: :data:`~mbtools.registry.paths.LINUX_UDEV_RULE_PATH`.
DEFAULT_UDEV_RULE_PATH = LINUX_UDEV_RULE_PATH

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


#: Ticket 006-004 consolidation: this module used to define its own
#: ``_resolve_operating_user`` (ticket 008's escape hatch for the old
#: ``install-service --user`` flag), byte-for-byte identical to
#: ``registry.service``'s own ``_resolve_linux_operating_user`` (tickets
#: 002/003 duplicated it there rather than import from here, since
#: sprint.md's Architecture fixes the dependency direction as
#: ``cli`` -> ``service``, never the reverse -- importing this module's
#: version from ``service.py`` would be a real circular import). Now that
#: :func:`cmd_install_service` delegates its whole write+run sequence to
#: :func:`linux_install` (which already resolves the operating user
#: itself via that function), this module has no caller left for its own
#: copy -- deleted here, per the ticket's "consolidate into one (in
#: service.py)" instruction, rather than kept as unused dead code.


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
    socket_path = find_local_api_address(args.socket, _SOCKET_ENV_VAR)

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
    claim_fn: Any = None,
    chip_identity_session_factory: Any = None,
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

    ``claim_fn`` (sprint 007, ticket 002) is forwarded verbatim to
    :class:`Daemon`'s own ``claim_fn`` -- see that class's module
    docstring's "Cross-instance claim" note. Left ``None`` here (this
    function's own default), a bare :func:`assemble_daemon_and_api` call
    gets :class:`Daemon`'s own filesystem-free no-op default -- real,
    cross-instance claim enforcement is wired only by
    :func:`_run_registry`, which always passes a real
    :func:`mbtools.registry.claims.build_claim_fn` result through
    :func:`assemble_registry` below. Every pre-ticket-002 caller/test that
    omits it is unaffected.

    ``chip_identity_session_factory`` (sprint 007, ticket 003) is
    forwarded verbatim to :class:`Daemon`'s own parameter of the same name
    -- see that class's own docstring. Left ``None`` here (this function's
    own default, same as every other test-only escape hatch above), a
    bare call gets :class:`Daemon`'s own default of "use pyOCD for real".
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
        claim_fn=claim_fn,
        chip_identity_session_factory=chip_identity_session_factory,
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
    claim_fn: Any = None,
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

    ``claim_fn`` (sprint 007, ticket 002) is forwarded verbatim to
    :func:`assemble_daemon_and_api` -- see that function's own ``claim_fn``
    docstring note. :func:`_run_registry` always passes a real
    :func:`mbtools.registry.claims.build_claim_fn` result here; every
    other/earlier caller/test that omits it gets :class:`Daemon`'s own
    no-op default, unaffected.

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
        claim_fn=claim_fn,
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
    # Sprint 007, ticket 002: always wire the real cross-instance claim
    # (registry.claims.try_claim, through build_claim_fn's --only-uid/
    # --exclude-uid filter) for a real `mbregistry run` -- unconditionally,
    # not only when --only-uid/--exclude-uid are given, since two
    # instances on one host must never both claim a board whether or not
    # an operator has partitioned uids explicitly (SUC-005).
    claim_fn = build_claim_fn(only_uids=args.only_uid, exclude_uids=args.exclude_uid)
    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=PollingPortWatcher(),
        socket_path=socket_path,
        remote_port=remote_port,
        peer_pub_port=peer_pub_port,
        peer_snapshot_port=peer_snapshot_port,
        auth_token=auth_token,
        lock=shared_lock,
        claim_fn=claim_fn,
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
# service install|uninstall|status -- ticket 006-004's own new command
# group, dispatching straight into registry.service's per-platform
# orchestration (macos_install/macos_uninstall/macos_status,
# linux_install/linux_uninstall/linux_status). Per sprint.md's Architecture
# and Design Rationale, this module owns argparse + user-facing text only;
# every actual write/run decision lives in registry.service.
#
# install-service (below, ticket 008/009's original command) is kept as a
# hidden, deprecated alias for exactly one release, per this ticket's own
# Migration guidance -- see cmd_install_service's own docstring.
# ---------------------------------------------------------------------------

#: Platforms ``service ...``/the deprecated ``install-service`` alias
#: actually dispatch orchestration for -- everything else (``win32``, and
#: any platform this package has never run on) is the Windows-guard's
#: "not supported" branch below. Named for both call sites (ticket 006-004
#: also uses it for the deprecated alias's generic platform dispatch) so
#: the two can never independently drift on which platforms are "real".
_SERVICE_PLATFORMS = ("darwin", "linux")


def _service_not_supported(subcommand: str) -> int:
    """The Windows guard every ``cmd_service_*`` function below takes
    first, per this ticket's own acceptance criterion: print
    ``"mbregistry: service <subcommand> is not supported on Windows"``
    (or the real platform name, for a platform that is neither Windows
    nor one of :data:`_SERVICE_PLATFORMS`) to stderr and return
    :data:`~mbtools.common.EXIT_ERROR` -- never importing or calling any
    of ``registry.service``'s platform-specific functions (they are
    already imported at module scope for ``darwin``/``linux`` callers,
    but this function itself never calls one).
    """
    platform_name = "Windows" if sys.platform == "win32" else sys.platform
    print(
        f"mbregistry: service {subcommand} is not supported on {platform_name}",
        file=sys.stderr,
    )
    return EXIT_ERROR


def _service_scope(args: argparse.Namespace) -> str:
    """``"user"``/``"system"`` from ``install``/``uninstall``'s own
    ``--user``/``--system`` mutually-exclusive, required flags -- argparse
    itself guarantees exactly one of the two booleans is ``True`` (this
    function is never called for ``status``, which takes neither flag).
    """
    return "user" if args.user else "system"


def cmd_service_install(args: argparse.Namespace) -> int:
    """``mbregistry service install (--user | --system) [--dry-run]`` --
    dispatch on ``sys.platform`` to :func:`macos_install`/
    :func:`linux_install`, forwarding ``--dry-run`` verbatim (both
    functions already implement "render/write, or print what would be
    written, and run commands through the same runner seam told to print
    instead of execute" -- see ``registry.service``'s own Design
    Rationale). This function does nothing itself beyond parsing,
    dispatch, and printing the one-line outcome; every decision about
    *what* gets written or run lives in ``registry.service``.

    Catches :class:`LinuxUserPreflightError` (only ever raised by
    :func:`linux_install`'s ``scope="user"`` path) and turns it into
    :data:`~mbtools.common.EXIT_LINUX_USER_PREFLIGHT` -- a distinct
    nonzero exit, per this ticket's own instruction, rather than letting
    it propagate as an uncaught exception/traceback. The remediation text
    itself was already printed to stderr by ``linux_install`` before it
    raised (see that exception's own docstring), so this function does
    not print anything additional for that case.
    """
    if sys.platform not in _SERVICE_PLATFORMS:
        return _service_not_supported("install")

    scope = _service_scope(args)
    install_fn = macos_install if sys.platform == "darwin" else linux_install
    try:
        path = install_fn(scope, dry_run=args.dry_run)
    except LinuxUserPreflightError:
        return EXIT_LINUX_USER_PREFLIGHT

    verb = "would install" if args.dry_run else "installed"
    print(f"mbregistry: {verb} the {scope} service ({path}).", file=sys.stderr)
    return EXIT_OK


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    """``mbregistry service uninstall (--user | --system) [--purge]`` --
    dispatch on ``sys.platform`` to :func:`macos_uninstall`/
    :func:`linux_uninstall` and print the outcome message either one
    returns. Always exits :data:`~mbtools.common.EXIT_OK`, even when
    nothing was installed at the requested scope -- both orchestration
    functions are themselves a safe no-op in that case (spec's own
    "uninstall ... is a safe no-op (exit 0 ...) when nothing is
    installed"), so there is no separate error branch here to exit
    nonzero from.
    """
    if sys.platform not in _SERVICE_PLATFORMS:
        return _service_not_supported("uninstall")

    scope = _service_scope(args)
    uninstall_fn = macos_uninstall if sys.platform == "darwin" else linux_uninstall
    message = uninstall_fn(scope, purge=args.purge, dry_run=args.dry_run)
    print(message, file=sys.stderr)
    return EXIT_OK


def cmd_service_status(args: argparse.Namespace) -> int:
    """``mbregistry service status`` -- report *both* scopes' installed/
    running state and resolved paths (SUC-004), on whichever platform
    this host is (:func:`macos_status`/:func:`linux_status`). Takes no
    flags -- unlike ``install``/``uninstall``, there is no scope choice
    to make; a status check always looks at both.

    Plain ``print`` lines (per this ticket's own Description -- this
    output shape is not specified elsewhere, and ``render.py``'s table
    conventions are shaped for a list of *devices*, not a two-row
    scope/state/path report), one line per scope:
    ``<scope> <installed/running state> <path>``.
    """
    if sys.platform not in _SERVICE_PLATFORMS:
        return _service_not_supported("status")

    status_fn = macos_status if sys.platform == "darwin" else linux_status
    for scope in ("user", "system"):
        status = status_fn(scope)
        print(f"{scope:<6} {status.describe():<24} {status.path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# install-service -- deprecated, hidden alias for `service install --system`
# ---------------------------------------------------------------------------


def cmd_install_service(args: argparse.Namespace) -> int:
    """``mbregistry install-service`` -- deprecated (sprint 006, ticket
    006-004) hidden alias for ``mbregistry service install --system``,
    kept for exactly one release per the linked issue's own Migration
    guidance, so a script that still invokes it does not suddenly start
    and enable a real service it never asked to run.

    **``sys.platform == "win32"`` is untouched** (sprint 005 ticket 005's
    own branch, unchanged by this ticket per the stakeholder's explicit
    "leave Windows code alone"): dispatches to
    :func:`~mbtools.registry.service_windows.cmd_install_service_windows`
    exactly as before, never printing the deprecation notice below or
    reaching ``registry.service`` at all.

    **On macOS/Linux**, prints the one-line deprecation notice, then
    reconciles two statements from this ticket's own spec that are in
    tension for a literal ``dry_run=True`` call: "matches ``service
    install --system --dry-run``" (Description item 2) vs. "still writes
    the systemd unit/udev rule files ... unchanged observable behavior"
    (this ticket's own Acceptance Criteria) -- :func:`linux_install`/
    :func:`macos_install`'s ``dry_run`` bool, per their own docstrings,
    only gates whether the plist/unit file is written; it is independent
    of the ``runner`` a caller injects. Passing ``dry_run=False`` (so the
    file *is* written, matching the old command's observable behavior)
    together with an explicit :class:`DryRunCommandRunner` as ``runner``
    (so every ``launchctl``/``systemctl``/``udevadm``/``usermod`` call
    only ever prints ``"would run: ..."`` instead of executing) satisfies
    both: files written, commands printed, nothing actually loaded or
    started -- exactly the old command's "write files, print commands,
    start nothing" contract, still using ``service install --system``'s
    own code path rather than a second, parallel implementation.

    ``--user`` (a *string* override here -- the operating user named in
    the printed ``usermod`` line -- unrelated to ``service install``'s
    own boolean ``--user``/``--system`` scope flags, a different
    subparser's namespace) is forwarded to :func:`linux_install` as
    ``operating_user``; ``macos_install`` has no such parameter (macOS
    has no ``plugdev``/``usermod`` equivalent) so it is not passed there.
    """
    if sys.platform == "win32":
        exec_path = f"{sys.executable} -m mbtools.registry.cli run --windows-service"
        return cmd_install_service_windows(exec_path)

    print(
        "mbregistry: install-service is deprecated, use 'mbregistry "
        "service install --system' -- this will be removed in a future "
        "release.",
        file=sys.stderr,
    )

    runner = DryRunCommandRunner()
    if sys.platform == "darwin":
        path = macos_install("system", dry_run=False, runner=runner)
    else:
        path = linux_install(
            "system", dry_run=False, runner=runner, operating_user=args.user
        )

    print(f"mbregistry: wrote {path}", file=sys.stderr)
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
    run_p.add_argument(
        "--only-uid",
        action="append",
        metavar="UID",
        help=(
            "only ever claim/probe this uid (repeatable); default: no "
            "restriction -- claim any uid this instance can win"
        ),
    )
    run_p.add_argument(
        "--exclude-uid",
        action="append",
        metavar="UID",
        help="never claim/probe this uid (repeatable)",
    )
    run_p.set_defaults(func=cmd_run)

    service_p = sub.add_parser(
        "service",
        help=(
            "install, uninstall, or report the mbregistry service "
            "(macOS/Linux only)"
        ),
    )
    service_sub = service_p.add_subparsers(dest="service_command", required=True)

    service_install_p = service_sub.add_parser(
        "install", help="write the service file(s) and load/start the service"
    )
    service_install_scope = service_install_p.add_mutually_exclusive_group(
        required=True
    )
    service_install_scope.add_argument(
        "--user",
        action="store_true",
        help="install for the current user (launchd LaunchAgent / systemd --user)",
    )
    service_install_scope.add_argument(
        "--system",
        action="store_true",
        help=(
            "install machine-wide, needs root/sudo (launchd LaunchDaemon / "
            "systemd system unit)"
        ),
    )
    service_install_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be written and run, touching nothing",
    )
    service_install_p.set_defaults(func=cmd_service_install)

    service_uninstall_p = service_sub.add_parser(
        "uninstall", help="stop and remove the service"
    )
    service_uninstall_scope = service_uninstall_p.add_mutually_exclusive_group(
        required=True
    )
    service_uninstall_scope.add_argument(
        "--user", action="store_true", help="uninstall the current user's service"
    )
    service_uninstall_scope.add_argument(
        "--system",
        action="store_true",
        help="uninstall the machine-wide service, needs root/sudo",
    )
    service_uninstall_p.add_argument(
        "--purge",
        action="store_true",
        help="also remove devices.db (kept by default)",
    )
    service_uninstall_p.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would be stopped and removed, touching nothing",
    )
    service_uninstall_p.set_defaults(func=cmd_service_uninstall)

    service_status_p = service_sub.add_parser(
        "status", help="report both scopes' installed/running state and paths"
    )
    service_status_p.set_defaults(func=cmd_service_status)

    #: Hidden, deprecated alias for `service install --system` (sprint 006,
    #: ticket 006-004) -- no ``help=`` kwarg at all, which is what keeps
    #: argparse from listing it in the "positional arguments" subcommand
    #: descriptions below `--help`'s usage line (it still necessarily
    #: appears in that usage line's own `{list,run,service,install-
    #: service}` choice brace -- argparse has no clean way to hide a
    #: subparser from that specifically). Kept for exactly one release,
    #: per cmd_install_service's own docstring.
    install_p = sub.add_parser("install-service")
    install_p.add_argument(
        "--user",
        help=(
            "operating user named in the printed usermod command "
            "(default $SUDO_USER, then $USER, then the current user); "
            "Linux only"
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
