"""Tests for sprint 007 ticket 005's own ``registry.cli`` additions:
``--ready-json``, ``--exit-with-parent``, and ``--no-peering``, per
``docs/design/robot-console-integration.md`` Sec.4 item 4's spawn recipe.

Two layers, per ticket 005's own Testing plan:

1. ``--no-peering``'s "``PeerDiscovery`` never constructed" proof and
   ``assemble_names_api``'s None-callback safety are exercised directly
   at the :func:`~mbtools.registry.cli.assemble_registry`/
   :func:`~mbtools.registry.cli.assemble_names_api` level -- the same
   precedent ``test_cli_run_peering.py``/``test_cli_ports_instance_pipe.py``
   already establish for testing this module's assembly functions rather
   than ``_run_registry`` itself (per ``test_cli_claims.py``'s own
   docstring note: ``_run_registry`` has no seam to inject a fake
   ``zeroconf``, so exercising it for real would mean real mDNS
   registration).
2. ``--ready-json``'s output shape, ``--exit-with-parent``'s stdin-EOF
   watcher, and all three flags composed together (the spawn recipe
   itself) are exercised against a *fully mocked* ``assemble_registry``/
   ``assemble_relay_pool``/``assemble_names_api`` (this ticket's own
   Testing plan, "no real sockets/ports") -- the one new test seam this
   ticket adds to make that possible is ``_run_registry``'s own
   keyword-only ``stdin`` parameter, used only by the
   ``--exit-with-parent`` tests below.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import Mock

import pytest

from mbtools.common import DAPLINK_VID_PID, EXIT_OK, PortInfo
from mbtools.registry import cli as cli_module
from mbtools.registry.cli import (
    DEFAULT_PUB_PORT,
    DEFAULT_SNAPSHOT_PORT,
    assemble_names_api,
    assemble_registry,
    build_parser,
)
from mbtools.registry.store import Store
from mbtools.testing.fakes import FakeUSBSource

VID, PID_ = DAPLINK_VID_PID


def _uid(tag: str) -> str:
    unique = (tag * 4)[:16]
    return "9900" + "0000" + "11112222" + unique + "77778888" + "6e052820"


def _port_info(uid: str, port: str = "/dev/ttyACM0") -> PortInfo:
    return PortInfo(uid=uid, port=port, vid=VID, pid=PID_)


def _wait_until(predicate, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition never became true within timeout"


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbregistry-cli-spawn-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# argparse: --ready-json/--exit-with-parent/--no-peering parse and default
# to False, mirroring test_cli_run_peering.py's own accepts/defaults pair.
# ---------------------------------------------------------------------------


def test_build_parser_run_accepts_spawn_flags():
    parser = build_parser()
    args = parser.parse_args(["run", "--ready-json", "--exit-with-parent", "--no-peering"])
    assert args.ready_json is True
    assert args.exit_with_parent is True
    assert args.no_peering is True


def test_build_parser_run_spawn_flags_default_to_false():
    parser = build_parser()
    args = parser.parse_args(["run"])
    assert args.ready_json is False
    assert args.exit_with_parent is False
    assert args.no_peering is False


# ---------------------------------------------------------------------------
# --no-peering at the assemble_registry level: PeerDiscovery's own
# constructor is never called (not merely a no-op start()/stop()), and
# the fourth return value is None.
# ---------------------------------------------------------------------------


@pytest.mark.requires_af_unix
def test_assemble_registry_no_peering_never_constructs_peer_discovery(
    monkeypatch, tmp_path, socket_dir
):
    peer_discovery_ctor = Mock(name="PeerDiscovery")
    monkeypatch.setattr(cli_module, "PeerDiscovery", peer_discovery_ctor)

    uid = _uid("nopeer01")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        no_peering=True,
    )
    try:
        peer_discovery_ctor.assert_not_called()
        assert peering is None

        api.start()
        remote_api.start()
        assert remote_api.bound_port != 0
    finally:
        remote_api.stop()
        api.stop()
        store.close()


@pytest.mark.requires_af_unix
def test_assemble_registry_no_peering_false_still_constructs_peer_discovery(
    monkeypatch, tmp_path, socket_dir
):
    """Regression guard: the default (``no_peering=False``) is unchanged
    -- every pre-ticket-005 caller/test still gets a real
    ``PeerDiscovery``."""
    uid = _uid("haspeer1")
    store = Store(tmp_path / "devices.db")
    usbwatch = FakeUSBSource([[_port_info(uid)]])

    daemon, api, remote_api, peering = assemble_registry(
        store=store,
        usbwatch=usbwatch,
        socket_path=f"{socket_dir}/api.sock",
        remote_port=0,
        peer_pub_port=18102,
        peer_snapshot_port=18103,
    )
    try:
        assert peering is not None
    finally:
        store.close()


# ---------------------------------------------------------------------------
# assemble_names_api: None callbacks (what --no-peering forces) never
# raise from any of the three op handlers.
# ---------------------------------------------------------------------------


def test_assemble_names_api_none_callbacks_do_not_raise(tmp_path):
    import threading as _threading

    store = Store(tmp_path / "devices.db")
    try:
        names_api = assemble_names_api(
            store=store,
            lock=_threading.RLock(),
            name_set_callback=None,
            name_clear_callback=None,
        )
        status, _payload = names_api.handle_put(
            "tovez", json.dumps({"channel": 20, "group": 4}).encode()
        )
        assert status == 200

        status, _payload = names_api.handle_get("tovez")
        assert status == 200

        status, _payload = names_api.handle_delete("tovez")
        assert status == 200
    finally:
        store.close()


# ---------------------------------------------------------------------------
# --exit-with-parent's watcher, in isolation: EOF on stdin sets
# stop_event, the exact Event SIGTERM/SIGINT already set.
# ---------------------------------------------------------------------------


def test_watch_stdin_for_parent_exit_sets_stop_event_on_eof():
    stop_event = threading.Event()
    read_fd, write_fd = os.pipe()
    read_file = os.fdopen(read_fd, "r")
    thread = threading.Thread(
        target=cli_module._watch_stdin_for_parent_exit,
        args=(read_file, stop_event),
    )
    thread.start()
    try:
        # Nothing written yet -- the watcher must still be blocked
        # reading, not already-set.
        assert not stop_event.wait(timeout=0.2)

        os.close(write_fd)  # EOF on the read end

        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert stop_event.is_set()
    finally:
        read_file.close()


def test_watch_stdin_for_parent_exit_sets_stop_event_on_closed_stdin_error():
    """A ``ValueError``/``OSError`` reading a closed-out-from-under-it
    stdin is treated the same as EOF -- stop_event still gets set,
    rather than the thread dying silently."""

    class _RaisingStdin:
        def readline(self):
            raise ValueError("I/O operation on closed file")

    stop_event = threading.Event()
    cli_module._watch_stdin_for_parent_exit(_RaisingStdin(), stop_event)
    assert stop_event.is_set()


# ---------------------------------------------------------------------------
# _run_registry against a fully mocked assemble_registry/
# assemble_relay_pool/assemble_names_api -- this ticket's own Testing
# plan ("no real sockets/ports"). Fakes mimic just enough surface
# (bound_port, start()/stop(), .locks, .run()) for _run_registry's own
# flag-handling/print logic to run for real.
# ---------------------------------------------------------------------------


class _FakeListener:
    def __init__(self, bound_port: int) -> None:
        self.bound_port = bound_port
        self.start_calls = 0
        self.stop_calls = 0

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


class _FakePeering(_FakeListener):
    def __init__(self, bound_port: int = 0) -> None:
        super().__init__(bound_port)
        self.connect_peer_calls: list[tuple] = []
        # Sprint 007 ticket 007 hardware finding: cmd_run now calls this
        # between remote_api.start() and peering.start() (see cli.py's
        # own comment at that call site) -- recorded, not just accepted,
        # so a test could assert on it if it ever needed to.
        self.set_remote_port_calls: list[int] = []

    def publish_name_set(self, *args, **kwargs) -> None:
        pass

    def publish_name_clear(self, *args, **kwargs) -> None:
        pass

    def connect_peer(self, *args, **kwargs) -> None:
        self.connect_peer_calls.append((args, kwargs))

    def set_remote_port(self, port: int) -> None:
        self.set_remote_port_calls.append(port)


class _FakeDaemon:
    """Mimics ``Daemon.run``'s own ``while not stop(): ...`` shape
    (``daemon.py``'s real ``run()``) just enough to prove
    ``--exit-with-parent`` drives shutdown through the same ``stop_event``
    -- without any real USB/store/lock machinery. ``loop_on_stop=False``
    (the default) instead returns immediately, for tests that only care
    about the print/flag-forwarding logic and would otherwise block
    forever on a ``stop`` that nothing ever sets.
    """

    def __init__(self, loop_on_stop: bool = False) -> None:
        self.locks = object()
        self._loop_on_stop = loop_on_stop
        self.run_calls: list[float] = []

    def run(self, *, interval_s: float, stop) -> None:
        self.run_calls.append(interval_s)
        if self._loop_on_stop:
            while not stop():
                time.sleep(0.01)


def _install_fake_assembly(
    monkeypatch,
    *,
    remote_port: int = 17999,
    pool_port: int = 17998,
    names_port: int = 17997,
    loop_daemon: bool = False,
):
    """Replace ``assemble_registry``/``assemble_relay_pool``/
    ``assemble_names_api`` inside ``cli.py`` with the fakes above, and
    return (captured_kwargs, fake_daemon) so a test can both inspect what
    ``_run_registry`` passed each assembly function and, for
    ``loop_daemon=True``, observe/drive the fake poll loop.
    """
    captured: dict = {}
    fake_daemon = _FakeDaemon(loop_on_stop=loop_daemon)
    fake_api = _FakeListener(bound_port=0)
    fake_remote_api = _FakeListener(bound_port=remote_port)

    def fake_assemble_registry(**kwargs):
        captured["assemble_registry_kwargs"] = kwargs
        peering = None if kwargs.get("no_peering") else _FakePeering()
        return fake_daemon, fake_api, fake_remote_api, peering

    def fake_assemble_relay_pool(**kwargs):
        captured["assemble_relay_pool_kwargs"] = kwargs
        return _FakeListener(bound_port=pool_port)

    def fake_assemble_names_api(**kwargs):
        captured["assemble_names_api_kwargs"] = kwargs
        return _FakeListener(bound_port=names_port)

    monkeypatch.setattr(cli_module, "assemble_registry", fake_assemble_registry)
    monkeypatch.setattr(cli_module, "assemble_relay_pool", fake_assemble_relay_pool)
    monkeypatch.setattr(cli_module, "assemble_names_api", fake_assemble_names_api)
    return captured, fake_daemon


def _run_args(tmp_path, extra: list[str] | None = None) -> list[str]:
    return [
        "run",
        "--socket",
        str(tmp_path / "api.sock"),
        "--db",
        str(tmp_path / "devices.db"),
        *(extra or []),
    ]


# -- --ready-json shape --------------------------------------------------


def test_run_registry_without_ready_json_prints_nothing_to_stdout(
    monkeypatch, tmp_path, capsys
):
    _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path))
    stop_event = threading.Event()

    rc = cli_module._run_registry(args, stop_event)

    assert rc == EXIT_OK
    out = capsys.readouterr()
    assert out.out == ""
    assert "mbregistry: listening on" in out.err


def test_run_registry_ready_json_single_line_with_all_bound_ports(
    monkeypatch, tmp_path, capsys
):
    _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path, ["--ready-json", "--instance", "loki"]))
    stop_event = threading.Event()

    rc = cli_module._run_registry(args, stop_event)

    assert rc == EXIT_OK
    out = capsys.readouterr()
    lines = [line for line in out.out.splitlines() if line.strip()]
    assert len(lines) == 1, f"expected exactly one stdout line, got: {out.out!r}"

    payload = json.loads(lines[0])
    assert payload["ready"] is True
    assert payload["instance"] == "loki"
    assert payload["socket"] == str(tmp_path / "api.sock")
    assert payload["ports"] == {
        "remote": 17999,
        "peer_pub": DEFAULT_PUB_PORT,
        "peer_snapshot": DEFAULT_SNAPSHOT_PORT,
        "pool": 17998,
        "names": 17997,
    }


def test_run_registry_ready_json_resolves_instance_to_short_hostname_when_omitted(
    monkeypatch, tmp_path, capsys
):
    import socket as _socket

    _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path, ["--ready-json"]))
    stop_event = threading.Event()

    cli_module._run_registry(args, stop_event)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["instance"] == _socket.gethostname().split(".")[0]


def test_run_registry_ready_json_omits_pool_when_no_relay_pool(
    monkeypatch, tmp_path, capsys
):
    captured, _ = _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path, ["--ready-json", "--no-relay-pool"]))
    stop_event = threading.Event()

    cli_module._run_registry(args, stop_event)

    payload = json.loads(capsys.readouterr().out.strip())
    assert "pool" not in payload["ports"]
    assert "assemble_relay_pool_kwargs" not in captured


# -- --no-peering forwarding + ready-json shape --------------------------


def test_run_registry_no_peering_forwarded_and_ready_json_omits_peer_ports(
    monkeypatch, tmp_path, capsys
):
    captured, _ = _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path, ["--ready-json", "--no-peering"]))
    stop_event = threading.Event()

    rc = cli_module._run_registry(args, stop_event)

    assert rc == EXIT_OK
    assert captured["assemble_registry_kwargs"]["no_peering"] is True
    assert captured["assemble_names_api_kwargs"]["name_set_callback"] is None
    assert captured["assemble_names_api_kwargs"]["name_clear_callback"] is None

    out = capsys.readouterr()
    lines = [line for line in out.out.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert set(payload["ports"]) == {"remote", "pool", "names"}
    assert "peering disabled" in out.err


def test_run_registry_no_peering_with_explicit_peer_does_not_raise(
    monkeypatch, tmp_path, capsys
):
    """``--peer`` alongside ``--no-peering`` has nothing to connect
    through (``peering`` is ``None``) -- must not crash, and must not
    print anything extra to stdout."""
    _install_fake_assembly(monkeypatch)
    parser = build_parser()
    args = parser.parse_args(
        _run_args(tmp_path, ["--no-peering", "--peer", "loki:7440"])
    )
    stop_event = threading.Event()

    rc = cli_module._run_registry(args, stop_event)

    assert rc == EXIT_OK
    out = capsys.readouterr()
    assert out.out == ""
    assert "--peer ignored" in out.err


# -- --exit-with-parent, wired through _run_registry ----------------------


def test_run_registry_exit_with_parent_stops_via_same_stop_event(monkeypatch, tmp_path):
    captured, fake_daemon = _install_fake_assembly(monkeypatch, loop_daemon=True)
    parser = build_parser()
    args = parser.parse_args(_run_args(tmp_path, ["--exit-with-parent"]))
    stop_event = threading.Event()

    read_fd, write_fd = os.pipe()
    read_file = os.fdopen(read_fd, "r")
    result: dict = {}

    def _target():
        result["rc"] = cli_module._run_registry(args, stop_event, stdin=read_file)

    thread = threading.Thread(target=_target)
    thread.start()
    try:
        _wait_until(lambda: fake_daemon.run_calls != [])
        assert not stop_event.is_set()
        assert thread.is_alive()

        os.close(write_fd)  # EOF -- the watcher must set stop_event

        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert stop_event.is_set()
        assert result["rc"] == EXIT_OK
    finally:
        read_file.close()
        if thread.is_alive():
            stop_event.set()
            thread.join(timeout=2.0)


# -- all three flags composed, per the spawn recipe ------------------------


def test_run_registry_composes_ready_json_exit_with_parent_no_peering(
    monkeypatch, tmp_path, capsys
):
    """``docs/design/robot-console-integration.md`` Sec.4 item 4's own
    spawn recipe shape: ``--ready-json --exit-with-parent --no-peering``
    alongside every port at 0. This is ticket 005's own AC5, "all three
    flags compose correctly together", exercised as one test."""
    captured, fake_daemon = _install_fake_assembly(monkeypatch, loop_daemon=True)
    parser = build_parser()
    args = parser.parse_args(
        _run_args(
            tmp_path,
            [
                "--instance",
                "test-console",
                "--remote-port",
                "0",
                "--peer-pub-port",
                "0",
                "--peer-snapshot-port",
                "0",
                "--pool-port",
                "0",
                "--names-port",
                "0",
                "--no-peering",
                "--ready-json",
                "--exit-with-parent",
            ],
        )
    )
    stop_event = threading.Event()

    read_fd, write_fd = os.pipe()
    read_file = os.fdopen(read_fd, "r")
    result: dict = {}

    def _target():
        result["rc"] = cli_module._run_registry(args, stop_event, stdin=read_file)

    thread = threading.Thread(target=_target)
    thread.start()
    try:
        _wait_until(lambda: fake_daemon.run_calls != [])
        assert not stop_event.is_set()

        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload == {
            "ready": True,
            "instance": "test-console",
            "socket": str(tmp_path / "api.sock"),
            "ports": {"remote": 17999, "pool": 17998, "names": 17997},
        }

        os.close(write_fd)
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert stop_event.is_set()
        assert result["rc"] == EXIT_OK
        assert captured["assemble_registry_kwargs"]["no_peering"] is True
    finally:
        read_file.close()
        if thread.is_alive():
            stop_event.set()
            thread.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Sprint 007 ticket 007 hardware finding: the spawn recipe over a *real*
# OS pipe, not the mocked-assembly/capsys shape every other test in this
# file uses (per this module's own docstring, "no real sockets/ports").
# capsys replaces ``sys.stdout`` with an in-memory object that has none
# of a real pipe's block-buffering behavior, so it could never have
# caught this: a real subprocess's stdout is block-buffered (not
# line-buffered) whenever it isn't a tty -- exactly the case for a real
# spawning parent reading this recipe's own ready line through a pipe,
# which is the literal, documented purpose of ``--ready-json``. Without
# an explicit ``flush=True`` on that one `print`, the JSON line sat in
# the child's stdout buffer forever (the main loop never writes anything
# else to stdout to flush it), and a parent honestly following
# docs/service.md's own spawn recipe would block on ``readline()``
# indefinitely. Caught running exactly that recipe from a real shell
# parent against real hardware (docs/acceptance/007-hardware.md).
# ---------------------------------------------------------------------------


def test_ready_json_line_reaches_a_real_pipe_promptly(socket_dir):
    """A real subprocess, real OS pipe, real (loopback, ephemeral-port)
    sockets -- no mocks. Reads the ready line with a short deadline: on
    the pre-fix code (``print`` with no ``flush=True``), this reliably
    times out and fails; with the fix, the line arrives promptly.

    Uses the ``socket_dir`` fixture (a short ``tempfile.mkdtemp()`` path),
    not ``tmp_path`` -- ``tmp_path``'s own per-test nested directory name
    (this test's own long, descriptive name included) reliably overflows
    ``AF_UNIX``'s ~104-byte path limit on macOS, which looks identical to
    the hang this test exists to catch (an empty read) unless its own
    stderr is inspected -- a real pitfall hit writing this test, not
    proving anything about the fix.
    """
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mbtools.registry.cli",
            "run",
            "--socket",
            f"{socket_dir}/api.sock",
            "--db",
            f"{socket_dir}/devices.db",
            "--remote-port",
            "0",
            "--pool-port",
            "0",
            "--names-port",
            "0",
            "--no-peering",
            "--ready-json",
            "--exit-with-parent",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        result: dict = {}

        def _read_ready_line():
            result["line"] = proc.stdout.readline()

        reader = threading.Thread(target=_read_ready_line, daemon=True)
        reader.start()
        reader.join(timeout=10.0)
        assert not reader.is_alive(), (
            "ready-json line never arrived on the real stdout pipe within "
            "10s -- the exact hang this test exists to catch (see the "
            "flush=True fix in cmd_run)"
        )
        assert result.get("line"), (
            f"empty read from stdout; child exit={proc.poll()!r}, "
            f"stderr={proc.stderr.read()!r}"
        )

        payload = json.loads(result["line"])
        assert payload["ready"] is True
        assert payload["ports"]["remote"] != 0

        # --exit-with-parent: closing stdin should end the child promptly.
        proc.stdin.close()
        assert proc.wait(timeout=10.0) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
