"""Tests for ``mbrelay`` (sprint 004, ticket 005) -- ``mbtools.relay.cli``.

Most tests here drive the CLI's internal orchestration functions
(:func:`~mbtools.relay.cli._run_connect` and friends) directly against a
:class:`FakeRegistryClient` -- a hand-rolled double that records every
call it receives -- rather than a real ``RegistryAPIServer``/socket.
This is deliberate, not a shortcut: the ticket's own acceptance criterion
("``mbrelay connect`` never enumerates or probes devices itself ...
verified by asserting on the registry client mock in tests, not just by
inspection") asks for exactly this shape of test, and it also sidesteps
needing a full ``RemoteAPIServer``/hardware-adjacent fixture just to
prove the CLI's own resolve/lock/tune/release sequencing and error
messages -- the lower layers (``relay.protocol``/``relay.channel``) have
their own, already-passing test suites (tickets 003/004) that prove the
real threaded channel/board-sequence fidelity; this file is not trying
to re-prove that.

:class:`FakeByteChannel` is a synchronous, in-memory ``ByteChannel`` --
the same shape ``tests/relay/test_protocol.py``'s own fake uses, extended
here with the ``!CG``/``!GO``/``PING`` replies this CLI's own tuning
step (``cli._tune``, not part of ``relay.protocol``'s vocabulary) needs.
``_open_local_channel``/``_open_remote_channel`` are the CLI's own test
seam for substituting it (mirrors ``serial.connect``'s ``_pyserial``
module-level swap point).

The ``mbrelay names ...`` subcommands are additionally proven end to end
against a real ``RegistryAPIServer``/``Store`` (mirroring ``tests/serial/
test_mbserial_cli.py``'s own convention) -- the acceptance criterion asks
for a genuine round-trip "against ``registry.store``'s methods from
ticket 001", which a fake client cannot demonstrate by itself.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
from typing import Any, Callable

import pytest

from mbtools.common import (
    EXIT_ERROR,
    EXIT_HARDWARE,
    EXIT_LOCKED,
    EXIT_NO_DEVICE,
    EXIT_OK,
    EXIT_USAGE,
)
from mbtools.registry.api import RegistryAPIServer
from mbtools.registry.client import (
    DeviceLockedError,
    DeviceNotFoundError,
    RegistryClientError,
)
from mbtools.registry.flash import FlashOp
from mbtools.registry.locks import KIND_RELAY, LockManager
from mbtools.registry.store import Store
from mbtools.relay import cli as cli_mod
from mbtools.relay import naming

# ---------------------------------------------------------------------------
# FakeByteChannel -- a synchronous, in-memory ByteChannel (same shape as
# test_protocol.py's own; not imported from there -- see module docstring
# on cross-test-file imports).
# ---------------------------------------------------------------------------

RADIOBRIDGE_BANNER = b"DEVICE:RADIOBRIDGE:relay:togov:1234\n"

NORMALIZE_ACKS: dict[bytes, bytes] = {
    b"!MODE RAW250\n": b"# mode: RAW250\n",
    b"!FRAG OFF\n": b"# frag: OFF\n",
    b"!ECHO OFF\n": b"# echo: OFF\n",
    b"!P 7\n": b"# channel: 0 group: 10 mode: RAW250 power: 7\n",
    b"!C 0\n": b"# channel: 0 group: 10\n",
}
DEFAULT_QUERY_REPLY = b"# channel: 0 group: 10 mode: RAW250 power: 7\n"


class FakeByteChannel:
    """A scripted, in-memory :class:`~mbtools.relay.protocol.ByteChannel`.
    See ``tests/relay/test_protocol.py``'s ``FakeByteChannel`` for the
    same shape -- ``script`` maps a written command to a fixed reply or a
    ``(channel) -> bytes | None`` callable, delivered synchronously from
    inside :meth:`write_nowait`.
    """

    def __init__(self, script: "dict[bytes, Any] | None" = None) -> None:
        self.script = dict(script or {})
        self.written: list[bytes] = []
        self.break_calls = 0
        self.opened = False
        self.closed = False
        self.start_reading_calls = 0
        self._on_data: Callable[[bytes], None] = lambda _: None
        self._on_error: Callable[[object], None] = lambda _: None

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.closed = True

    def start_reading(self, on_data, on_error) -> None:
        self.start_reading_calls += 1
        self._on_data, self._on_error = on_data, on_error

    def stop_reading(self) -> None:
        self._on_data = lambda _: None
        self._on_error = lambda _: None

    def write_nowait(self, data: bytes) -> None:
        self.written.append(data)
        handler = self.script.get(data)
        if handler is None:
            return
        reply = handler(self) if callable(handler) else handler
        if reply:
            self._on_data(reply)

    def send_break(self, duration: float = 0.4) -> None:
        self.break_calls += 1

    def drain(self, timeout: float = 2.0) -> None:
        pass

    @property
    def pending_bytes(self) -> int:
        return 0

    def set_watermarks(self, on_high, on_low) -> None:
        pass


def full_script(channel: int, group: int, *, pong: bool = True) -> dict[bytes, Any]:
    script = dict(NORMALIZE_ACKS)
    script[b"?\n"] = DEFAULT_QUERY_REPLY
    script[b"HELLO\n"] = RADIOBRIDGE_BANNER
    script[b"!VER?\n"] = b"# version: 1.2.3\n"
    script[b"!DEFAULTS\n"] = b"# stored config cleared\n"
    script[f"!CG {channel} {group}\n".encode()] = f"# channel: {channel} group: {group}\n".encode()
    script[b"!GO\n"] = b"# entering data plane\n"
    if pong:
        script[b"PING\n"] = b"pong 12\n"
    return script


# ---------------------------------------------------------------------------
# FakeRegistryClient -- records every call, so "never enumerates or
# probes devices itself" is an assertion, not an inspection.
# ---------------------------------------------------------------------------


class FakeRegistryClient:
    def __init__(self, devices=(), names=None, lock_errors=None):
        self._devices = list(devices)
        self._names = dict(names or {})
        self._lock_errors = dict(lock_errors or {})
        self.calls: list[tuple] = []
        self.locked: set[str] = set()

    def list(self):
        self.calls.append(("list",))
        return list(self._devices)

    def find(self, uid):  # pragma: no cover -- must never be called
        self.calls.append(("find", uid))
        raise AssertionError(
            "mbrelay connect must resolve a relay via list()/lock(), never find()"
        )

    def lock(self, uid, kind):
        self.calls.append(("lock", uid, kind))
        if uid in self._lock_errors:
            raise self._lock_errors[uid]
        self.locked.add(uid)

    def unlock(self, uid):
        self.calls.append(("unlock", uid))
        was_locked = uid in self.locked
        self.locked.discard(uid)
        return was_locked

    def names_get(self, name):
        self.calls.append(("names_get", name))
        return self._names.get(name)

    def names_set(self, name, channel, group):
        entry = {
            "name": name, "channel": channel, "group": group,
            "source": "registry", "updated": 1.0, "conflict": [], "channel_conflict": [],
        }
        self._names[name] = entry
        self.calls.append(("names_set", name, channel, group))
        return entry

    def names_clear(self, name):
        self.calls.append(("names_clear", name))
        self._names.pop(name, None)

    def names_list(self):
        self.calls.append(("names_list",))
        return list(self._names.values())


LOCAL_RELAY = {
    "uid": "uid-local-relay",
    "device_name": "togov",
    "role": "RADIOBRIDGE",
    "host": None,
    "lock_kind": None,
    "port": "/dev/fake-relay0",
}

REMOTE_RELAY = {
    "uid": "uid-remote-relay",
    "device_name": "meili-relay",
    "role": "RADIOBRIDGE",
    "host": "meili",
    "lock_kind": None,
    "endpoint": "meili:7440",
}

NAME_ENTRY_TOVEZ = {
    "name": "tovez", "channel": 20, "group": 30,
    "source": "registry", "updated": 1.0, "conflict": [], "channel_conflict": [],
}


def _connect_args(target: str, extra: "list[str] | None" = None):
    argv = ["connect", target, *(extra or [])]
    return cli_mod.build_parser().parse_args(argv)


@pytest.fixture(autouse=True)
def _fast_timing(monkeypatch):
    """Zero out every real ``time.sleep`` this module's own orchestration
    calls (``TUNE_SETTLE_S``, ``RelayControl.open_settle``) -- harmless
    with :class:`FakeByteChannel`'s synchronous replies (nothing here
    ever genuinely waits on I/O), purely so the suite stays fast. Mirrors
    ``tests/serial/test_mbserial_cli.py``'s own ``_install_fake_pyserial``
    zeroing out ``serial.connect``'s settle constants.
    """
    monkeypatch.setattr(cli_mod, "TUNE_SETTLE_S", 0.0)
    monkeypatch.setattr(
        cli_mod, "_make_control",
        lambda: cli_mod.RelayControl(
            open_settle=0.0, hello_timeout=0.05, hello_attempts=1,
            break_duration=0.0, break_settle=0.0,
        ),
    )


@pytest.fixture(autouse=True)
def _immediate_stdin_eof(monkeypatch):
    """Every interactive-mode test wants the session to end immediately
    -- an empty ``io.StringIO`` has no ``fileno()``, so :func:`~mbtools.
    relay.cli._interactive` takes its non-tty fallback branch and reads
    "" on the first ``readline()``, exactly like ``test_mbserial_cli.py``'s
    own convention for the same reason.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


