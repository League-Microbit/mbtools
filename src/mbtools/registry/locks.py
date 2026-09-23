"""mbtools.registry.locks — PID-tied exclusive lock manager, kind-tagged.

Per sprint.md's Architecture (module "locks") and Design Rationale
("locks are in-memory only, never persisted to the SQLite store"), this
is a pure, in-memory table of ``uid -> (holder pid, kind)`` plus a
liveness sweep -- no I/O, no socket. Holder identity comes in as a plain
``pid: int`` parameter; the real ``SO_PEERCRED`` extraction that turns a
socket connection into a PID is the API module's job (ticket 008), which
calls into this module with the PID it extracted. That boundary is what
makes this module testable without any real process or socket -- except
for the one integration test in ``tests/registry/locks/test_locks.py``
that deliberately *does* use a real subprocess, to prove the injectable
``is_pid_alive`` seam matches real OS behavior, not just its own fakes.

A lock is exclusive per device uid, regardless of kind: a ``serial``
lock and a ``flash`` lock cannot coexist on the same uid -- kind is
metadata for listings (SUC-005/UC-006's "locked for flash by pid 4821"),
not a separate lock namespace. Release happens two ways per sprint.md's
ASSUMPTION ("the lock releases when the PID dies *or* the connection
closes" -- kept as separate triggers because a leaked fd across a fork
could keep a connection open after its original process exits, or vice
versa): explicit :meth:`LockManager.release` (checked against the
current holder, so a non-holder can't steal or grief a lock), and
:meth:`LockManager.sweep`, a liveness check driven by an injected
``is_pid_alive`` callable. Both are mechanism only -- wiring ``sweep()``
to a timer or a connection-close event is ``daemon``'s job (ticket 006)
or ``api``'s (ticket 008), not this module's.

A ``flash``-kind lock's release is also the daemon's re-probe trigger
(per Design Rationale, "a flash-kind lock's release is the re-probe
trigger"): this module exposes a callback, registered at construction,
that fires exactly once whenever a ``flash``-kind lock is released,
naming the uid -- mirroring the ``now_fn`` injectable-seam convention
``store``/``identity``/``usbwatch`` already establish, rather than a
general pub/sub API this ticket doesn't need.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = [
    "LockManager",
    "LockStatus",
    "LockHeldError",
    "KIND_SERIAL",
    "KIND_RELAY",
    "KIND_FLASH",
    "KIND_DEBUG",
    "LOCK_KINDS",
]

# Lock kinds, per sprint.md's brief §3.5/spec §3.6, cross-cutting §3.
KIND_SERIAL = "serial"
KIND_RELAY = "relay"
KIND_FLASH = "flash"
KIND_DEBUG = "debug"

LOCK_KINDS = (KIND_SERIAL, KIND_RELAY, KIND_FLASH, KIND_DEBUG)


@dataclass(frozen=True)
class LockStatus:
    """A device's current lock holder -- what kind of lock, held by whom.

    Returned by :meth:`LockManager.status` for a locked device, and
    carried by :class:`LockHeldError` on a failed acquire, so a caller
    (the API, ticket 008) can build UC-006's "locked for flash by pid
    4821" message from either path without a second lookup.
    """

    kind: str
    pid: int


class LockHeldError(Exception):
    """Raised by :meth:`LockManager.acquire` when ``uid`` is already
    locked by a different holder.

    Chosen over a bare ``False`` return (the acceptance criteria's other
    allowed option) because the caller needs the *current* holder's kind
    and pid to build UC-006's refusal message ("locked for flash by pid
    4821") -- a boolean can't carry that, and a bare ``False`` would
    force a second :meth:`LockManager.status` call that could race the
    first. :meth:`LockManager.acquire` uses this exception consistently:
    it never returns ``False``, only ``True`` on success or this
    exception on conflict.
    """

    def __init__(self, uid: str, holder: LockStatus) -> None:
        self.uid = uid
        self.holder = holder
        super().__init__(
            f"{uid} already locked for {holder.kind} by pid {holder.pid}"
        )


class LockManager:
    """The exclusive per-device lock table -- in-memory only, never
    persisted (see module docstring).

    ``flash_release_callback``, if given, is called with a uid exactly
    once whenever a ``flash``-kind lock on that uid is released (via
    either :meth:`release` or :meth:`sweep`) -- this is where
    ``daemon``'s (ticket 006) flash-triggered re-probe hook attaches.
    """

    def __init__(
        self,
        *,
        flash_release_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._locks: dict[str, LockStatus] = {}
        self._flash_release_callback = flash_release_callback

    def acquire(self, uid: str, kind: str, pid: int) -> bool:
        """Grant an exclusive lock of ``kind`` on ``uid`` to ``pid``.

        Returns ``True`` if ``uid`` was unlocked and the lock is now
        held by ``pid``. Raises :class:`LockHeldError` (never returns
        ``False`` -- see the exception's own docstring for why) if
        ``uid`` is already locked by a different holder. State is left
        untouched on failure: the existing holder keeps its lock.
        """
        current = self._locks.get(uid)
        if current is not None:
            raise LockHeldError(uid, current)
        self._locks[uid] = LockStatus(kind=kind, pid=pid)
        return True

    def release(self, uid: str, pid: int) -> bool:
        """Release ``uid``'s lock, but only if ``pid`` matches the
        current holder.

        A no-op (not an error) if ``uid`` is unlocked or held by a
        different pid -- per the ticket's "not an error that could be
        used to steal a lock" requirement. Returns whether a release
        actually happened. The flash-release callback (when ``uid`` held
        a ``flash``-kind lock) only fires on an actual release, never on
        the no-op path.
        """
        current = self._locks.get(uid)
        if current is None or current.pid != pid:
            return False
        self._release(uid, current)
        return True

    def sweep(self, is_pid_alive: Callable[[int], bool]) -> list[str]:
        """Release every lock whose holder pid ``is_pid_alive`` reports
        as dead; leave live-holder locks untouched.

        Returns the uids that were released, for a caller that wants to
        log or react beyond the flash-release callback.
        ``is_pid_alive`` is injected (see module docstring) so this can
        be unit-tested with a fake liveness function and, separately,
        proven against a real one
        (``tests/registry/locks/test_locks.py``'s subprocess integration
        test).
        """
        dead_uids = [
            uid for uid, holder in self._locks.items() if not is_pid_alive(holder.pid)
        ]
        for uid in dead_uids:
            self._release(uid, self._locks[uid])
        return dead_uids

    def status(self, uid: str) -> LockStatus | None:
        """The current holder of ``uid``'s lock, or ``None`` if
        unlocked.

        ``None`` is returned identically whether ``uid`` was never
        locked or was locked and later fully released -- no "never
        touched" vs "released" distinction leaks out (the ticket's own
        idempotence requirement), since both states are simply "no entry
        in the table".
        """
        return self._locks.get(uid)

    def _release(self, uid: str, holder: LockStatus) -> None:
        """Shared release mechanics: drop the table entry, then fire the
        flash-release callback if ``holder`` was flash-kind.

        Both public release paths (:meth:`release`, :meth:`sweep`) funnel
        through here so the callback can't be fired from one path and
        skipped from the other.
        """
        del self._locks[uid]
        if holder.kind == KIND_FLASH and self._flash_release_callback is not None:
            self._flash_release_callback(uid)
