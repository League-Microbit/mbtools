"""Tests for sprint 005 ticket 005's addition: a Windows named-pipe
transport for ``registry.client.RegistryClient`` -- ticket 003 left this
class's Windows side as a documented seam (``RegistryClient``'s own
former "Windows gap" docstring note); this closes it.

Every test here monkeypatches ``client.sys.platform`` to ``"win32"``
(mirroring ``tests/registry/api/test_api.py``'s own ``default_peer_pid``
precedent) and injects a fake ``win32=`` double implementing
``api_windows._Win32PipeAPI``'s client-relevant method surface
(``open_client_pipe``/``read_file``/``write_file``/``close_handle``),
never touching a real ``ctypes.windll`` call -- mirrors
``tests/registry/api_windows/test_api_windows.py``'s own fake-transport
precedent for the server side of this same module.

A real pipe round-trip -- server *and* client both for real, against an
actual Windows named pipe -- needs actual Windows and is
``skipif(sys.platform != "win32")`` below, first exercised by ticket
006's ``windows-latest`` CI job (this suite never runs it).
"""

from __future__ import annotations

import json
import sys

import pytest

from mbtools.registry import client as client_module
from mbtools.registry.client import RegistryClient, RegistryUnavailable


class _FakeClientWin32:
    """Stands in for ``api_windows._Win32PipeAPI``'s own method surface,
    from the *client*'s point of view: ``open_client_pipe`` hands back a
    fresh integer handle (or raises, if ``fail_open`` is set, mirroring
    a real ``CreateFileW`` failure -- no listener, or a wrong pipe
    name), ``write_file`` records what was sent, and ``read_file`` plays
    back pre-scripted response bytes -- mirrors
    ``test_api_windows.py``'s own ``_ScriptedLifecycleWin32``/
    ``_ScriptedWin32ForFraming`` fakes, but for the client's own open
    call instead of the server's ``create_named_pipe``/
    ``connect_named_pipe`` pair.
    """

    def __init__(self, response_lines=(), *, fail_open=False):
        self._inbound = "".join(
            line if line.endswith("\n") else line + "\n" for line in response_lines
        ).encode("utf-8")
        self.fail_open = fail_open
        self.opened: list[str] = []
        self.written: list[bytes] = []
        self.closed: list[int] = []
        self._next_handle = 1

    def open_client_pipe(self, name):
        if self.fail_open:
            raise OSError(f"api_windows: CreateFileW failed for {name!r}")
        self.opened.append(name)
        handle = self._next_handle
        self._next_handle += 1
        return handle

    def read_file(self, handle, size):
        data, self._inbound = self._inbound[:size], self._inbound[size:]
        return data

    def write_file(self, handle, data):
        self.written.append(data)

    def close_handle(self, handle):
        self.closed.append(handle)


PIPE_NAME = r"\\.\pipe\mbregistry"


# ---------------------------------------------------------------------------
# connect / close
# ---------------------------------------------------------------------------


def test_connect_opens_the_named_pipe_via_open_client_pipe_on_simulated_win32(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32()
    client = RegistryClient(PIPE_NAME, win32=fake)

    client.connect()

    assert fake.opened == [PIPE_NAME]
    assert isinstance(client.socket_path, str)  # never routed through pathlib.Path

    client.close()
    assert fake.closed == [1]


def test_connect_is_idempotent_no_second_open_call(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32()
    client = RegistryClient(PIPE_NAME, win32=fake)

    client.connect()
    client.connect()

    assert fake.opened == [PIPE_NAME]
    client.close()


def test_connect_raises_registry_unavailable_when_createfilew_fails(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32(fail_open=True)
    client = RegistryClient(PIPE_NAME, win32=fake)

    with pytest.raises(RegistryUnavailable):
        client.connect()


def test_close_is_idempotent_and_safe_on_a_never_connected_instance(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32()
    client = RegistryClient(PIPE_NAME, win32=fake)

    client.close()
    client.close()

    assert fake.closed == []


def test_context_manager_connects_and_closes(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32([json.dumps({"ok": True, "devices": []})])

    with RegistryClient(PIPE_NAME, win32=fake) as client:
        assert fake.opened == [PIPE_NAME]
        client.list()

    assert fake.closed == [1]


# ---------------------------------------------------------------------------
# wire protocol round-trip over the fake pipe transport
# ---------------------------------------------------------------------------


def test_list_round_trips_over_the_fake_pipe_transport(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    resp = json.dumps({"ok": True, "devices": [{"uid": "abc123"}]})
    fake = _FakeClientWin32([resp])

    with RegistryClient(PIPE_NAME, win32=fake) as client:
        devices = client.list()

    assert devices == [{"uid": "abc123"}]
    assert len(fake.written) == 1
    sent = json.loads(fake.written[0].decode("utf-8").strip())
    assert sent == {"op": "list"}


def test_find_round_trips_and_raises_typed_error_on_not_found(monkeypatch):
    from mbtools.registry.client import DeviceNotFoundError

    monkeypatch.setattr(client_module.sys, "platform", "win32")
    resp = json.dumps({"ok": False, "code": "not_found", "error": "no such device"})
    fake = _FakeClientWin32([resp])

    with RegistryClient(PIPE_NAME, win32=fake) as client:
        with pytest.raises(DeviceNotFoundError):
            client.find("nonexistent")


def test_connection_closed_mid_session_raises_registry_unavailable(monkeypatch):
    monkeypatch.setattr(client_module.sys, "platform", "win32")
    fake = _FakeClientWin32([])  # no scripted response -- read_file returns b"" (EOF)

    with RegistryClient(PIPE_NAME, win32=fake) as client:
        with pytest.raises(RegistryUnavailable):
            client.list()


# ---------------------------------------------------------------------------
# real pipe round-trip -- only on actual Windows (ticket 006's CI job)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="needs a real Win32 named pipe; exercised by the windows-latest CI job (ticket 006)",
)
def test_real_pipe_round_trip_server_and_client_together(tmp_path):
    from mbtools.registry.api_windows import WindowsPipeAPIServer
    from mbtools.registry.locks import LockManager
    from mbtools.registry.store import Store

    pipe_name = r"\\.\pipe\mbregistry-test-client-real"
    store = Store(tmp_path / "devices.db")
    locks = LockManager()
    server = WindowsPipeAPIServer(
        pipe_name=pipe_name, store=store, locks=locks, sweep_interval_s=100.0
    )
    server.start()
    try:
        with RegistryClient(pipe_name) as client:
            devices = client.list()
        assert devices == []
    finally:
        server.stop()
        store.close()