# ---------------------------------------------------------------------------
# target parsing
# ---------------------------------------------------------------------------


def test_parse_target_robot_only():
    target = cli_mod.parse_target("tovez")
    assert target.robot == "tovez"
    assert target.host is None


def test_parse_target_robot_at_host():
    target = cli_mod.parse_target("tovez@meili")
    assert target.robot == "tovez"
    assert target.host == "meili"


def test_parse_target_malformed_robot_raises():
    with pytest.raises(ValueError):
        cli_mod.parse_target("not-a-name")


def test_parse_target_empty_host_after_at_raises():
    with pytest.raises(ValueError):
        cli_mod.parse_target("tovez@")


# ---------------------------------------------------------------------------
# _find_free_relay
# ---------------------------------------------------------------------------


def test_find_free_relay_filters_role_lock_and_host():
    client = FakeRegistryClient(devices=[
        {"uid": "a", "role": "NEZHA2", "lock_kind": None, "host": None},
        {"uid": "b", "role": "RADIOBRIDGE", "lock_kind": "relay", "host": None},
        {"uid": "c", "role": "RADIOBRIDGE", "lock_kind": None, "host": "elsewhere"},
        {"uid": "d", "role": "RADIORELAY", "lock_kind": None, "host": None},
    ])
    found = cli_mod._find_free_relay(client, None)
    assert found["uid"] == "d"


