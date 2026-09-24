"""``ByteChannel`` adapters that let ticket 003's ``relay.protocol`` run
against a registry-obtained device connection (sprint 004, ticket 004).

``relay.protocol`` knows nothing about ``mbregistry`` -- it only knows the
``ByteChannel`` Protocol (that ticket's own Implementation Notes: sync, not
asyncio; ``reset_and_normalize(channel)``/``probe(channel)`` take an
already-constructed ``ByteChannel`` directly, no ``(factory, port)`` pair).
This module is the only new code sprint 004 needs for that portability
(architecture Decision 3): two small adapters, one per transport, each
already knowing what it opens -- a local port path, or a registry-obtained
remote stream -- so there is no separate factory step to fit in.

No new wire protocol is needed (Decision 4): reset always goes through a
real BREAK (:meth:`LocalRelayChannel.send_break`/
:meth:`RemoteRelayChannel.send_break`), never through ``open()`` toggling
DTR -- ``relay.protocol.RelayControl.hello``'s own docstring is why: on
Linux, closing and reopening a port does not reset a DAPLink target,
measured on Ubuntu 24.04, so ``mbtools.serial.connect.open_no_reboot``
(DTR/RTS held low, no reboot) is exactly the right way for
:meth:`LocalRelayChannel.open` to open the port, and
:meth:`RemoteRelayChannel.open` is a no-op for the same reason: by the
time a caller has a ``RemoteStream`` to wrap, ``RemoteAPIServer.
_handle_stream`` has already opened the owning host's local port the same
``open_no_reboot`` way, server-side. Both adapters lean on
``RelayControl.hello``'s own BREAK-fallback path for the actual reset --
``LocalRelayChannel.send_break`` calls pyserial's ``send_break`` directly;
``RemoteRelayChannel.send_break`` calls through to the existing
``RemoteStream.send_break()`` (a ``BREAK`` frame the wire protocol already
has), never inventing a new frame type or op.

**Composition, not inheritance** (architecture "Impact on Existing
Components": parallel, not shared): neither adapter subclasses or
modifies ``serial.connect.Session`` or ``serial.remote_connect.
RemoteSession`` -- ``LocalRelayChannel`` opens its own pyserial object via
the same public ``open_no_reboot`` helper ``serial.connect.connect`` and
``registry.remote_api``'s own ``_handle_stream`` both call, and
``RemoteRelayChannel`` wraps a ``RemoteStream`` the caller already
obtained (``RemoteRegistryClient.lock(uid, "relay")`` then
``.open_stream(uid)``, on one connection -- the caller's job, not this
module's; see ``registry.remote_client``'s own "Session model" docstring
for why the lock and the stream have to share one connection). Both
``serial.connect.Session`` and ``serial.remote_connect.RemoteSession`` are
``read``/``write``-shaped wrappers for an interactive console
(``mbserial``); these two classes are ``ByteChannel``-shaped instead
(``start_reading(on_data, on_error)`` push callbacks, not pull
``read()``), a genuinely different interface, which is exactly why they
are new, parallel classes rather than a shared base.

**Background read thread, not an event loop** (ticket 003's own
concurrency-model note: nothing in ``mbtools`` uses asyncio). Each adapter
runs one daemon thread that blocks in the transport's own blocking read
primitive and forwards whatever arrives to ``on_data`` -- the same
"continuously pull, forward every arrival" shape
``serial.remote_connect.RemoteSession._pump`` already uses for
``RemoteStream.read()``'s "blocks for exactly one frame, no per-call
timeout of its own" behavior, adapted here to a push callback instead of
that class's own buffer/``Condition``, since ``ByteChannel`` callers
(``relay.protocol.Reader``) already do their own buffering and waiting.
``LocalRelayChannel``'s thread instead blocks in ``ser.read(max(1,
ser.in_waiting))`` -- the exact pattern ``serial.connect.interact``'s own
pump thread uses -- which naturally wakes on the port's own configured
read timeout (``open_no_reboot``'s ``READ_TIMEOUT``) rather than needing
an explicit stop-unblock step the way the remote side does.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from mbtools.registry.remote_client import RemoteStream
from mbtools.relay.protocol import OnData, OnError
from mbtools.serial.connect import BAUD_RATE, OPEN_SETTLE, ConnectError, open_no_reboot

try:  # pyserial is a declared dependency, but keep this importable without
    # a real port available -- mirrors serial.connect's own optional import.
    import serial as _pyserial  # type: ignore
except Exception:  # pragma: no cover
    _pyserial = None  # type: ignore

__all__ = ["LocalRelayChannel", "RemoteRelayChannel"]


class LocalRelayChannel:
    """A ``ByteChannel`` over a locally-attached relay's serial port.

    Constructed already knowing what to open -- ``port`` -- so
    ``relay.protocol.RelayControl.reset_and_normalize``/``probe`` can take
    it directly, no ``ChannelFactory`` step. The caller is expected to
    have already taken the ``relay``-kind lock on this device (a new lock
    kind, distinct from ``serial.connect``'s own ``"serial"`` -- a
    separate concern this module does not itself enforce, same as
    ``serial.connect.open_no_reboot`` not enforcing its own callers'
    locks) before constructing this adapter.

    :meth:`open` opens the port with DTR/RTS held low (module docstring --
    no reboot on open; reset is always a deliberate
    :meth:`send_break`), via the same public
    ``mbtools.serial.connect.open_no_reboot`` helper
    ``serial.connect.connect``/``registry.remote_api._handle_stream`` both
    call, so there is exactly one place that ever touches the four
    pyserial calls a no-reboot open takes.
    """

    def __init__(
        self,
        port: str,
        *,
        baud: int = BAUD_RATE,
        open_settle: float = OPEN_SETTLE,
        serial_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._port = port
        self._baud = baud
        self._open_settle = open_settle
        self._serial_factory = serial_factory
        self._ser: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_data: OnData = lambda _data: None
        self._on_error: OnError = lambda _exc: None
        self._on_high: Callable[[], None] = lambda: None
        self._on_low: Callable[[], None] = lambda: None

    # -- lifecycle -----------------------------------------------------

    def open(self) -> None:
        """Open the port with DTR/RTS held low. Does NOT reset the board
        (module docstring) -- reset is :meth:`send_break`'s job, via
        ``relay.protocol.RelayControl.hello``'s own BREAK-fallback path.
        """
        factory = self._serial_factory
        if factory is None:
            if _pyserial is None:  # pragma: no cover - pyserial is a dependency
                raise ConnectError(
                    "pyserial is not installed, so no serial port can be opened."
                )
            factory = _pyserial.Serial
        self._ser = open_no_reboot(factory, self._port, self._baud, self._open_settle)

    def close(self) -> None:
        self.stop_reading()
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    # -- reading ---------------------------------------------------------

    def start_reading(self, on_data: OnData, on_error: OnError) -> None:
        self._on_data, self._on_error = on_data, on_error
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._pump, name="mbtools-relay-local-read", daemon=True
        )
        self._thread.start()

    def stop_reading(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _pump(self) -> None:
        """Block in ``ser.read(max(1, ser.in_waiting))`` -- ``serial.
        connect.interact``'s own pump pattern -- until ``stop_reading``
        sets the stop flag. A real pyserial port unblocks on its own
        configured read timeout, so this needs no explicit unblock step
        the way :class:`RemoteRelayChannel`'s equivalent thread does.
        """
        while not self._stop.is_set():
            try:
                data = self._ser.read(max(1, self._ser.in_waiting))
            except Exception as exc:
                if not self._stop.is_set():
                    self._on_error(exc)
                return
            if data:
                self._on_data(data)

    # -- writing -----------------------------------------------------------

    def write_nowait(self, data: bytes) -> None:
        """Write ``data`` to the port directly.

        A plain, blocking pyserial ``write`` rather than a true non-
        blocking queue (unlike the legacy asyncio ``SerialChannel``'s own
        ``write_nowait``, which buffered and drained via
        ``loop.add_writer``): every command ``relay.protocol`` sends is a
        short line with generous timeouts built around it, and there is
        no event loop here for a write to block. See :attr:`pending_bytes`.
        """
        self._ser.write(data)

    def send_break(self, duration: float = 0.4) -> None:
        """Assert a BREAK directly via pyserial -- the actual reset
        mechanism (module docstring), not ``open()``'s DTR toggle."""
        self._ser.send_break(duration)

    def drain(self, timeout: float = 2.0) -> None:
        """No-op: :meth:`write_nowait` already writes synchronously, so
        there is never anything queued left to wait out."""

    @property
    def pending_bytes(self) -> int:
        return 0

    def set_watermarks(
        self, on_high: Callable[[], None], on_low: Callable[[], None]
    ) -> None:
        """Recorded for ``ByteChannel`` parity but never fired -- with no
        internal write queue (:attr:`pending_bytes` is always ``0``),
        there is no high/low watermark to cross."""
        self._on_high, self._on_low = on_high, on_low


