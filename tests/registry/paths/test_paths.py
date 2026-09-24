"""Tests for mbtools.registry.paths -- where mbregistry keeps its database
and local API socket, per platform and per privilege level, and how
clients find a running daemon.

Every case is exercised by monkeypatching ``sys.platform`` and
``os.geteuid`` on the module, so the whole matrix runs on any host.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mbtools.registry.paths as paths_module
from mbtools.registry.paths import (
    client_socket_candidates,
    default_db_path,
    default_pipe_name,
    default_socket_path,
    find_client_socket,
)


@pytest.fixture
def as_platform(monkeypatch, tmp_path):
    """Set platform + root-ness, with HOME and XDG vars pointed at tmp."""

    def _set(platform: str, root: bool, *, runtime_dir: bool = False) -> Path:
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        monkeypatch.setattr(paths_module.sys, "platform", platform)
        monkeypatch.setattr(paths_module.os, "geteuid", lambda: 0 if root else 1000, raising=False)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        if runtime_dir:
            runtime = tmp_path / "run-user"
            runtime.mkdir(exist_ok=True)
            monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        else:
            monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        return home

    return _set


# -- database -----------------------------------------------------------------


def test_db_linux_root(as_platform):
    as_platform("linux", root=True)
    assert default_db_path() == Path("/var/lib/mbregistry/devices.db")


def test_db_linux_user_defaults_to_local_state(as_platform):
    home = as_platform("linux", root=False)
    assert default_db_path() == home / ".local" / "state" / "mbregistry" / "devices.db"


def test_db_linux_user_honours_xdg_state_home(as_platform, monkeypatch, tmp_path):
    as_platform("linux", root=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_db_path() == tmp_path / "state" / "mbregistry" / "devices.db"


def test_db_macos_root(as_platform):
    as_platform("darwin", root=True)
    assert default_db_path() == Path("/Library/Application Support/mbregistry/devices.db")


def test_db_macos_user(as_platform):
    home = as_platform("darwin", root=False)
    assert default_db_path() == (
        home / "Library" / "Application Support" / "mbregistry" / "devices.db"
    )


def test_db_windows_rooted_at_programdata_env(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"D:\CustomProgramData")
    assert default_db_path() == Path(r"D:\CustomProgramData") / "mbregistry" / "devices.db"


def test_db_windows_falls_back_when_programdata_unset(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    assert default_db_path() == Path(r"C:\ProgramData") / "mbregistry" / "devices.db"


def test_store_default_db_path_comes_from_this_module():
    from mbtools.registry.store import DEFAULT_DB_PATH

    assert DEFAULT_DB_PATH == default_db_path()


# -- socket -------------------------------------------------------------------


def test_socket_linux_root(as_platform):
    as_platform("linux", root=True)
    assert default_socket_path() == Path("/run/mbregistry/api.sock")


def test_socket_linux_user_uses_xdg_runtime_dir(as_platform, tmp_path):
    as_platform("linux", root=False, runtime_dir=True)
    assert default_socket_path() == tmp_path / "run-user" / "mbregistry" / "api.sock"


def test_socket_linux_user_falls_back_to_cache(as_platform):
    home = as_platform("linux", root=False)
    assert default_socket_path() == home / ".cache" / "mbregistry" / "api.sock"


def test_socket_macos_root_uses_var_run(as_platform):
    as_platform("darwin", root=True)
    assert default_socket_path() == Path("/var/run/mbregistry/api.sock")


def test_socket_macos_user(as_platform):
    home = as_platform("darwin", root=False)
    assert default_socket_path() == (
        home / "Library" / "Application Support" / "mbregistry" / "api.sock"
    )


def test_macos_user_socket_fits_sun_path():
    """AF_UNIX paths are capped at 104 bytes on macOS."""
    long_home = "/Users/" + "x" * 32
    path = Path(long_home) / "Library" / "Application Support" / "mbregistry" / "api.sock"
    assert len(str(path).encode()) < 104


def test_socket_raises_on_windows(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    with pytest.raises(NotImplementedError):
        default_socket_path()


@pytest.mark.skipif(
    paths_module.sys.platform == "win32",
    reason="registry.api.DEFAULT_SOCKET_PATH is None on Windows (named pipe instead)",
)
def test_api_default_socket_path_comes_from_this_module():
    from mbtools.registry.api import DEFAULT_SOCKET_PATH

    assert DEFAULT_SOCKET_PATH == default_socket_path()


# -- client discovery ---------------------------------------------------------


def test_user_client_looks_at_own_daemon_then_system(as_platform):
    home = as_platform("linux", root=False)
    assert client_socket_candidates() == [
        home / ".cache" / "mbregistry" / "api.sock",
        Path("/run/mbregistry/api.sock"),
    ]


def test_root_client_looks_at_system_daemon_first(as_platform):
    home = as_platform("darwin", root=True)
    assert client_socket_candidates() == [
        Path("/var/run/mbregistry/api.sock"),
        home / "Library" / "Application Support" / "mbregistry" / "api.sock",
    ]


def test_find_client_socket_picks_the_first_that_exists(as_platform, monkeypatch, tmp_path):
    as_platform("linux", root=False)
    user_sock = tmp_path / "user.sock"
    system_sock = tmp_path / "system.sock"
    monkeypatch.setattr(paths_module, "user_socket_path", lambda: user_sock)
    monkeypatch.setattr(paths_module, "system_socket_path", lambda: system_sock)

    system_sock.touch()
    assert find_client_socket() == system_sock  # only the root daemon is up

    user_sock.touch()
    assert find_client_socket() == user_sock  # own daemon wins when present


def test_find_client_socket_names_first_candidate_when_none_exist(as_platform):
    home = as_platform("linux", root=False)
    assert find_client_socket() == home / ".cache" / "mbregistry" / "api.sock"


# -- named pipe ---------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_default_pipe_name_fixed_and_callable_on_any_platform(monkeypatch, platform):
    monkeypatch.setattr(paths_module.sys, "platform", platform)
    assert default_pipe_name() == r"\\.\pipe\mbregistry"
