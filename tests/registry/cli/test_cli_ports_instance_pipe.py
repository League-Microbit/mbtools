"""Tests for sprint 007 ticket 004's own ``registry.cli`` additions:
configurable console-compat ports (``--pool-port``/``--names-port``),
per-instance mDNS/peer identity (``--instance``), and the Windows
named-pipe transport name override (``--pipe``) -- the same
flag/env/default-resolution pattern ``--remote-port`` (``test_cli_run_
peering.py``) and ``--only-uid``/``--exclude-uid`` (``test_cli_claims.
py``) already establish.

Per ``test_cli_claims.py``'s own module docstring, ``_run_registry``
itself has no seam to inject a fake ``zeroconf`` and so is never called
directly by any test in this suite. Real, actually-bound port
advertisement (the ``--pool-port 0``/``--names-port 0`` ephemeral case)
and per-instance mDNS naming (``--instance``) are instead proven here at
the ``assemble_registry``/``assemble_relay_pool``/``assemble_names_api``
level -- the exact functions ``_run_registry`` calls with the CLI's own
resolved values, mirroring ``test_cli_run_peering.py``'s own precedent
of testing ``assemble_registry`` directly with a fake ``zeroconf``.
"""

from __future__ import annotations

import shutil
import socket
import tempfile
import threading

import pytest

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry.api_windows import WindowsPipeAPIServer
from mbtools.registry.cli import (
    DEFAULT_NAMES_API_PORT,
    DEFAULT_POOL_PORT,
    _INSTANCE_ENV_VAR,
    _NAMES_PORT_ENV_VAR,
    _POOL_PORT_ENV_VAR,
    _resolve_int,
    _resolve_token,
    assemble_daemon_and_api,
    assemble_names_api,
    assemble_registry,
    assemble_relay_pool,
    build_parser,
)
from mbtools.registry.console_compat.relay_pool import SERVICE_TYPE as POOL_SERVICE_TYPE
from mbtools.registry.console_compat.relay_pool import TXT_REGISTRY_PORT
from mbtools.registry.locks import LockManager
from mbtools.registry.peering import SERVICE_TYPE as PEERING_SERVICE_TYPE
from mbtools.registry.peering import TXT_REMOTE_PORT
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeUSBSource

VID, PID_ = DAPLINK_VID_PID


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


# ---------------------------------------------------------------------------
# fake zeroconf -- mirrors test_cli_run_peering.py's own fake of the same
# shape, duplicated here per this project's "one fixture set per test
# module" convention.
# ---------------------------------------------------------------------------


class _FakeServiceInfo:
    def __init__(self, type_, name, *, addresses, port, properties, server):
        self.type_ = type_
        self.name = name
        self.addresses = addresses
        self.port = port
        self.properties = properties
        self.server = server


class _FakeZeroconf:
    def __init__(self):
        self.registered: list = []

    def register_service(self, info, allow_name_change=False):
        self.registered.append(info)

    def unregister_service(self, info):
        pass

    def close(self):
        pass


class _FakeServiceBrowser:
    def __init__(self, zc, type_, listener=None):
        pass

    def cancel(self):
        pass


class _FakeZeroconfNamespace:
    ServiceInfo = staticmethod(_FakeServiceInfo)
    ServiceBrowser = staticmethod(_FakeServiceBrowser)

    def __init__(self):
        self.instance = _FakeZeroconf()

    def Zeroconf(self):
        return self.instance


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-ports-instance-pipe-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


# ---------------------------------------------------------------------------
# build_parser: --pool-port/--names-port/--instance/--pipe parse and
# default to None (mirrors test_cli_run_peering.py's own
# test_build_parser_run_accepts_all_new_flags/..._default_to_none pair).
# ---------------------------------------------------------------------------


def test_build_parser_run_accepts_pool_names_instance_pipe_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            "--pool-port",
            "7500",
            "--names-port",
            "7501",
            "--instance",
            "custom-instance",
            "--pipe",
            r"\\.\pipe\custom",
        ]
    )
    assert args.pool_port == 7500
    assert args.names_port == 7501
    assert args.instance == "custom-instance"
    assert args.pipe == r"\\.\pipe\custom"


def test_build_parser_run_pool_names_instance_pipe_default_to_none():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.pool_port is None
    assert args.names_port is None
    assert args.instance is None
    assert args.pipe is None