class RemoteRelayChannel:
    """A ``ByteChannel`` over a relay reached through ``mbregistry``'s
    remote stream sub-protocol.

    Wraps a :class:`~mbtools.registry.remote_client.RemoteStream` the
    caller already obtained -- ``RemoteRegistryClient.lock(uid, "relay")``
    then ``.open_stream(uid)``, on one connection (module docstring; see
    ``registry.remote_client``'s own "Session model" docstring for why
    those two calls must share a connection). By the time this class
    exists, the owning host has already opened its local port
    (``registry.remote_api.RemoteAPIServer._handle_stream``, the same
    ``open_no_reboot`` no-reboot way :class:`LocalRelayChannel` opens its
    own port) -- so :meth:`open` is a no-op.
    """

    def __init__(self, stream: RemoteStream) -> None:
        self._stream = stream
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._on_data: OnData = lambda _data: None
        self._on_error: OnError = lambda _exc: None
        self._on_high: Callable[[], None] = lambda: None
        self._on_low: Callable[[], None] = lambda: None

    # -- lifecycle -----------------------------------------------------

    def open(self) -> None:
        """No-op: the owning host already opened its local port,
        no-reboot, as part of establishing ``self._stream`` (class
        docstring) -- there is nothing left for this call to do."""

    def close(self) -> None:
        self.stop_reading()

    # -- reading ---------------------------------------------------------

    def start_reading(self, on_data: OnData, on_error: OnError) -> None:
        self._on_data, self._on_error = on_data, on_error
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._pump, name="mbtools-relay-remote-read", daemon=True
        )
        self._thread.start()

    def stop_reading(self) -> None:
        """Stop the background read thread.

        A thread parked in ``RemoteStream.read()`` is blocked in a socket
        read with no per-call timeout of its own (unlike
        :class:`LocalRelayChannel`'s pyserial port), so the only way to
        unblock it is to close the stream -- best-effort and idempotent,
        exactly like ``RemoteStream.close()``'s own contract, and the same
        "close the transport to unstick a blocked reader" move
        ``serial.remote_connect.RemoteSession.close`` already makes for
        the same underlying blocking call.
        """
        self._stop.set()
        try:
            self._stream.close()
        except Exception:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._stream.read()
            except Exception as exc:
                if not self._stop.is_set():
                    self._on_error(exc)
                return
            if self._stop.is_set():
                # stop_reading()'s own close() unblocked this read -- an
                # intentional teardown, not a failure worth reporting.
                return
            if payload is None:
                self._on_error(None)
                return
            if payload:
                self._on_data(payload)

    # -- writing -----------------------------------------------------------

    def write_nowait(self, data: bytes) -> None:
        """Write ``data`` as one ``DATA`` frame -- ``RemoteStream.write``
        already ``sendall()``s it in full, so this is as synchronous as
        :class:`LocalRelayChannel.write_nowait`; see that method's own
        docstring for why a true non-blocking queue is unneeded here."""
        self._stream.write(data)

    def send_break(self, duration: float = 0.4) -> None:
        """Call through to ``RemoteStream.send_break()`` -- the existing
        ``BREAK`` frame the wire protocol already has (module docstring;
        architecture Decision 4: no new wire protocol). ``duration`` is
        accepted for ``ByteChannel`` parity but has no effect: a ``BREAK``
        frame carries no duration of its own -- ``RemoteAPIServer.
        _handle_stream`` asserts it server-side for its own configured
        ``break_duration_s``, the same way every other remote session's
        reset already works.
        """
        self._stream.send_break()

    def drain(self, timeout: float = 2.0) -> None:
        """No-op -- see :meth:`LocalRelayChannel.drain`."""

    @property
    def pending_bytes(self) -> int:
        return 0

    def set_watermarks(
        self, on_high: Callable[[], None], on_low: Callable[[], None]
    ) -> None:
        """Recorded for ``ByteChannel`` parity but never fired -- see
        :meth:`LocalRelayChannel.set_watermarks`."""
        self._on_high, self._on_low = on_high, on_low
