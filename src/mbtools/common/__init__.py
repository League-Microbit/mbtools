"""Shared DTOs used by more than one mbtools subpackage.

Kept deliberately minimal in sprint 001 — per sprint.md's ticket 001 notes,
this module stays empty except for a genuinely shared type needed to make
the test fakes typecheck cleanly. :class:`PortInfo` is that type: it's the
per-port value sprint.md's architecture describes
``usbwatch.PortWatcher.scan() -> {uid: PortInfo}`` returning, and
``mbtools.testing.fakes.FakeUSBSource`` needs a concrete shape to script
against ahead of ticket 003, which owns the real ``usbwatch`` module and
may extend this type then. The registry wire-protocol DTOs sprint 002
needs will live here too, once that sprint needs them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PortInfo:
    """One USB serial port, shaped like a filtered ``comports()`` entry.

    ``uid`` is the DAPLink unique id pyserial reports as ``serial_number``
    (and which doubles as the pyOCD probe UID); ``port`` is the OS device
    path (``/dev/ttyACM0``, ``/dev/cu.usbmodem141101``); ``vid``/``pid``
    are the USB vendor/product id the port enumerated with — kept here
    (rather than assumed already-filtered) so a scripted snapshot can
    include a non-matching device and prove the filter rejects it.
    """

    uid: str
    port: str
    vid: int
    pid: int
