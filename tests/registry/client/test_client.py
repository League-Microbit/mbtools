"""Tests for mbtools.registry.client -- the typed local Unix-socket
client library (ticket 001).

Per sprint.md's Test Strategy ("protocol/dispatch tests run against a
real AF_UNIX socket in tmp_path ... reusing sprint 001's
RegistryAPIServer as the server side"): every test here drives a real
``RegistryAPIServer`` over a real ``AF_UNIX`` socket, exactly like
``tests/registry/api/test_api.py``'s own tests do for the server side of
the same protocol -- this module exercises the same wire protocol from
the client's typed side.
"""

from __future__ import annotations

import itertools
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from mbtools.common import (
    EXIT_ERROR,
    EXIT_LOCKED,
    EXIT_NO_DEVICE,
    EXIT_USAGE,
)
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.client import (
    DeviceLockedError,
    DeviceNotFoundError,
    InvalidRequestError,
    RegistryClient,
    RegistryClientError,
    RegistryUnavailable,
)
from mbtools.registry.flash import FlashOp
from mbtools.registry.identity import ProbeResult
from mbtools.registry.locks import KIND_FLASH, KIND_SERIAL, LockManager
from mbtools.registry.store import Store

VID_PID = "0d28:0204"


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


UID = _uid("aaaa1111")
UID2 = _uid("bbbb2222")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_dir():
    """A short-path temp dir for the AF_UNIX socket file -- same reason
    as test_api.py's own fixture of the same name: pytest's tmp_path
    regularly exceeds sizeof(sockaddr_un.sun_path).
    """
    d = tempfile.mkdtemp(prefix="mbregistry-client-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    s.upsert_attached(UID, "/dev/ttyACM0", VID_PID)
    s.apply_probe_result(
        UID,
        ProbeResult(
            role="NEZHA2", common_name="robot", device_name="vevov", serial="1", raw="raw"
        ),
    )
    s.upsert_attached(UID2, "/dev/ttyACM1", VID_PID)
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def server(socket_dir, store, locks):
    """A real ``RegistryAPIServer`` with a ``peer_pid_fn`` that hands out
    a fresh, sequential fake pid per accepted connection -- so each
    :class:`RegistryClient` instance in a test (each opens its own
    connection) looks like a distinct process, exactly the scenario
    ``lock``'s contention/holder behavior needs to be tested against.
    """
    pid_counter = itertools.count(4001)
    pid_lock = threading.Lock()

    def peer_pid_fn(_sock: socket.socket) -> int:
        with pid_lock:
            return next(pid_counter)

    flash_op = FlashOp(locks=locks, store=store)
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        peer_pid_fn=peer_pid_fn,
        sweep_interval_s=100.0,
    )
    srv.start()
    yield srv
    srv.stop()


# ---------------------------------------------------------------------------
# list / find
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_list_returns_plain_dicts_for_every_device(server):
    with RegistryClient(server.socket_path) as client:
        devices = client.list()

    assert {d["uid"] for d in devices} == {UID, UID2}
    by_uid = {d["uid"]: d for d in devices}
    assert by_uid[UID]["device_name"] == "vevov"
    assert by_uid[UID]["lock_kind"] is None


@pytest.mark.requires_af_unix
def test_find_returns_the_matching_device(server):
    with RegistryClient(server.socket_path) as client:
        device = client.find(UID)

    assert device["uid"] == UID
    assert device["device_name"] == "vevov"