def test_find_free_relay_honors_host_scope():
    client = FakeRegistryClient(devices=[
        {"uid": "d", "role": "RADIORELAY", "lock_kind": None, "host": None},
        {"uid": "c", "role": "RADIOBRIDGE", "lock_kind": None, "host": "elsewhere"},
    ])
    found = cli_mod._find_free_relay(client, "elsewhere")
    assert found["uid"] == "c"


def test_find_free_relay_returns_none_when_nothing_matches():
    client = FakeRegistryClient(devices=[{"uid": "a", "role": "NEZHA2", "lock_kind": None, "host": None}])
    assert cli_mod._find_free_relay(client, None) is None


# ---------------------------------------------------------------------------
# local connect: full resolve/lock/normalize/tune/interact/release flow
# ---------------------------------------------------------------------------


def test_local_connect_end_to_end(monkeypatch, capsys):
    fake_channel = FakeByteChannel(full_script(20, 30))
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)

    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args("tovez")

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

    assert code == EXIT_OK
    # Every device access went through the registry client -- list() to
    # find a free relay, lock()/unlock() around the session -- never a
    # direct probe (find() would raise if called; see FakeRegistryClient).
    assert client.calls == [
        ("list",),
        ("names_get", "tovez"),
        ("lock", "uid-local-relay", KIND_RELAY),
        ("unlock", "uid-local-relay"),
    ]
    assert fake_channel.opened is True
    assert fake_channel.closed is True
    # The full acquire sequence ran: HELLO, !VER?, normalize batch, ?,
    # tune (!CG/!GO/PING), then !DEFAULTS on release.
    assert fake_channel.written[:3] == [b"HELLO\n", b"!VER?\n", b"!MODE RAW250\n"]
    assert b"!CG 20 30\n" in fake_channel.written
    assert b"!GO\n" in fake_channel.written
    assert b"PING\n" in fake_channel.written
    assert fake_channel.written[-1] == b"!DEFAULTS\n"

    err = capsys.readouterr().err
    assert "reset and normalized" in err
    assert "tuned to tovez: channel 20 group 30" in err
    assert "answered PING" in err


