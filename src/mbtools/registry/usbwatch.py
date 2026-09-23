"""mbtools.registry.usbwatch — USB attach/detach detection for
micro:bit-shaped devices.

Per sprint.md's Architecture (module "usbwatch") and Design Rationale
("usbwatch ships only a polling implementation in sprint 001, behind an
interface"), this module defines the narrow :class:`PortWatcher`
interface and ships exactly one implementation, :class:`PollingPortWatcher`
(wrapping pyserial's ``comports()``), so a future udev/netlink source can
be added later without touching any caller.

``PortWatcher.scan()`` returns the *current* set of matching devices —
it does not diff against a previous scan or emit events itself. That's
``mbtools.registry.daemon``'s job (ticket 006): it calls ``scan()`` on an
interval and diffs the result against what it already knows. This keeps
``usbwatch`` a pure, stateless snapshot source, easy to fake
(``mbtools.testing.fakes.FakeUSBSource`` implements the same interface)
and easy to test without any timing assumptions.

Ported from ``mbdeploy/src/mbdeploy/devices.py``'s ``port_serial_map``.
``PollingPortWatcher`` is plain ``comports()`` — no platform-specific
code — so it runs unmodified on macOS too, which is what lets
``mbregistry run`` work as a macOS dev convenience without this module
doing anything macOS-specific.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, Protocol, runtime_checkable

try:  # pyserial is a declared dependency, but keep this importable without
    # it (mirrors devices.py's own optional import).
    import serial.tools.list_ports as _list_ports  # type: ignore
except Exception:  # pragma: no cover
    _list_ports = None  # type: ignore

from mbtools.common import DAPLINK_VID_PID, PortInfo

logger = logging.getLogger(__name__)

__all__ = ["PortWatcher", "PollingPortWatcher"]


@runtime_checkable
class PortWatcher(Protocol):
    """The one interface every USB attach/detach source implements.

    Deliberately narrow: a snapshot source, not an event source. A
    caller (``daemon``, ticket 006) calls :meth:`scan` on an interval and
    diffs the result against what it already knows — ``usbwatch`` itself
    never tracks previous state or fires callbacks. That's what lets
    ``mbtools.testing.fakes.FakeUSBSource`` satisfy this same interface
    with a scripted sequence of snapshots instead of any real polling.
    """

    def scan(self) -> dict[str, PortInfo]:
        """Return the *current* set of matching devices, keyed by uid.

        Never raises for "nothing attached" — an empty result is a
        (possibly empty) ``dict``, never ``None`` and never an
        exception.
        """
        ...


class PollingPortWatcher:
    """:class:`PortWatcher` implementation backed by pyserial's ``comports()``.

    Filters to the DAPLink VID:PID (:data:`mbtools.common.DAPLINK_VID_PID`)
    and keys the result by the DAPLink unique id (pyserial's
    ``serial_number``, which doubles as the pyOCD probe UID) — mirroring
    ``mbdeploy``'s ``port_serial_map``.

    ``comports_fn`` is a test-only escape hatch, mirroring
    ``mbtools.registry.identity.probe``'s ``serial_factory`` parameter: a
    test passes a callable returning a scripted, pyserial-``ListPortInfo``
    -shaped iterable (each entry exposing ``.vid``, ``.pid``,
    ``.serial_number``, ``.device``) so no test needs to touch a real USB
    bus. Production code leaves it at its default
    (``serial.tools.list_ports.comports``).
    """

    def __init__(
        self, comports_fn: Callable[[], Iterable[object]] | None = None
    ) -> None:
        self._comports_fn = comports_fn

    def scan(self) -> dict[str, PortInfo]:
        """Return the current DAPLink-matching ports, keyed by uid.

        A raw ``comports()`` entry with no ``serial_number`` (not a real
        possibility for a DAPLink interface, but defensive against a
        malformed/mocked entry) is skipped rather than raising, per the
        "no test touches real hardware, but the code shouldn't crash on
        an odd result either" spirit of the interface. The first port
        seen for a given uid wins, mirroring ``port_serial_map``'s
        ``setdefault``.
        """
        comports_fn = self._comports_fn
        if comports_fn is None:
            if _list_ports is None:  # pragma: no cover - pyserial is a dependency
                return {}
            comports_fn = _list_ports.comports

        out: dict[str, PortInfo] = {}
        for port in comports_fn():
            vid = getattr(port, "vid", None)
            pid = getattr(port, "pid", None)
            if (vid, pid) != DAPLINK_VID_PID:
                continue
            uid = getattr(port, "serial_number", None)
            if uid is None:
                continue
            device = getattr(port, "device", None)
            out.setdefault(uid, PortInfo(uid=uid, port=device, vid=vid, pid=pid))
        return out
