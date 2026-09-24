"""Tests for sprint 005 ticket 005 -- wiring Windows platform support
into ``mbregistry run`` (``registry.cli.assemble_daemon_and_api``'s own
platform branch, the ``--windows-service`` flag/dispatch through
``service_windows.run_as_windows_service``, and
``_resolve_local_api_address``'s pipe-name-on-Windows precedence).

Per this ticket's own Testing note ("A single, real end-to-end test on
the current dev platform... proves the branch *selection* logic itself,
even though the Windows-side behavior it dispatches to can only be
exercised via fakes"): every test here either runs on this dev host's
own real platform (macOS/Linux -- the "unchanged on Linux/macOS"
regression guard) or monkeypatches ``cli.sys.platform`` to ``"win32"``
(mirroring ``tests/registry/api/test_api.py``'s own
``default_peer_pid`` precedent --
``monkeypatch.setattr(<module>.sys, "platform", ...)``) and asserts the
right *class*/function gets constructed/called. The Windows-side
behavior those classes/functions themselves provide (real ``ctypes``
calls) is proven with fakes in their own test modules
(``tests/registry/api_windows/``, ``tests/registry/service_windows/``),
not re-proven here -- so no test here ever calls ``.start()`` on a
``WindowsPipeAPIServer`` built against the real (unbound, off-Windows)
``_Win32PipeAPI``, and no test here lets ``run_as_windows_service`` run
for real (it's always monkeypatched to a recording fake, since the real
one requires actual Windows -- see ``service_windows.py``'s own
``_Win32ServiceAPI`` constructor guard).
"""

from __future__ import annotations

import threading

import pytest

from mbtools.common import DAPLINK_VID_PID, PortInfo
from mbtools.registry import cli
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.api_windows import WindowsPipeAPIServer
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeUSBSource

VID, PID_ = DAPLINK_VID_PID


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


# ---------------------------------------------------------------------------
# assemble_daemon_and_api: branch selection (AC1/AC2)
# ---------------------------------------------------------------------------


def test_assemble_daemon_and_api_uses_windows_pipe_server_on_simulated_win32(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(_uid("winb1111"))]])
    try:
        daemon, api = cli.assemble_daemon_and_api(
            store=store,
            usbwatch=usbwatch,
            socket_path=r"\\.\pipe\mbregistry-test",
        )
        assert isinstance(api, WindowsPipeAPIServer)
        assert not isinstance(api, RegistryAPIServer)
        assert api.pipe_name == r"\\.\pipe\mbregistry-test"
        # Every other component is assembled exactly as on Linux/macOS --
        # same Daemon, same shared RLock.
        assert daemon._lock is api._lock
    finally:
        store.close()


def test_assemble_daemon_and_api_still_uses_registry_api_server_off_windows(
    tmp_path, monkeypatch
):
    # Ticket 006 correction: this used to leave sys.platform alone,
    # relying on the dev host's own real platform (macOS/Linux) --
    # which broke the instant this suite actually ran on real Windows
    # (the windows-latest CI job), since sys.platform there genuinely
    # is "win32". This test's own subject is the *off-Windows* branch,
    # so it now forces a non-"win32" value explicitly -- deterministic
    # on every CI leg, still the same "byte-for-byte unchanged off
    # Windows" acceptance criterion (see
    # test_cli_run.py/test_cli_run_peering.py, unmodified by this
    # ticket and still green there, on the real off-Windows platform).
    monkeypatch.setattr(cli.sys, "platform", "linux")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(_uid("winb2222"))]])
    try:
        daemon, api = cli.assemble_daemon_and_api(
            store=store,
            usbwatch=usbwatch,
            socket_path=str(tmp_path / "api.sock"),
        )
        assert isinstance(api, RegistryAPIServer)
        assert daemon._lock is api._lock
    finally:
        store.close()


# ---------------------------------------------------------------------------
# _resolve_local_api_address: flag > env var > platform default
# ---------------------------------------------------------------------------