def test_interactive_session_survives_non_tty_real_fd_stdin(monkeypatch, capsys):
    """Regression test (sprint 005 ticket 010, found on real hardware):
    a redirected file, a pipe, or ``/dev/null`` gives ``sys.stdin`` a
    real file descriptor (``fileno()`` succeeds) while ``os.isatty()``
    is still ``False`` -- unlike the ``_immediate_stdin_eof`` autouse
    fixture's ``io.StringIO("")``, which has no ``fileno()`` at all and
    so never exercised this branch. Before the fix, ``_interactive``
    chose its ``select``-based read loop whenever ``stdin_fd is not
    None`` (true here), but only imports ``select``/``termios``/``tty``
    when ``is_tty`` is also true (false here) -- crashing with
    ``AttributeError: 'NoneType' object has no attribute 'select'`` on
    the very first loop iteration. This is exactly what happened running
    ``mbrelay connect`` with stdin redirected from ``/dev/null`` during
    this ticket's hardware acceptance pass.
    """
    import os

    devnull = open(os.devnull, "r")
    monkeypatch.setattr(sys, "stdin", devnull)
    try:
        assert devnull.fileno() is not None
        assert os.isatty(devnull.fileno()) is False

        fake_channel = FakeByteChannel(full_script(20, 30))
        monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
        client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
        args = _connect_args("tovez")

        code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

        assert code == EXIT_OK
        err = capsys.readouterr().err
        assert "relay closed the connection" not in err
    finally:
        devnull.close()


def test_local_connect_never_calls_open_remote_channel(monkeypatch):
    fake_channel = FakeByteChannel(full_script(20, 30))
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)

    def _forbidden(stream):
        raise AssertionError("a local connect must never open a remote channel")

    monkeypatch.setattr(cli_mod, "_open_remote_channel", _forbidden)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), _connect_args("tovez"))
    assert code == EXIT_OK


# ---------------------------------------------------------------------------
# error flows: two distinct, correctly-labeled errors
# ---------------------------------------------------------------------------


def test_no_free_relay_is_reported_distinctly(capsys):
    client = FakeRegistryClient(devices=[], names={"tovez": NAME_ENTRY_TOVEZ})

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), _connect_args("tovez"))

    assert code == EXIT_NO_DEVICE
    err = capsys.readouterr().err
    assert "no free relay" in err
    # names_get was never reached -- there is nothing to tune yet.
    assert ("names_get", "tovez") not in client.calls


