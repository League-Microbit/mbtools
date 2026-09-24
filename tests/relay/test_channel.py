"""Tests for ``mbtools.relay.channel`` (sprint 004, ticket 004).

Each adapter is exercised two ways: directly (open/close, write_nowait,
send_break, the no-op paths) and end-to-end through ticket 003's
``RelayControl.reset_and_normalize()`` -- the acceptance criteria's own
bar ("verified by running ... against it") -- against a scripted fake of
the transport each adapter wraps: :class:`ScriptedSerial` (pyserial-
shaped) for :class:`~mbtools.relay.channel.LocalRelayChannel`, and
:class:`FakeRemoteStream` (``RemoteStream``-shaped) for
:class:`~mbtools.relay.channel.RemoteRelayChannel`. Both fakes are driven
by the same command -> scripted-reply shape ``test_protocol.
FakeByteChannel`` already uses, adapted to each adapter's own background
read thread (real threads, real queues/locks -- these adapters are not
same-thread fakes themselves, so the tests have to actually exercise the
threading).
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

import pytest

from mbtools.relay.channel import LocalRelayChannel, RemoteRelayChannel
from mbtools.relay.protocol import (
    DEFAULT_CFG,
    NORMALIZE_STEPS,
    Reader,
    RelayControl,
    RelayError,
)

# A relay board's banner in the current, colon-delimited dialect -- same
# fixture data as test_protocol.py, duplicated here for this test file's
# own self-containment (matches that file's own convention).
RADIOBRIDGE_BANNER = b"DEVICE:RADIOBRIDGE:relay:togov:1234\n"

NORMALIZE_ACKS: dict[bytes, bytes] = {
    b"!MODE RAW250\n": b"# mode: RAW250\n",
    b"!FRAG OFF\n": b"# frag: OFF\n",
    b"!ECHO OFF\n": b"# echo: OFF\n",
    b"!P 7\n": b"# channel: 0 group: 10 mode: RAW250 power: 7\n",
    b"!C 0\n": b"# channel: 0 group: 10\n",
}
DEFAULT_QUERY_REPLY = b"# channel: 0 group: 10 mode: RAW250 power: 7\n"


def normalize_script(query_reply: bytes = DEFAULT_QUERY_REPLY,
                     hello_reply: bytes = RADIOBRIDGE_BANNER) -> dict:
    """A full script for reset_and_normalize(): HELLO, !VER?, the
    normalize batch, and its verification query -- same shape as
    ``test_protocol.normalize_script``."""
    script = dict(NORMALIZE_ACKS)
    script[b"?\n"] = query_reply
    script[b"HELLO\n"] = hello_reply
    script[b"!VER?\n"] = b"# version: 1.2.3\n"
    return script


def make_control(**overrides) -> RelayControl:
    """A RelayControl with timeouts generous enough for real background
    threads (unlike test_protocol.py's same-thread fake, these adapters'
    replies arrive via an actual reader thread, so timeouts need real
    margin) but still small enough to keep the suite fast."""
    kwargs = dict(open_settle=0.0, hello_timeout=0.5, hello_attempts=2,
                  post_close_settle=0.0, break_duration=0.05, break_settle=0.05)
    kwargs.update(overrides)
    return RelayControl(**kwargs)


# ---------------------------------------------------------------------------
# LocalRelayChannel -- ScriptedSerial, a pyserial-shaped fake
# ---------------------------------------------------------------------------

class ScriptedSerial:
    """A pyserial-shaped fake driven by a command -> reply script (fixed
    bytes, or a callable ``(fake) -> bytes | None`` for stateful
    behavior, e.g. "only answer HELLO after a break" -- same shape
    ``test_protocol.FakeByteChannel`` uses).

    Replies land in a lock-protected buffer as soon as the matching
    ``write()`` happens (synchronously, same thread) so
    :class:`LocalRelayChannel`'s own background read thread -- calling
    ``read(max(1, in_waiting))`` in a loop, exactly like ``serial.connect.
    interact``'s pump -- picks them up on its very next iteration, same as
    a real port would.
    """

    def __init__(self, *, script: "dict[bytes, Any] | None" = None,
                 break_script: Any = None, read_delay: float = 0.005,
                 **serial_kwargs: object) -> None:
        self.script = dict(script or {})
        self.break_script = break_script
        self._read_delay = read_delay
        self._lock = threading.Lock()
        self._buf = bytearray()
        self.written: list[bytes] = []
        self.break_calls: list[float] = []
        self.open_calls = 0
        self.close_calls = 0
        self.is_open = False
        self._dtr = False
        self._rts = False
        for key, value in serial_kwargs.items():
            setattr(self, key, value)
        self.port = serial_kwargs.get("port")

    @property
    def dtr(self) -> bool:
        return self._dtr

    @dtr.setter
    def dtr(self, value: bool) -> None:
        self._dtr = value

    @property
    def rts(self) -> bool:
        return self._rts

    @rts.setter
    def rts(self, value: bool) -> None:
        self._rts = value

    def open(self) -> None:
        self.open_calls += 1
        self.is_open = True

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def write(self, data: bytes) -> int:
        self.written.append(data)
        handler = self.script.get(data)
        if handler is not None:
            reply = handler(self) if callable(handler) else handler
            if reply:
                with self._lock:
                    self._buf.extend(reply)
        return len(data)

    def send_break(self, duration: float = 0.4) -> None:
        self.break_calls.append(duration)
        if self.break_script is not None:
            reply = self.break_script(self) if callable(self.break_script) else self.break_script
            if reply:
                with self._lock:
                    self._buf.extend(reply)

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._buf)

    def read(self, size: int = 1) -> bytes:
        with self._lock:
            if self._buf:
                n = min(size, len(self._buf))
                data = bytes(self._buf[:n])
                del self._buf[:n]
                return data
        time.sleep(self._read_delay)
        return b""

    def reset_input_buffer(self) -> None:
        pass

    def flush(self) -> None:
        pass


def make_local_channel(script: dict | None = None, **kwargs) -> tuple[LocalRelayChannel, ScriptedSerial]:
    fake = ScriptedSerial(script=script)
    channel = LocalRelayChannel(
        "/dev/fake-relay0", open_settle=0.0,
        serial_factory=lambda **_kw: fake, **kwargs,
    )
    return channel, fake


def test_local_channel_reset_and_normalize_runs_full_sequence():
    script = normalize_script()
    script[b"!DEFAULTS\n"] = b"# stored config cleared\n"
    channel, fake = make_local_channel(script)
    control = make_control()

    info = control.reset_and_normalize(channel)

    assert info.device_name == "togov"
    assert info.firmware == "1.2.3"
    assert fake.open_calls == 1
    assert fake.is_open is False
    step_commands = [cmd for cmd, _ in NORMALIZE_STEPS]
    assert fake.written == [
        b"HELLO\n", b"!VER?\n", *step_commands, b"?\n", b"!DEFAULTS\n",
    ]


def test_local_channel_closes_and_stops_reading_even_on_failure():
    channel, fake = make_local_channel({})  # HELLO never answered
    control = make_control(hello_attempts=1)

    with pytest.raises(RelayError):
        control.reset_and_normalize(channel)

    assert fake.is_open is False
    assert channel._thread is None  # the read thread was joined, not leaked


def test_local_channel_hello_break_fallback_calls_pyserial_send_break():
    """A board silent until a break is exactly hello()'s documented
    escape hatch -- exercised here through the real adapter so
    ``send_break`` is proven to reach pyserial directly, not just that
    hello() eventually succeeds some other way."""
    script = {b"HELLO\n": lambda f: RADIOBRIDGE_BANNER if f.break_calls else None}
    channel, fake = make_local_channel(script)
    control = make_control(hello_attempts=1)

    channel.open()
    reader = Reader(channel)
    try:
        info = control.hello(channel, reader)
    finally:
        channel.close()

    assert info.device_name == "togov"
    assert fake.break_calls == [control.break_duration]


def test_local_channel_write_nowait_writes_directly_to_pyserial():
    channel, fake = make_local_channel()
    channel.open()
    try:
        channel.write_nowait(b"HELLO\n")
    finally:
        channel.close()
    assert fake.written == [b"HELLO\n"]


def test_local_channel_pending_bytes_and_drain_and_watermarks_are_inert():
    """No internal write queue (write_nowait is synchronous -- see the
    module's own docstring), so pending_bytes is always 0, drain() is a
    no-op, and watermark callbacks are recorded but never fire."""
    channel, _fake = make_local_channel()
    calls: list[str] = []
    channel.set_watermarks(lambda: calls.append("high"), lambda: calls.append("low"))

    assert channel.pending_bytes == 0
    channel.drain(timeout=0.01)
    assert calls == []


def test_local_channel_does_not_subclass_serial_connect_session():
    from mbtools.serial.connect import Session

    assert not issubclass(LocalRelayChannel, Session)


# ---------------------------------------------------------------------------
# RemoteRelayChannel -- FakeRemoteStream, a RemoteStream-shaped fake
# ---------------------------------------------------------------------------

class FakeRemoteStream:
    """A ``RemoteStream``-shaped fake driven by the same command -> reply
    script shape as :class:`ScriptedSerial`, but delivering replies
    through a queue with ``read()``'s real documented contract: blocks
    for exactly one frame, returns ``None`` once ``close()`` has been
    called and nothing else is queued (mirrors ``RemoteStream.read()``'s
    own "CLOSE frame or clean EOF -> None" collapse).
    """

    def __init__(self, *, script: "dict[bytes, Any] | None" = None,
                 break_script: Any = None) -> None:
        self.script = dict(script or {})
        self.break_script = break_script
        self.written: list[bytes] = []
        self.break_calls = 0
        self.closed = False
        self._queue: "queue.Queue[bytes]" = queue.Queue()

    def write(self, data: bytes) -> None:
        self.written.append(data)
        handler = self.script.get(data)
        if handler is None:
            return
        reply = handler(self) if callable(handler) else handler
        if reply:
            self._queue.put(reply)

    def send_break(self) -> None:
        self.break_calls += 1
        if self.break_script is not None:
            reply = self.break_script(self) if callable(self.break_script) else self.break_script
            if reply:
                self._queue.put(reply)

    def read(self) -> "bytes | None":
        while True:
            try:
                return self._queue.get(timeout=0.05)
            except queue.Empty:
                if self.closed:
                    return None

    def close(self) -> None:
        self.closed = True


def test_remote_channel_reset_and_normalize_runs_full_sequence():
    script = normalize_script()
    script[b"!DEFAULTS\n"] = b"# stored config cleared\n"
    stream = FakeRemoteStream(script=script)
    channel = RemoteRelayChannel(stream)
    control = make_control()

    info = control.reset_and_normalize(channel)

    assert info.device_name == "togov"
    assert info.firmware == "1.2.3"
    assert stream.closed is True
    step_commands = [cmd for cmd, _ in NORMALIZE_STEPS]
    assert stream.written == [
        b"HELLO\n", b"!VER?\n", *step_commands, b"?\n", b"!DEFAULTS\n",
    ]


def test_remote_channel_closes_and_stops_reading_even_on_failure():
    stream = FakeRemoteStream()  # HELLO never answered
    channel = RemoteRelayChannel(stream)
    control = make_control(hello_attempts=1)

    with pytest.raises(RelayError):
        control.reset_and_normalize(channel)

    assert stream.closed is True
    assert channel._thread is None  # the read thread was joined, not leaked


def test_remote_channel_open_is_a_noop():
    """The owning host already opened its local port as part of
    establishing the stream (registry.remote_api._handle_stream) -- this
    adapter's open() has nothing left to do."""
    stream = FakeRemoteStream()
    channel = RemoteRelayChannel(stream)

    channel.open()

    assert stream.written == []
    assert stream.closed is False


def test_remote_channel_send_break_calls_through_to_remote_stream():
    """AC: RemoteRelayChannel.send_break() calls through to
    RemoteStream.send_break() with no new wire protocol -- asserted on
    the fake's own call record, not merely "no exception raised"."""
    stream = FakeRemoteStream()
    channel = RemoteRelayChannel(stream)

    channel.send_break(0.4)

    assert stream.break_calls == 1


def test_remote_channel_hello_break_fallback_calls_through_to_remote_stream():
    script = {b"HELLO\n": lambda s: RADIOBRIDGE_BANNER if s.break_calls else None}
    stream = FakeRemoteStream(script=script)
    channel = RemoteRelayChannel(stream)
    control = make_control(hello_attempts=1)

    channel.open()
    reader = Reader(channel)
    try:
        info = control.hello(channel, reader)
    finally:
        channel.close()

    assert info.device_name == "togov"
    assert stream.break_calls == 1


def test_remote_channel_write_nowait_writes_directly_to_remote_stream():
    stream = FakeRemoteStream()
    channel = RemoteRelayChannel(stream)
    channel.open()
    try:
        channel.write_nowait(b"HELLO\n")
    finally:
        channel.close()
    assert stream.written == [b"HELLO\n"]


def test_remote_channel_pending_bytes_and_drain_and_watermarks_are_inert():
    stream = FakeRemoteStream()
    channel = RemoteRelayChannel(stream)
    calls: list[str] = []
    channel.set_watermarks(lambda: calls.append("high"), lambda: calls.append("low"))

    assert channel.pending_bytes == 0
    channel.drain(timeout=0.01)
    assert calls == []


def test_remote_channel_does_not_subclass_remote_session_or_remote_stream():
    from mbtools.registry.remote_client import RemoteStream
    from mbtools.serial.remote_connect import RemoteSession

    assert not issubclass(RemoteRelayChannel, RemoteSession)
    assert not issubclass(RemoteRelayChannel, RemoteStream)