def test_resolve_local_api_address_defaults_to_the_pipe_name_on_simulated_win32(
    monkeypatch,
):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.delenv("MBREGISTRY_SOCKET", raising=False)
    result = cli._resolve_local_api_address(None, "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\mbregistry"
    assert isinstance(result, str)  # never routed through pathlib.Path


def test_resolve_local_api_address_flag_wins_on_simulated_win32(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    result = cli._resolve_local_api_address(r"\\.\pipe\custom", "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\custom"
    assert isinstance(result, str)


def test_resolve_local_api_address_env_var_wins_over_default_on_simulated_win32(
    monkeypatch,
):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setenv("MBREGISTRY_SOCKET", r"\\.\pipe\from-env")
    result = cli._resolve_local_api_address(None, "MBREGISTRY_SOCKET")
    assert result == r"\\.\pipe\from-env"


def test_resolve_local_api_address_unaffected_off_windows(tmp_path, monkeypatch):
    # Ticket 006: force the off-Windows branch explicitly -- see the
    # sibling fix above this test's own docstring-less twin,
    # test_assemble_daemon_and_api_still_uses_registry_api_server_off_windows,
    # for why relying on the ambient host platform broke on the
    # windows-latest CI job.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    result = cli._resolve_local_api_address(str(tmp_path / "api.sock"), "MBREGISTRY_SOCKET")
    assert result == tmp_path / "api.sock"


# ---------------------------------------------------------------------------
# --windows-service flag: parsing
# ---------------------------------------------------------------------------


def test_build_parser_run_windows_service_flag_defaults_false():
    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    assert args.windows_service is False


def test_build_parser_run_windows_service_flag_settable():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--windows-service"])
    assert args.windows_service is True


# ---------------------------------------------------------------------------
# cmd_run: dispatch (AC5 -- branch selection itself, not the Windows-side
# behavior each branch calls into)
# ---------------------------------------------------------------------------


def test_cmd_run_with_windows_service_flag_dispatches_through_run_as_windows_service(
    monkeypatch,
):
    calls = {}

    def fake_run_as_windows_service(main, *, service_name=None, win32=None):
        calls["main"] = main
        return 42

    monkeypatch.setattr(cli, "run_as_windows_service", fake_run_as_windows_service)

    parser = cli.build_parser()
    args = parser.parse_args(["run", "--windows-service"])
    code = cli.cmd_run(args)

    assert code == 42
    assert callable(calls["main"])


def test_cmd_run_without_windows_service_flag_never_calls_run_as_windows_service(
    monkeypatch,
):
    def _forbidden(*args, **kwargs):
        raise AssertionError("run_as_windows_service must not be called without --windows-service")

    monkeypatch.setattr(cli, "run_as_windows_service", _forbidden)

    seen = {}

    def fake_run_registry(args, stop_event):
        seen["stop_event"] = stop_event
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_run_registry", fake_run_registry)

    parser = cli.build_parser()
    args = parser.parse_args(["run"])
    code = cli.cmd_run(args)

    assert code == cli.EXIT_OK
    assert isinstance(seen["stop_event"], threading.Event)
    assert not seen["stop_event"].is_set()


def test_cmd_run_windows_service_branch_wraps_run_registry_as_main(monkeypatch):
    """The ``main`` handed to ``run_as_windows_service`` is
    ``_run_registry`` bound to this call's own ``args`` -- proven by
    substituting a fake ``_run_registry`` and confirming the ``main``
    callable ``run_as_windows_service`` receives, when invoked with a
    stop_event, reaches it.
    """
    recorded = {}

    def fake_run_registry(args, stop_event):
        recorded["args"] = args
        recorded["stop_event"] = stop_event
        return 7

    monkeypatch.setattr(cli, "_run_registry", fake_run_registry)

    def fake_run_as_windows_service(main, *, service_name=None, win32=None):
        stop_event = threading.Event()
        return main(stop_event)

    monkeypatch.setattr(cli, "run_as_windows_service", fake_run_as_windows_service)

    parser = cli.build_parser()
    args = parser.parse_args(["run", "--windows-service"])
    code = cli.cmd_run(args)

    assert code == 7
    assert recorded["args"] is args
    assert isinstance(recorded["stop_event"], threading.Event)