# ---------------------------------------------------------------------------
# flag > env var > default precedence -- _resolve_int/_resolve_token
# themselves are already covered generically by test_cli_run_peering.py;
# these exercise this ticket's own three new env var constants
# specifically (AC3: "verify against _resolve_int's actual precedence
# rule rather than assuming").
# ---------------------------------------------------------------------------


def test_pool_port_flag_beats_env_beats_default(monkeypatch):
    monkeypatch.setenv(_POOL_PORT_ENV_VAR, "9999")
    assert _resolve_int(1234, _POOL_PORT_ENV_VAR, DEFAULT_POOL_PORT) == 1234


def test_pool_port_env_beats_default(monkeypatch):
    monkeypatch.setenv(_POOL_PORT_ENV_VAR, "9999")
    assert _resolve_int(None, _POOL_PORT_ENV_VAR, DEFAULT_POOL_PORT) == 9999


def test_pool_port_default_when_neither_given(monkeypatch):
    monkeypatch.delenv(_POOL_PORT_ENV_VAR, raising=False)
    assert _resolve_int(None, _POOL_PORT_ENV_VAR, DEFAULT_POOL_PORT) == DEFAULT_POOL_PORT


def test_names_port_flag_beats_env_beats_default(monkeypatch):
    monkeypatch.setenv(_NAMES_PORT_ENV_VAR, "9999")
    assert _resolve_int(1234, _NAMES_PORT_ENV_VAR, DEFAULT_NAMES_API_PORT) == 1234


def test_names_port_env_beats_default(monkeypatch):
    monkeypatch.setenv(_NAMES_PORT_ENV_VAR, "9999")
    assert _resolve_int(None, _NAMES_PORT_ENV_VAR, DEFAULT_NAMES_API_PORT) == 9999


def test_names_port_default_when_neither_given(monkeypatch):
    monkeypatch.delenv(_NAMES_PORT_ENV_VAR, raising=False)
    assert (
        _resolve_int(None, _NAMES_PORT_ENV_VAR, DEFAULT_NAMES_API_PORT)
        == DEFAULT_NAMES_API_PORT
    )


def test_instance_flag_beats_env(monkeypatch):
    monkeypatch.setenv(_INSTANCE_ENV_VAR, "from-env")
    assert _resolve_token("from-flag", _INSTANCE_ENV_VAR) == "from-flag"


def test_instance_env_when_no_flag(monkeypatch):
    monkeypatch.setenv(_INSTANCE_ENV_VAR, "from-env")
    assert _resolve_token(None, _INSTANCE_ENV_VAR) == "from-env"


def test_instance_none_when_neither_given(monkeypatch):
    monkeypatch.delenv(_INSTANCE_ENV_VAR, raising=False)
    assert _resolve_token(None, _INSTANCE_ENV_VAR) is None


# ---------------------------------------------------------------------------
# --pool-port/--names-port: ephemeral (0) advertises the real bound port,
# an explicit non-zero value is bound and advertised as given (AC1/AC2).
# ---------------------------------------------------------------------------


def test_assemble_names_api_ephemeral_port_binds_and_is_not_zero(store):
    names_api = assemble_names_api(store=store, lock=threading.RLock(), port=0)
    names_api.start()
    try:
        assert names_api.bound_port != 0
    finally:
        names_api.stop()


def test_assemble_names_api_explicit_port_bound_as_given(store):
    names_api = assemble_names_api(store=store, lock=threading.RLock(), port=17902)
    names_api.start()
    try:
        assert names_api.bound_port == 17902
    finally:
        names_api.stop()


def test_assemble_relay_pool_explicit_port_bound_as_given(store, locks):
    pool = assemble_relay_pool(
        store=store,
        locks=locks,
        lock=threading.RLock(),
        host="127.0.0.1",
        port=17901,
        zeroconf=_FakeZeroconfNamespace(),
    )
    pool.start()
    try:
        assert pool.bound_port == 17901
    finally:
        pool.stop()


