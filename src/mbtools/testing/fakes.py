"""Hardware-free test fakes for the USB-watch and identity-probe paths.

Built in ticket 001 and reused by every later ticket in sprint 001 (and by
sprint 002+'s client-tool tests) per sprint.md's Test Strategy — shipping
these as an importable module, rather than sprint-local fixtures, is what
lets ticket 003 (``usbwatch``) and ticket 002 (``identity``) write tests
against a scripted USB/serial world without touching real hardware, and
avoids every later sprint reinventing the same fakes.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Iterable, Sequence

from mbtools.common import DAPLINK_VID_PID, PortInfo

__all__ = ["DAPLINK_VID_PID", "FakeSerial", "FakeUSBSource"]


class FakeUSBSource:
    """Scripts a sequence of ``comports()``-shaped USB snapshots.

    Each call to :meth:`scan` pops the next scripted snapshot — a sequence
    of :class:`~mbtools.common.PortInfo`, unfiltered, exactly like a raw
    ``comports()`` result — and returns it filtered down to the DAPLink
    VID:PID, keyed by uid: the ``{uid: PortInfo}`` shape ticket 003's
    ``PortWatcher.scan()`` will return. Scripting a snapshot that includes
    a non-matching device lets a test prove the filter actually rejects
    it, not just that it passes through a pre-filtered list.

    Once the scripted sequence is exhausted, further calls keep returning
    the last snapshot (mirrors a real port list staying steady when
    nothing attaches/detaches) unless ``exhaust_raises`` is set, which
    raises :class:`IndexError` instead — useful for a test asserting it
    never calls ``scan()`` more times than it scripted.
    """

    def __init__(
        self,
        snapshots: Sequence[Iterable[PortInfo]] = (),
        *,
        exhaust_raises: bool = False,
    ) -> None:
        self._snapshots: Deque[tuple[PortInfo, ...]] = deque(
            tuple(snapshot) for snapshot in snapshots
        )
        self._exhaust_raises = exhaust_raises
        self._last: tuple[PortInfo, ...] = ()
        self.call_count = 0

    def scan(self) -> dict[str, PortInfo]:
        """Return the next scripted snapshot, filtered to DAPLink VID:PID."""
        self.call_count += 1
        if self._snapshots:
            self._last = self._snapshots.popleft()
        elif self._exhaust_raises:
            raise IndexError(
                "FakeUSBSource: scripted snapshot sequence exhausted"
            )
        return {
            port.uid: port
            for port in self._last
            if (port.vid, port.pid) == DAPLINK_VID_PID
        }


class FakeSerial:
    """A pyserial ``Serial``-shaped fake for the identity probe (ticket 002).

    Mirrors the construction/use pattern ``mbdeploy``'s ``probe_type()``
    uses — ``serial.Serial(baudrate=..., timeout=..., dsrdtr=..., rtscts=...)``,
    then ``.port = port``, ``.dtr = False``, ``.rts = False``, ``.open()``,
    ``.reset_input_buffer()``, ``.write(...)``, repeated ``.readline()``,
    ``.close()`` — so a probe implementation exercising that exact call
    sequence can run against this fake unmodified. Any keyword accepted by
    the real ``serial.Serial`` constructor (``port``, ``baudrate``,
    ``timeout``, ``dsrdtr``, ``rtscts``, ...) is accepted here too and
    just recorded as an attribute.

    Scripts exactly one of three outcomes:

    - ``announcement="..."`` — once at least ``announcement_after_writes``
      ``write()`` calls have been made (default ``0``, i.e. no ``write()``
      required — matches the pre-ticket-004 behavior of answering on the
      very first ``readline()``), the next ``readline()`` returns this
      line (newline-terminated, utf-8 encoded), exactly as either
      announcement dialect would arrive; every ``readline()`` after that
      returns ``b""`` (silence), matching a real port that has said its
      one line and gone quiet. Passing ``announcement_after_writes=2``
      scripts a board that stays silent through an earlier ``write()``
      (e.g. a first ``HELLO``) and only answers a later one — used by
      ``identity``'s ticket 004 bounded-retry tests to prove a second
      ``HELLO`` was actually sent before the board answers.
    - neither ``announcement`` nor ``busy`` given — silence: every
      ``readline()`` call returns ``b""``, simulating a real ``Serial``
      timing out with nothing to read (a probe should treat this as "no
      firmware" / timed out, per SUC-001's error flow).
    - ``busy=True`` — ``open()`` itself raises ``OSError``, simulating a
      port already held by another process.
    """

    def __init__(
        self,
        *,
        announcement: str | None = None,
        busy: bool = False,
        announcement_after_writes: int = 0,
        **serial_kwargs: object,
    ) -> None:
        if announcement is not None and busy:
            raise ValueError(
                "FakeSerial: cannot script both an announcement and busy=True"
            )
        self._announcement = announcement
        self._busy = busy
        self._announcement_after_writes = announcement_after_writes
        self._announcement_sent = False

        # Recorded, pyserial-shaped state.
        self.is_open = False
        self._dtr = False
        self._rts = False
        self.written: list[bytes] = []
        self.open_calls = 0
        self.close_calls = 0
        self.reset_input_buffer_calls = 0
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
        if self._busy:
            raise OSError("FakeSerial: port busy (scripted)")
        self.is_open = True

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False

    def reset_input_buffer(self) -> None:
        self.reset_input_buffer_calls += 1

    def write(self, data: bytes) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        pass

    def readline(self) -> bytes:
        if (
            self._announcement is not None
            and not self._announcement_sent
            and len(self.written) >= self._announcement_after_writes
        ):
            self._announcement_sent = True
            line = self._announcement
            if not line.endswith("\n"):
                line += "\n"
            return line.encode("utf-8")
        return b""