def test_unregistered_robot_connects_on_its_derived_link(monkeypatch, capsys):
    channel, group = naming.name_to_radio("vevov")
    fake_channel = FakeByteChannel(full_script(channel, group))
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={})

    code = cli_mod._run_connect(client, cli_mod.parse_target("vevov"), _connect_args("vevov"))

    assert code == EXIT_OK
    assert f"!CG {channel} {group}\n".encode() in fake_channel.written
    assert "source: derived" in capsys.readouterr().err
    # Deriving is a read: connect never writes a name-registry row.
    assert not any(call[0] == "names_set" for call in client.calls)


def test_resolve_entry_prefers_the_registered_link():
    client = FakeRegistryClient(names={"tovez": NAME_ENTRY_TOVEZ})
    assert cli_mod.resolve_entry(client, "tovez") == NAME_ENTRY_TOVEZ


def test_resolve_entry_derives_an_unregistered_name():
    channel, group = naming.name_to_radio("vevov")
    entry = cli_mod.resolve_entry(FakeRegistryClient(), "vevov")
    assert entry == {"name": "vevov", "channel": channel, "group": group, "source": "derived"}


def test_already_locked_reports_holder(capsys):
    fake_channel = FakeByteChannel(full_script(20, 30))
    client = FakeRegistryClient(
        devices=[LOCAL_RELAY],
        names={"tovez": NAME_ENTRY_TOVEZ},
        lock_errors={
            "uid-local-relay": DeviceLockedError(
                "locked", "already locked", {"kind": "relay", "pid": 4242}
            )
        },
    )

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), _connect_args("tovez"))

    assert code == EXIT_LOCKED
    assert "locked for relay by pid 4242" in capsys.readouterr().err
    assert not fake_channel.opened  # never reached the channel at all


# ---------------------------------------------------------------------------
# --send/--expect scripting mode
# ---------------------------------------------------------------------------


def test_send_expect_scripting_mode_matches_and_exits_ok(monkeypatch, capsys):
    script = full_script(20, 30)
    script[b"SET SPEED 50\n"] = b"# ok\n"
    fake_channel = FakeByteChannel(script)
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args("tovez", ["--send", "SET SPEED 50", "--expect", "ok", "--timeout", "2"])

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

    assert code == EXIT_OK
    assert b"SET SPEED 50\n" in fake_channel.written
    assert "# ok" in capsys.readouterr().out


def test_send_expect_scripting_mode_times_out_reports_error(monkeypatch, capsys):
    script = full_script(20, 30)
    fake_channel = FakeByteChannel(script)  # no reply scripted for the send
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args(
        "tovez", ["--send", "PING ROBOT", "--expect", "never-happens", "--timeout", "0.2"]
    )

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

    assert code == EXIT_ERROR
    assert "never saw" in capsys.readouterr().err


def test_send_without_expect_prints_the_reply(monkeypatch, capsys):
    script = full_script(20, 30)
    script[b"SPEED?\n"] = b"speed 50\n"
    fake_channel = FakeByteChannel(script)
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    monkeypatch.setattr(cli_mod, "SCRIPT_IDLE_GAP_S", 0.0)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args("tovez", ["--send", "SPEED?", "--timeout", "2"])

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

    assert code == EXIT_OK
    assert "speed 50" in capsys.readouterr().out


def test_send_without_expect_and_no_reply_is_an_error(monkeypatch, capsys):
    fake_channel = FakeByteChannel(full_script(20, 30))
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args("tovez", ["--send", "SPEED?", "--timeout", "0.2"])

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), args)

    assert code == EXIT_ERROR
    assert "no response" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# mbserial <robot>: no attached device by that name -> through a relay
# ---------------------------------------------------------------------------


class _NoAttachedDeviceClient(FakeRegistryClient):
    def find(self, uid):
        self.calls.append(("find", uid))
        raise DeviceNotFoundError("not_found", f"no such device: {uid!r}")


def _mbserial_args(argv):
    from mbtools.serial import cli as serial_cli
    return serial_cli, serial_cli.build_parser().parse_args(argv)