def test_ephemeral_names_port_is_advertised_by_relay_pool_not_zero(store, locks):
    """Reproduces ``_run_registry``'s own ordering (names_api constructed
    and started first, its real ``bound_port`` then threaded into
    ``assemble_relay_pool``'s own ``names_api_port``): with
    ``--names-port 0``, the pool's own mDNS TXT ``registry=`` key must
    carry the real bound port, never the literal ``"0"`` that was
    configured (this ticket's own acceptance criterion)."""
    names_api = assemble_names_api(store=store, lock=threading.RLock(), port=0)
    names_api.start()
    try:
        assert names_api.bound_port != 0

        ns = _FakeZeroconfNamespace()
        pool = assemble_relay_pool(
            store=store,
            locks=locks,
            lock=threading.RLock(),
            host="127.0.0.1",
            port=0,
            names_api_port=names_api.bound_port,
            zeroconf=ns,
        )
        pool.start()
        try:
            info = ns.instance.registered[0]
            assert info.port == pool.bound_port
            assert info.port != 0
            assert (
                info.properties[TXT_REGISTRY_PORT.encode()]
                == str(names_api.bound_port).encode()
            )
            assert info.properties[TXT_REGISTRY_PORT.encode()] != b"0"
        finally:
            pool.stop()
    finally:
        names_api.stop()


# ---------------------------------------------------------------------------
# Sprint 007 ticket 007 hardware finding: ``--remote-port 0`` (ephemeral)
# used to leave PeerDiscovery's own ``_mbregistry._tcp`` advertisement
# stuck at the literal requested "0" forever, because nothing read back
# RemoteAPIServer's real ``bound_port`` before peering.start() built its
# ServiceInfo/TXT record -- caught running two hand-started
# ``mbregistry run --remote-port 0`` instances on real hardware
# (docs/acceptance/007-hardware.md), not previously exercised by any
# test (every existing ``remote_port=0`` test here/in
# test_cli_run_peering.py only ever asserted on
# ``remote_api.bound_port``, never on what PeerDiscovery itself
# advertised). Mirrors ``test_ephemeral_names_port_is_advertised_by_
# relay_pool_not_zero``'s own "assert the real bound port made it into
# the advertisement, not the configured one" shape.
# ---------------------------------------------------------------------------


def test_ephemeral_remote_port_is_advertised_by_peering_not_zero(tmp_path, socket_dir):
    uid = _uid("ephremot")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17910,
        peer_snapshot_port=17911,
        zeroconf=_FakeZeroconfNamespace(),
    )
    try:
        api.start()
        remote_api.start()
        assert remote_api.bound_port != 0

        # The fix: cmd_run calls this between remote_api.start() and
        # peering.start(). Without it, peering._own_info.port/TXT
        # remote_port stay at the literal 0 this object was constructed
        # with.
        peering.set_remote_port(remote_api.bound_port)
        peering.start()

        assert peering._own_info.port == remote_api.bound_port
        assert peering._own_info.port != 0
        assert (
            peering._own_info.properties[TXT_REMOTE_PORT.encode()]
            == str(remote_api.bound_port).encode()
        )
        assert peering._own_info.properties[TXT_REMOTE_PORT.encode()] != b"0"
    finally:
        peering.stop()
        remote_api.stop()
        api.stop()
        store.close()


# ---------------------------------------------------------------------------
# --instance: reaches PeerDiscovery's/RelayPool's own ServiceInfo
# name/server fields (AC4); omitting it preserves today's short-hostname
# default exactly (AC5).
# ---------------------------------------------------------------------------


def test_assemble_registry_instance_reaches_peering_service_info(tmp_path, socket_dir):
    uid = _uid("instpeer")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17903,
        peer_snapshot_port=17904,
        peering_host="custom-instance",
        zeroconf=_FakeZeroconfNamespace(),
    )
    try:
        api.start()
        remote_api.start()
        peering.start()

        assert peering._own_info.name == f"custom-instance.{PEERING_SERVICE_TYPE}"
        assert peering._own_info.server == "custom-instance.local."
    finally:
        peering.stop()
        remote_api.stop()
        api.stop()
        store.close()


def test_assemble_registry_omitted_instance_preserves_short_hostname_default(
    tmp_path, socket_dir
):
    uid = _uid("instdflt")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])
    expected_host = socket.gethostname().split(".")[0]

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=17905,
        peer_snapshot_port=17906,
        zeroconf=_FakeZeroconfNamespace(),
    )
    try:
        api.start()
        remote_api.start()
        peering.start()

        assert peering._own_info.name == f"{expected_host}.{PEERING_SERVICE_TYPE}"
        assert peering._own_info.server == f"{expected_host}.local."
    finally:
        peering.stop()
        remote_api.stop()
        api.stop()
        store.close()


