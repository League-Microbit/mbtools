"""mbtools.registry.virtual -- registered virtual boards (nezsim sprint 015,
ticket 001).

A *registered board* is a board that does not exist on a USB port: some
other process (a simulator) registers it over the local control socket
under its own name and serves it from an endpoint it provides. This module
holds the pure pieces of that feature -- the row-flavour constants and
predicates, name/endpoint validation, and the live-registration table --
with no sqlite, socket or server code, so ``store``, ``daemon``, ``peering``
and the API servers can all depend on it without cycles.

A registered board is an ordinary local ``device`` row (``host`` NULL) whose
``port`` is ``virtual:<name>`` and ``vid_pid`` is ``virtual``;
:func:`is_virtual` is the single predicate every exclusion point uses (the
USB poll diff, peering snapshot/events, the remote TCP API's visibility, the
startup sweep).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any

from mbtools.relay import naming

__all__ = [
    "VIRTUAL_PORT_PREFIX",
    "VIRTUAL_UID_PREFIX",
    "VIRTUAL_VID_PID",
    "VirtualRegistration",
    "VirtualBoards",
    "is_virtual",
    "is_virtual_uid",
    "is_reserved_name",
    "parse_endpoint",
    "validate_registration",
]

VIRTUAL_VID_PID = "virtual"
VIRTUAL_PORT_PREFIX = "virtual:"
VIRTUAL_UID_PREFIX = "sim-"

#: What a registered board's name may look like: a letter first, then
#: letters/digits/hyphen, 2-32 chars. No ``@`` (reserved for ``name@host``
#: lookups), no whitespace, no path characters.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{1,31}$")
_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SERIAL_RE = re.compile(r"^[0-9]{1,20}$")
DEFAULT_ROLE = "NEZHA2"


def is_virtual(record: Any) -> bool:
    """Is ``record`` (a ``DeviceRecord``) a registered virtual board?"""
    return (
        record.host is None
        and record.vid_pid == VIRTUAL_VID_PID
        and isinstance(record.port, str)
        and record.port.startswith(VIRTUAL_PORT_PREFIX)
    )


def is_virtual_uid(uid: str) -> bool:
    """Cheap uid-only form of :func:`is_virtual`, for call sites (lock
    events, flash-release hooks) that only have a uid. Real uids are hex
    DAPLink ids, which can never start with ``sim-``."""
    return isinstance(uid, str) and uid.startswith(VIRTUAL_UID_PREFIX)


def is_reserved_name(name: str) -> bool:
    """Does ``name`` match the real-robot CVCVC pattern (after the
    firmware's trim/lowercase normalisation)?"""
    return naming.NAME_RE.match(naming.normalize(name)) is not None


def parse_endpoint(endpoint: Any) -> tuple[str, int]:
    """Split ``"host:port"`` (``[v6]:port`` accepted) into ``(host, port)``.
    Raises ``ValueError`` for anything else."""
    if not isinstance(endpoint, str) or not endpoint or any(c.isspace() for c in endpoint):
        raise ValueError("'endpoint' must be a 'host:port' string")
    host, sep, port_s = endpoint.rpartition(":")
    if not sep or not host or not port_s.isdigit():
        raise ValueError("'endpoint' must be a 'host:port' string")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ValueError("'endpoint' must be a 'host:port' string")
    port = int(port_s)
    if not 1 <= port <= 65535:
        raise ValueError("'endpoint' port must be 1-65535")
    return host, port


@dataclass
class VirtualRegistration:
    """One live registration. The registering connection is the lifetime
    handle: when it ends, the board detaches."""

    name: str
    uid: str
    endpoint: tuple[str, int]
    role: str
    serial: str
    conn: Any = field(default=None, repr=False)


def validate_registration(req: dict[str, Any]) -> VirtualRegistration:
    """Validate a ``register_board`` request's fields. Raises ``ValueError``
    (message suitable for ``invalid_request``). Name-reservation and
    in-use checks are separate (they have their own error codes)."""
    name = req.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            "'name' must be 2-32 characters: a letter, then letters, digits or '-'"
        )
    endpoint = parse_endpoint(req.get("endpoint"))
    role = req.get("role", DEFAULT_ROLE)
    if not isinstance(role, str) or not _ROLE_RE.match(role):
        raise ValueError("'role' must be a short identifier such as 'NEZHA2'")
    serial = req.get("serial")
    if isinstance(serial, int) and not isinstance(serial, bool) and serial >= 0:
        serial = str(serial)
    if not isinstance(serial, str) or not _SERIAL_RE.match(serial):
        raise ValueError("'serial' must be a decimal string")
    if req.get("simulated") is not True:
        raise ValueError("'simulated' must be true")
    return VirtualRegistration(
        name=name,
        uid=VIRTUAL_UID_PREFIX + name,
        endpoint=endpoint,
        role=role,
        serial=serial,
    )


class VirtualBoards:
    """Thread-safe table of live registrations, keyed by lowercase name.

    Callers (the API server) hold their own shared lock around
    check-then-add sequences; this class's own lock only keeps each method
    atomic.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_name: dict[str, VirtualRegistration] = {}

    def name_in_use(self, name: str) -> bool:
        with self._lock:
            return name.lower() in self._by_name

    def add(self, reg: VirtualRegistration) -> None:
        with self._lock:
            self._by_name[reg.name.lower()] = reg

    def remove(self, reg: VirtualRegistration) -> bool:
        """Remove ``reg`` only if it is still the live entry for its name."""
        with self._lock:
            if self._by_name.get(reg.name.lower()) is reg:
                del self._by_name[reg.name.lower()]
                return True
            return False

    def by_uid(self, uid: str) -> VirtualRegistration | None:
        with self._lock:
            for reg in self._by_name.values():
                if reg.uid == uid:
                    return reg
        return None