def test_mbserial_falls_back_to_a_relay_for_a_robot_name(monkeypatch, capsys):
    channel, group = naming.name_to_radio("vevov")
    script = full_script(channel, group)
    script[b"GREET\n"] = b"hi from vevov\n"
    fake_channel = FakeByteChannel(script)
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    monkeypatch.setattr(cli_mod, "SCRIPT_IDLE_GAP_S", 0.0)
    client = _NoAttachedDeviceClient(devices=[LOCAL_RELAY], names={})
    serial_cli, args = _mbserial_args(["vevov", "GREET"])

    code = serial_cli._run_connect(client, args)

    assert code == EXIT_OK
    assert f"!CG {channel} {group}\n".encode() in fake_channel.written
    out, err = capsys.readouterr()
    assert "hi from vevov" in out
    assert "radio relay" in err
    assert ("unlock", "uid-local-relay") in client.calls


def test_mbserial_keeps_no_such_device_for_a_non_name(capsys):
    client = _NoAttachedDeviceClient(devices=[LOCAL_RELAY], names={})
    serial_cli, args = _mbserial_args(["robot1", "HELLO"])

    code = serial_cli._run_connect(client, args)

    assert code == EXIT_NO_DEVICE
    assert "no such device" in capsys.readouterr().err
    assert not any(call[0] == "lock" for call in client.calls)


# ---------------------------------------------------------------------------
# remote connect: locks/opens a stream on the owning host, never a local port
# ---------------------------------------------------------------------------


class _FakeRemoteStream:
    pass


class FakeRemoteRegistryClient:
    """Stands in for ``registry.remote_client.RemoteRegistryClient`` --
    records lock/open_stream calls, never opens a real socket.
    """
    instances: list["FakeRemoteRegistryClient"] = []

    def __init__(self, host, port, **kwargs):
        self.host = host
        self.port = port
        self.calls: list[tuple] = []
        FakeRemoteRegistryClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def lock(self, uid, kind):
        self.calls.append(("lock", uid, kind))

    def open_stream(self, uid):
        self.calls.append(("open_stream", uid))
        return _FakeRemoteStream()


def test_remote_connect_never_opens_a_local_port(monkeypatch, capsys):
    FakeRemoteRegistryClient.instances.clear()
    monkeypatch.setattr(cli_mod, "RemoteRegistryClient", FakeRemoteRegistryClient)
    fake_channel = FakeByteChannel(full_script(20, 30))
    monkeypatch.setattr(cli_mod, "_open_remote_channel", lambda stream: fake_channel)

    def _forbidden(port):
        raise AssertionError("a remote connect must never open a local port")

    monkeypatch.setattr(cli_mod, "_open_local_channel", _forbidden)

    client = FakeRegistryClient(devices=[REMOTE_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})
    args = _connect_args("tovez@meili")

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez@meili"), args)

    assert code == EXIT_OK
    assert len(FakeRemoteRegistryClient.instances) == 1
    remote = FakeRemoteRegistryClient.instances[0]
    assert remote.host == "meili"
    assert remote.port == 7440
    assert remote.calls == [("lock", "uid-remote-relay", KIND_RELAY), ("open_stream", "uid-remote-relay")]
    # The local RegistryClient's own lock/unlock were never used for the
    # relay itself -- only the remote connection's lock, released by
    # closing the stream (module docstring).
    assert not any(call[0] in ("lock", "unlock") for call in client.calls)
    assert "tuned to tovez" in capsys.readouterr().err


def test_remote_connect_bad_endpoint_reports_cleanly(capsys):
    device = dict(REMOTE_RELAY)
    device["endpoint"] = ""
    client = FakeRegistryClient(devices=[device], names={"tovez": NAME_ENTRY_TOVEZ})

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez@meili"), _connect_args("tovez@meili"))

    assert code == EXIT_ERROR
    assert "no usable endpoint" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# hardware failure surfaces as EXIT_HARDWARE, not a stack trace
# ---------------------------------------------------------------------------