def test_assemble_relay_pool_instance_reaches_service_info(store, locks):
    ns = _FakeZeroconfNamespace()
    pool = assemble_relay_pool(
        store=store,
        locks=locks,
        lock=threading.RLock(),
        host="127.0.0.1",
        port=0,
        instance_host="custom-instance",
        zeroconf=ns,
    )
    pool.start()
    try:
        info = ns.instance.registered[0]
        assert info.name == f"custom-instance.{POOL_SERVICE_TYPE}"
        assert info.server == "custom-instance.local."
    finally:
        pool.stop()


def test_assemble_relay_pool_omitted_instance_preserves_short_hostname_default(
    store, locks
):
    ns = _FakeZeroconfNamespace()
    expected_host = socket.gethostname().split(".")[0]
    pool = assemble_relay_pool(
        store=store,
        locks=locks,
        lock=threading.RLock(),
        host="127.0.0.1",
        port=0,
        zeroconf=ns,
    )
    pool.start()
    try:
        info = ns.instance.registered[0]
        assert info.name == f"{expected_host}.{POOL_SERVICE_TYPE}"
        assert info.server == f"{expected_host}.local."
    finally:
        pool.stop()


# ---------------------------------------------------------------------------
# --pipe: assemble_daemon_and_api's own pipe_name override, and its
# fallback when omitted (AC6/AC7) -- api_windows's own pipe-name
# *resolution* (WindowsPipeAPIServer.__init__'s pipe_name-or-
# DEFAULT_PIPE_NAME logic) is already covered by
# tests/registry/api_windows/test_api_windows.py
# (test_pipe_name_overridable/test_pipe_name_defaults_when_omitted); this
# is ticket 004's own new plumbing -- cli.py's assembly threading a
# distinct override through, independent of socket_path.
# ---------------------------------------------------------------------------


def test_assemble_daemon_and_api_pipe_name_override_on_simulated_win32(
    tmp_path, monkeypatch
):
    from mbtools.registry import cli as cli_module

    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(_uid("pipeover"))]])
    try:
        daemon, api = assemble_daemon_and_api(
            store=store,
            usbwatch=usbwatch,
            socket_path=r"\\.\pipe\mbregistry-socket-value",
            pipe_name=r"\\.\pipe\mbregistry-pipe-override",
        )
        assert isinstance(api, WindowsPipeAPIServer)
        # --pipe wins over whatever --socket resolved to.
        assert api.pipe_name == r"\\.\pipe\mbregistry-pipe-override"
    finally:
        store.close()


def test_assemble_daemon_and_api_omitted_pipe_name_falls_back_to_socket_path(
    tmp_path, monkeypatch
):
    from mbtools.registry import cli as cli_module

    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(_uid("pipedflt"))]])
    try:
        daemon, api = assemble_daemon_and_api(
            store=store,
            usbwatch=usbwatch,
            socket_path=r"\\.\pipe\mbregistry-socket-value",
        )
        assert isinstance(api, WindowsPipeAPIServer)
        assert api.pipe_name == r"\\.\pipe\mbregistry-socket-value"
    finally:
        store.close()


def test_assemble_daemon_and_api_omitted_pipe_preserves_default_pipe_name(
    tmp_path, monkeypatch
):
    """Production shape: ``--pipe``/``--socket`` both omitted --
    ``_resolve_local_api_address`` resolves ``socket_path`` to
    :data:`~mbtools.registry.paths.default_pipe_name` on Windows, and
    with no ``pipe_name`` override this is exactly what
    :class:`WindowsPipeAPIServer` ends up bound to -- AC7, "Omitting
    --pipe preserves DEFAULT_PIPE_NAME's current fixed value."
    """
    from mbtools.registry import cli as cli_module
    from mbtools.registry.paths import default_pipe_name

    monkeypatch.setattr(cli_module.sys, "platform", "win32")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(_uid("pipedef2"))]])
    try:
        daemon, api = assemble_daemon_and_api(
            store=store,
            usbwatch=usbwatch,
            socket_path=default_pipe_name(),
        )
        assert isinstance(api, WindowsPipeAPIServer)
        assert api.pipe_name == default_pipe_name()
    finally:
        store.close()
