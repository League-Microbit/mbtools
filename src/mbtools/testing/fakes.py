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
from typing import Any, Callable, Deque, Iterable, Sequence

from mbtools.common import DAPLINK_VID_PID, PortInfo

__all__ = [
    "DAPLINK_VID_PID",
    "FakeSerial",
    "FakeUSBSource",
    "FakeChipIdentitySession",
    "fake_chip_identity_session_factory",
    "unavailable_chip_identity_session_factory",
]


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
        self.break_calls: list[float] = []
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

    def read(self, size: int = 1) -> bytes:
        """Pyserial-shaped ``read`` (ticket 009, ``mbtools.serial.connect``'s
        ``interact()``) -- delegates to :meth:`readline`, which already
        returns a whole scripted line (or ``b""``) in one call rather
        than one byte at a time. Good enough for ``interact()``'s pump
        loop, which never depends on a partial read; mirrors the local
        ``FakeSerial`` today's ``mbdeploy``'s own
        ``tests/test_connect.py`` uses for the same purpose.
        """
        return self.readline()

    @property
    def in_waiting(self) -> int:
        """Always ``0`` -- ``interact()``'s pump loop computes
        ``max(1, ser.in_waiting)`` before every :meth:`read`, so a
        constant ``0`` just means "read whatever's there right now, one
        call at a time", same as mbdeploy's own connect-test fake.
        """
        return 0

    def send_break(self, duration: float = 0.25) -> None:
        """Pyserial's own BREAK-condition API
        (``serial.Serial.send_break``). Records ``duration`` in
        :attr:`break_calls` rather than doing anything to a real port, so
        a test can assert ``mbserial --reset`` actually asserted BREAK on
        Linux (ticket 009) instead of silently no-op'ing.
        """
        self.break_calls.append(duration)


# ---------------------------------------------------------------------------
# SWD chip-identity fakes (sprint 007, ticket 003) -- mirrors FakeSerial's
# own "hardware-free by construction" role, but for
# mbtools.registry.identity.read_device_id/read_chip_identity's
# ConnectHelper.session_with_chosen_probe-shaped session_factory instead
# of a serial port. Needed because pyocd is a real, installed dependency
# of this project (unlike an optional import that's simply absent) -- a
# daemon test that reaches the SWD fallback path (a silent probe with no
# cached chip identity yet) without one of these fakes would otherwise
# call into a *real* pyOCD session, exactly what CLAUDE.md's standing
# hardware rule ("no micro:bits on the development Mac -- they lock it
# up") and this ticket's own acceptance criteria ("no real hardware in
# the automated suite") both forbid.
# ---------------------------------------------------------------------------


class FakeChipIdentitySession:
    """A ``ConnectHelper.session_with_chosen_probe``-shaped context
    manager for a *successful* SWD chip-identity read -- yields an object
    whose ``.target.read32(addr)`` returns a scripted ``device_id``,
    mirroring the exact shape ``identity.read_device_id`` calls it
    against: ``with factory(...) as session:
    session.target.read32(FICR_DEVICEID1)``.
    """

    def __init__(self, device_id: int) -> None:
        self._device_id = device_id

    def __enter__(self) -> "FakeChipIdentitySession":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    @property
    def target(self) -> "FakeChipIdentitySession":
        return self

    def read32(self, _addr: int) -> int:
        return self._device_id


def fake_chip_identity_session_factory(
    device_id: int, *, fail_first_n: int = 0
) -> Callable[..., FakeChipIdentitySession]:
    """Build a ``chip_identity_session_factory`` that succeeds with
    ``device_id`` -- optionally raising on the first ``fail_first_n``
    calls before succeeding, so a test can prove
    ``identity.read_device_id``'s own "try ``target_mcu``, then retry
    with ``target_override=None``" fallback actually exercises both
    attempts before succeeding (``fail_first_n=1``), or that a probe
    that never succeeds on either attempt returns ``None``
    (``fail_first_n=2`` -- or just always raise; see
    :func:`unavailable_chip_identity_session_factory`).
    """
    calls = {"n": 0}

    def factory(**_kwargs: object) -> FakeChipIdentitySession:
        calls["n"] += 1
        if calls["n"] <= fail_first_n:
            raise RuntimeError("fake_chip_identity_session_factory: scripted failure")
        return FakeChipIdentitySession(device_id)

    return factory


def unavailable_chip_identity_session_factory(**_kwargs: Any) -> Any:
    """A ``chip_identity_session_factory`` fake guaranteeing a test never
    opens a real pyOCD session, regardless of what happens to be
    installed in the test environment -- ``pyocd`` is a real, declared
    dependency of this project (``pyproject.toml``), not an optional
    import that's simply absent in CI, so leaving
    ``chip_identity_session_factory`` unset in a daemon test that reaches
    the SWD fallback path would otherwise call ``pyocd``'s real
    ``ConnectHelper.session_with_chosen_probe`` for real.

    Raises unconditionally -- ``identity.read_device_id``'s own
    try/except-and-continue loop treats that exactly like a busy or
    locked probe (tries the next ``target_override``, then gives up and
    returns ``None``), so a daemon test that incidentally reaches this
    fallback path (a silent probe with no cached chip identity yet) but
    isn't itself testing SWD naming gets a clean "unavailable" outcome.
    """
    raise RuntimeError(
        "unavailable_chip_identity_session_factory: no real pyOCD session "
        "may be opened in tests -- pass a scripted session_factory instead"
    )