def test_hello_failure_reports_hardware_error(monkeypatch, capsys):
    fake_channel = FakeByteChannel({})  # HELLO never answered
    monkeypatch.setattr(cli_mod, "_open_local_channel", lambda port: fake_channel)
    client = FakeRegistryClient(devices=[LOCAL_RELAY], names={"tovez": NAME_ENTRY_TOVEZ})

    code = cli_mod._run_connect(client, cli_mod.parse_target("tovez"), _connect_args("tovez"))

    assert code == EXIT_HARDWARE
    assert fake_channel.closed is True  # still released even on failure
    assert "no DEVICE banner" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_connect: usage error for a malformed target
# ---------------------------------------------------------------------------


def test_cmd_connect_malformed_target_reports_usage(capsys):
    args = cli_mod.build_parser().parse_args(["connect", "not-a-name"])
    code = cli_mod.cmd_connect(args)
    assert code == EXIT_USAGE
    assert "not a micro:bit name" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# mbrelay names get/set/clear/list -- real round-trip against a real
# RegistryAPIServer/Store (registry.store's own methods from ticket 001).
# ---------------------------------------------------------------------------


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbrelay-cli-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


@pytest.fixture
def server(socket_dir, store):
    locks = LockManager()
    flash_op = FlashOp(locks=locks, store=store)
    srv = RegistryAPIServer(
        socket_path=f"{socket_dir}/api.sock",
        store=store,
        locks=locks,
        flash_op=flash_op,
        sweep_interval_s=100.0,
    )
    srv.start()
    yield srv
    srv.stop()


@pytest.mark.requires_af_unix
def test_names_get_reports_not_registered(server, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "get", "tovez", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_ERROR
    assert "not in the name registry" in capsys.readouterr().err


@pytest.mark.requires_af_unix
def test_names_set_get_list_clear_round_trip(server, store, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "set", "tovez", "20", "30", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK
    assert "channel 20 group 30" in capsys.readouterr().out

    # Landed in the real Store, not just this connection's own view.
    row = store.get_name("tovez")
    assert row is not None
    assert (row.channel, row.group) == (20, 30)

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "get", "tovez", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK
    assert "channel 20 group 30" in capsys.readouterr().out

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "list", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK
    assert "tovez" in capsys.readouterr().out

    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "clear", "tovez", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK

    assert store.get_name("tovez") is None


@pytest.mark.requires_af_unix
def test_names_list_reports_when_empty(server, capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_mod.main(["names", "list", "--socket", str(server.socket_path)])
    assert excinfo.value.code == EXIT_OK
    assert "no names registered" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# local-registry address resolution on Windows (ticket 006, team-lead scope)
# ---------------------------------------------------------------------------


def test_cli_resolves_windows_pipe_name_with_no_socket_override(monkeypatch):
    """Regression test for the gap ticket 005 flagged and ticket 006 closed:
    every ``mbrelay`` call site that builds a :class:`RegistryClient`
    (``cmd_connect``/``_with_client``) used to call ``resolve_socket_path(
    args.socket, _SOCKET_ENV_VAR, DEFAULT_SOCKET_PATH)`` directly --
    ``DEFAULT_SOCKET_PATH`` is ``None`` on ``sys.platform == "win32"``, so
    with no ``--socket``/``$MBREGISTRY_SOCKET`` override that call raised
    ``TypeError`` from ``Path(None)`` before ever reaching
    ``RegistryClient``. ``cli.py`` now resolves through
    ``registry.client.resolve_local_api_address`` (imported as
    ``cli_mod.resolve_local_api_address``) instead, which dispatches on
    ``sys.platform`` first and returns the Windows named-pipe default
    rather than raising.
    """
    import sys

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("MBREGISTRY_SOCKET", raising=False)
    result = cli_mod.find_local_api_address(None, cli_mod._SOCKET_ENV_VAR)
    assert result == r"\\.\pipe\mbregistry"
    assert isinstance(result, str)
