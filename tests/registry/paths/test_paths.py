"""Tests for mbtools.registry.paths -- cross-platform state-location
resolution (sprint 005, ticket 002).

Per sprint.md's Design Rationale, Linux/macOS defaults must stay
byte-for-byte identical to what ``registry.store``/``registry.api``
already shipped before this ticket (no behavior change on already
-supported platforms); Windows defaults are new and are an ASSUMPTION,
not a stakeholder-confirmed decision (module docstring, brief's own open
decision #2). Every function is exercised on both platform branches by
monkeypatching ``mbtools.registry.paths.sys.platform``, mirroring
``tests/registry/api/test_api.py``'s own precedent for
``default_peer_pid``'s platform dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import mbtools.registry.paths as paths_module
from mbtools.registry.paths import (
    default_db_path,
    default_pipe_name,
    default_socket_path,
)


# ---------------------------------------------------------------------------
# default_db_path
# ---------------------------------------------------------------------------


def test_default_db_path_linux_unchanged(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "linux")
    assert default_db_path() == Path("/var/lib/mbregistry/devices.db")


def test_default_db_path_darwin_unchanged(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "darwin")
    assert default_db_path() == Path("/var/lib/mbregistry/devices.db")


def test_default_db_path_windows_rooted_at_programdata_env(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMDATA", r"D:\CustomProgramData")
    assert default_db_path() == Path(r"D:\CustomProgramData") / "mbregistry" / "devices.db"


def test_default_db_path_windows_falls_back_when_programdata_unset(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    monkeypatch.delenv("PROGRAMDATA", raising=False)
    assert default_db_path() == Path(r"C:\ProgramData") / "mbregistry" / "devices.db"


def test_default_db_path_matches_store_default_db_path():
    """registry.store.DEFAULT_DB_PATH must be sourced from this module and
    must be unchanged from its pre-ticket value on the platform running
    this test (per the ticket's no-behavior-change acceptance criterion).
    """
    from mbtools.registry.store import DEFAULT_DB_PATH

    assert DEFAULT_DB_PATH == default_db_path()
    if paths_module.sys.platform != "win32":
        assert DEFAULT_DB_PATH == Path("/var/lib/mbregistry/devices.db")


# ---------------------------------------------------------------------------
# default_socket_path
# ---------------------------------------------------------------------------


def test_default_socket_path_linux_unchanged(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "linux")
    assert default_socket_path() == Path("/run/mbregistry/api.sock")


def test_default_socket_path_darwin_unchanged(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "darwin")
    assert default_socket_path() == Path("/run/mbregistry/api.sock")


def test_default_socket_path_raises_on_windows(monkeypatch):
    monkeypatch.setattr(paths_module.sys, "platform", "win32")
    with pytest.raises(NotImplementedError):
        default_socket_path()


@pytest.mark.skipif(
    paths_module.sys.platform == "win32",
    reason=(
        "both sides of this comparison are meaningless on real Windows: "
        "default_socket_path() itself raises NotImplementedError there "
        "by design (its own docstring), and registry.api."
        "DEFAULT_SOCKET_PATH is bound once, at real import time, to "
        "None rather than calling default_socket_path() at all, "
        "specifically to avoid that raise (see DEFAULT_SOCKET_PATH's "
        "own docstring, 'ticket 003's import-safety fix')"
    ),
)
def test_default_socket_path_matches_api_default_socket_path():
    """registry.api.DEFAULT_SOCKET_PATH must be sourced from this module
    and must be unchanged from its pre-ticket value on the platform
    running this test.
    """
    from mbtools.registry.api import DEFAULT_SOCKET_PATH

    assert DEFAULT_SOCKET_PATH == default_socket_path()
    if paths_module.sys.platform != "win32":
        assert DEFAULT_SOCKET_PATH == Path("/run/mbregistry/api.sock")


# ---------------------------------------------------------------------------
# default_pipe_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_default_pipe_name_fixed_and_callable_on_any_platform(monkeypatch, platform):
    monkeypatch.setattr(paths_module.sys, "platform", platform)
    assert default_pipe_name() == r"\\.\pipe\mbregistry"


def test_default_pipe_name_returns_a_string():
    assert isinstance(default_pipe_name(), str)