@pytest.mark.requires_af_unix
def test_find_unknown_uid_raises_device_not_found(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(DeviceNotFoundError) as excinfo:
            client.find("does-not-exist")

    assert excinfo.value.exit_code == EXIT_NO_DEVICE
    assert excinfo.value.code == "not_found"


# ---------------------------------------------------------------------------
# lock / unlock
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_lock_then_unlock_round_trip(server, locks):
    with RegistryClient(server.socket_path) as client:
        client.lock(UID, KIND_SERIAL)
        assert locks.status(UID).kind == KIND_SERIAL

        released = client.unlock(UID)

    assert released is True
    assert locks.status(UID) is None


@pytest.mark.requires_af_unix
def test_lock_unknown_uid_raises_device_not_found(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(DeviceNotFoundError):
            client.lock("does-not-exist", KIND_SERIAL)


@pytest.mark.requires_af_unix
def test_lock_unknown_kind_raises_invalid_request(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(InvalidRequestError) as excinfo:
            client.lock(UID, "nonsense")

    assert excinfo.value.exit_code == EXIT_USAGE
    assert excinfo.value.code == "invalid_request"


@pytest.mark.requires_af_unix
def test_lock_already_held_raises_device_locked_with_holder_preserved(server, locks):
    holder = RegistryClient(server.socket_path)
    holder.connect()
    holder.lock(UID, KIND_FLASH)
    holder_pid = locks.status(UID).pid

    contender = RegistryClient(server.socket_path)
    contender.connect()
    try:
        with pytest.raises(DeviceLockedError) as excinfo:
            contender.lock(UID, KIND_SERIAL)
    finally:
        contender.close()
        holder.unlock(UID)
        holder.close()

    assert excinfo.value.exit_code == EXIT_LOCKED
    assert excinfo.value.code == "locked"
    assert excinfo.value.holder["kind"] == KIND_FLASH
    assert excinfo.value.holder["pid"] == holder_pid
    assert excinfo.value.holder["label"] is None
    assert isinstance(excinfo.value.holder["since"], float)


@pytest.mark.requires_af_unix
def test_unlock_unknown_uid_raises_device_not_found(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(DeviceNotFoundError):
            client.unlock("does-not-exist")


@pytest.mark.requires_af_unix
def test_unlock_when_not_held_by_this_connection_returns_false(server):
    with RegistryClient(server.socket_path) as client:
        released = client.unlock(UID)

    assert released is False


# ---------------------------------------------------------------------------
# mark_flashed
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_mark_flashed_without_flash_lock_raises_registry_client_error(server, store):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(RegistryClientError) as excinfo:
            client.mark_flashed(UID)

    assert excinfo.value.code == "not_locked"
    assert store.find(UID).flash_count == 0


@pytest.mark.requires_af_unix
def test_mark_flashed_increments_flash_count(server, store):
    with RegistryClient(server.socket_path) as client:
        client.lock(UID, KIND_FLASH)
        client.mark_flashed(UID)

    assert store.find(UID).flash_count == 1


@pytest.mark.requires_af_unix
def test_mark_flashed_unknown_uid_raises_device_not_found(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(DeviceNotFoundError):
            client.mark_flashed("does-not-exist")


# ---------------------------------------------------------------------------
# session model: a lock is released when the connection closes
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_closing_the_client_releases_locks_it_held(server, locks):
    client = RegistryClient(server.socket_path)
    client.connect()
    client.lock(UID, KIND_SERIAL)
    assert locks.status(UID) is not None

    client.close()

    # Server-side "lock release on connection close" (api.py) fires once
    # this connection's socket goes away -- a fresh connection must be
    # able to acquire the same uid immediately, with no explicit unlock().
    with RegistryClient(server.socket_path) as second:
        second.lock(UID, KIND_FLASH)
        assert locks.status(UID).kind == KIND_FLASH


@pytest.mark.requires_af_unix
def test_one_client_reuses_the_same_connection_across_calls(server):
    """A single RegistryClient sends several requests over one
    connection (the module's own "one connection is one client session"
    contract) rather than reconnecting per call.
    """
    client = RegistryClient(server.socket_path)
    client.connect()
    sock = client._sock  # noqa: SLF001 -- whitebox check that no reconnect happened
    assert sock is not None

    client.list()
    client.lock(UID, KIND_SERIAL)
    client.unlock(UID)

    assert client._sock is sock  # noqa: SLF001
    client.close()


# ---------------------------------------------------------------------------
# EXIT_* mapping -- table-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc_cls, code, exit_code",
    [
        (DeviceNotFoundError, "not_found", EXIT_NO_DEVICE),
        (InvalidRequestError, "invalid_request", EXIT_USAGE),
        (DeviceLockedError, "locked", EXIT_LOCKED),
        (RegistryClientError, "internal_error", EXIT_ERROR),
    ],
)
def test_exception_code_maps_to_stable_exit_code(exc_cls, code, exit_code):
    if exc_cls is DeviceLockedError:
        exc = exc_cls(code, "message", {"kind": "flash", "pid": 1})
    else:
        exc = exc_cls(code, "message")
    assert exc.code == code
    assert exc.exit_code == exit_code


@pytest.mark.requires_af_unix
def test_unmapped_code_raises_base_registry_client_error_with_default_exit_code(server):
    """An op-specific/unmapped code (e.g. ``not_locked``, which no
    list/find/lock/unlock response can produce) falls back to the base
    :class:`RegistryClientError` with ``EXIT_ERROR`` -- exercised here by
    forging a raw wire-protocol response through the private dispatch
    helper, since none of this ticket's four ops can trigger it over the
    real socket.
    """
    from mbtools.registry.client import _raise_for_error

    with pytest.raises(RegistryClientError) as excinfo:
        _raise_for_error({"ok": False, "code": "not_locked", "error": "nope"})

    assert type(excinfo.value) is RegistryClientError
    assert excinfo.value.exit_code == EXIT_ERROR


# ---------------------------------------------------------------------------
# RegistryUnavailable -- absent socket
# ---------------------------------------------------------------------------


def test_connect_to_absent_socket_raises_registry_unavailable(tmp_path):
    missing = tmp_path / "no-such-daemon.sock"
    client = RegistryClient(missing)

    with pytest.raises(RegistryUnavailable) as excinfo:
        client.connect()

    assert "registry unavailable" in str(excinfo.value)


@pytest.mark.requires_af_unix
def test_connect_to_stale_socket_file_raises_registry_unavailable(socket_dir):
    """A socket file that exists but has no listener behind it any more
    (daemon crashed without cleaning up, or never started) raises
    ``ConnectionRefusedError`` -- an ``OSError`` subclass, same catch as
    the absent-file case above, and equally a "no daemon to talk to"
    condition per the module docstring.
    """
    stale_path = f"{socket_dir}/stale.sock"
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(stale_path)
    bound.close()  # closed without listen()/unlink() -- file stays, nothing answers

    client = RegistryClient(stale_path)
    with pytest.raises(RegistryUnavailable):
        client.connect()


def test_list_against_absent_socket_raises_registry_unavailable_lazily(tmp_path):
    """A method call connects lazily if not already connected -- no
    explicit ``connect()`` call required before the first request.
    """
    missing = tmp_path / "no-such-daemon.sock"
    client = RegistryClient(missing)

    with pytest.raises(RegistryUnavailable):
        client.list()


def test_close_is_idempotent_and_safe_before_connect(tmp_path):
    client = RegistryClient(tmp_path / "never-connected.sock")
    client.close()
    client.close()


# ---------------------------------------------------------------------------
# socket path resolution: --socket flag beats $MBREGISTRY_SOCKET beats default
# ---------------------------------------------------------------------------


def test_resolve_socket_path_flag_wins(monkeypatch):
    from mbtools.registry.client import SOCKET_ENV_VAR, resolve_socket_path

    monkeypatch.setenv(SOCKET_ENV_VAR, "/env/api.sock")
    assert resolve_socket_path("/flag/api.sock") == Path("/flag/api.sock")


def test_resolve_socket_path_env_var_used_when_no_flag(monkeypatch):
    from mbtools.registry.client import SOCKET_ENV_VAR, resolve_socket_path

    monkeypatch.setenv(SOCKET_ENV_VAR, "/env/api.sock")
    assert resolve_socket_path(None) == Path("/env/api.sock")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "DEFAULT_SOCKET_PATH -- this call's own default `default=` "
        "parameter -- is bound once, at real import time, to None on "
        "real sys.platform == 'win32' (see that constant's own "
        "docstring in registry.api); a monkeypatch of sys.platform "
        "here cannot retroactively change an already-bound default "
        "parameter value, so on the windows-latest CI job this call "
        "genuinely raises TypeError from Path(None) -- documented, "
        "expected behavior (resolve_socket_path's own docstring: "
        "'ticket 005's Windows platform branch in cli.py is where a "
        "Windows-aware caller resolves the named pipe instead of "
        "calling this function at all' -- registry.client"
        ".resolve_local_api_address is that caller)"
    ),
)
def test_resolve_socket_path_default_when_neither_given(monkeypatch):
    from mbtools.registry.client import SOCKET_ENV_VAR, resolve_socket_path

    from mbtools.registry.api import DEFAULT_SOCKET_PATH

    monkeypatch.delenv(SOCKET_ENV_VAR, raising=False)
    assert resolve_socket_path(None) == DEFAULT_SOCKET_PATH


# ---------------------------------------------------------------------------
# resolve_local_api_address: flag > env var > platform default (ticket 006 --
# the shared helper every local-registry CLI now resolves its address
# through, moved here from registry.cli's own private
# _resolve_local_api_address so deploy.cli/serial.cli/relay.cli can use it
# too; see this function's own docstring for why calling
# resolve_socket_path directly, as those three did before this ticket, was
# broken on Windows). Mirrors tests/registry/cli/test_cli_run_windows.py's
# own _resolve_local_api_address coverage, monkeypatching this module's
# `sys.platform` instead of registry.cli's -- the same real `sys` module
# object either way, since `_resolve_local_api_address` in cli.py is now
# just this function under another name.
# ---------------------------------------------------------------------------


def test_resolve_local_api_address_defaults_to_the_pipe_name_on_simulated_win32(
    monkeypatch,
):
    import mbtools.registry.client as client

    monkeypatch.setattr(client.sys, "platform", "win32")
    monkeypatch.delenv("MBREGISTRY_SOCKET", raising=False)
    result = client.resolve_local_api_address(None, "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\mbregistry"
    assert isinstance(result, str)  # never routed through pathlib.Path


def test_resolve_local_api_address_flag_wins_on_simulated_win32(monkeypatch):
    import mbtools.registry.client as client

    monkeypatch.setattr(client.sys, "platform", "win32")
    result = client.resolve_local_api_address(r"\\.\pipe\custom", "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\custom"
    assert isinstance(result, str)


def test_resolve_local_api_address_env_var_wins_over_default_on_simulated_win32(
    monkeypatch,
):
    import mbtools.registry.client as client

    monkeypatch.setattr(client.sys, "platform", "win32")
    monkeypatch.setenv("MBREGISTRY_SOCKET", r"\\.\pipe\from-env")
    result = client.resolve_local_api_address(None, "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\from-env"


def test_resolve_local_api_address_unaffected_off_windows(tmp_path, monkeypatch):
    # Ticket 006: force the off-Windows branch explicitly -- the
    # sys.platform == "win32" check itself is live (evaluated on every
    # call, unlike DEFAULT_SOCKET_PATH's own import-time-frozen value --
    # see the skip reasons just above for that distinction), so on real
    # Windows CI this branch selection would otherwise flip and return a
    # bare str instead of a Path, failing this assertion.
    import mbtools.registry.client as client_module

    monkeypatch.setattr(client_module.sys, "platform", "linux")
    from mbtools.registry.client import resolve_local_api_address

    result = resolve_local_api_address(str(tmp_path / "api.sock"), "MBREGISTRY_SOCKET")
    assert result == tmp_path / "api.sock"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "this test's own subject is the off-Windows default path, which "
        "on real sys.platform == 'win32' is genuinely unreachable: the "
        "else branch passes DEFAULT_SOCKET_PATH -- bound once, at real "
        "import time, to None on win32 -- straight through to "
        "resolve_socket_path, which raises TypeError from Path(None); "
        "monkeypatching sys.platform here cannot retroactively change "
        "that already-bound value (see "
        "test_resolve_socket_path_default_when_neither_given's own "
        "skip reason just above for the same underlying issue)"
    ),
)
def test_resolve_local_api_address_off_windows_default_when_neither_given(monkeypatch):
    """Off Windows, this delegates to resolve_socket_path with
    DEFAULT_SOCKET_PATH -- a real, non-None default there (unlike
    win32, where DEFAULT_SOCKET_PATH is None) -- so no override is
    required and the production default path comes back unchanged.
    """
    from mbtools.registry.client import resolve_local_api_address

    monkeypatch.delenv("MBREGISTRY_SOCKET", raising=False)
    from mbtools.registry.paths import default_socket_path

    result = resolve_local_api_address(None, "MBREGISTRY_SOCKET")
    assert result == default_socket_path()


# ---------------------------------------------------------------------------
# names_get / names_set / names_clear / names_list (sprint 004, ticket 005)
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_names_get_returns_none_for_an_unregistered_name(server):
    with RegistryClient(server.socket_path) as client:
        assert client.names_get("tovez") is None


@pytest.mark.requires_af_unix
def test_names_set_then_get_round_trips(server):
    with RegistryClient(server.socket_path) as client:
        set_entry = client.names_set("tovez", 20, 30)
        assert set_entry["channel"] == 20
        assert set_entry["group"] == 30
        assert set_entry["source"] == "registry"

        got = client.names_get("tovez")
        assert got == set_entry


@pytest.mark.requires_af_unix
def test_names_clear_drops_the_row(server):
    with RegistryClient(server.socket_path) as client:
        client.names_set("tovez", 20, 30)
        client.names_clear("tovez")
        assert client.names_get("tovez") is None


@pytest.mark.requires_af_unix
def test_names_list_includes_every_row(server):
    with RegistryClient(server.socket_path) as client:
        client.names_set("tovez", 20, 30)
        client.names_set("vevov", 40, 50)
        entries = client.names_list()

    assert {e["name"] for e in entries} == {"tovez", "vevov"}


@pytest.mark.requires_af_unix
def test_names_set_of_a_malformed_name_raises_invalid_request(server):
    with RegistryClient(server.socket_path) as client:
        with pytest.raises(InvalidRequestError):
            client.names_set("not-a-name", 20, 30)
