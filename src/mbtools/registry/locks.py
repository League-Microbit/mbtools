"""mbtools.registry.locks — exclusive lock manager, kind-tagged, holder
identity generalized to local (PID-tied) or remote (session-tied).

Per sprint.md's Architecture (module "locks") and Design Rationale
("locks are in-memory only, never persisted to the SQLite store"), this
is a pure, in-memory table of ``uid -> (holder, kind)`` plus a liveness
sweep -- no I/O, no socket. Holder identity comes in as a
:class:`HolderRef` (ticket 002, sprint.md Decision 2), which names
either a local, PID-tied caller or a remote, session-tied one -- "a PID
means nothing across hosts" (brief open decision #3) is why a remote
holder can't just be another ``int``. Constructing the ``HolderRef`` for
a real local connection is the API module's job (ticket 008: it derives
one from the ``SO_PEERCRED``/``LOCAL_PEERPID`` pid it extracts); ticket
006's ``registry.remote_api`` will construct the remote-origin
equivalent, session-tied rather than PID-tied. That boundary is what
makes this module testable without any real process or socket -- except
for the one integration test in ``tests/registry/locks/test_locks.py``
that deliberately *does* use a real subprocess, to prove the injectable
``is_pid_alive`` seam matches real OS behavior, not just its own fakes.

A lock is exclusive per device uid, regardless of kind or holder origin:
a ``serial`` lock and a ``flash`` lock cannot coexist on the same uid,
and a local and a remote holder cannot both hold a lock on the same uid
at once -- kind is metadata for listings (SUC-005/UC-006's "locked for
flash by pid 4821"), not a separate lock namespace, and origin is
holder-identity metadata, not a second lock table (Decision 2's whole
point: one table, one acquire path, so exclusivity stays provably
correct across both local and remote callers). Release happens two ways
per sprint.md's ASSUMPTION ("the lock releases when the PID dies *or*
the connection closes" -- kept as separate triggers because a leaked fd
across a fork could keep a connection open after its original process
exits, or vice versa): explicit :meth:`LockManager.release` (checked
against the current holder by full :class:`HolderRef` equality, so a
non-holder can't steal or grief a lock), and :meth:`LockManager.sweep`,
a liveness check driven by an injected ``is_alive`` callable. Both are
mechanism only -- wiring ``sweep()`` to a timer or a connection-close
event is ``daemon``'s job (ticket 006) or ``api``'s (ticket 008), not
this module's.

A ``flash``-kind lock's release is also the daemon's re-probe trigger
(per Design Rationale, "a flash-kind lock's release is the re-probe
trigger"): this module exposes a callback, registered at construction,
that fires exactly once whenever a ``flash``-kind lock is released,
naming the uid -- mirroring the ``now_fn`` injectable-seam convention
``store``/``identity``/``usbwatch`` already establish, rather than a
general pub/sub API this ticket doesn't need.

**Lock-display hook (ticket 005)**: a second, independent callback,
``lock_display_callback``, fires on every successful :meth:`acquire` and
every actual (not no-op) :meth:`release`/:meth:`sweep`-driven release,
regardless of ``kind`` -- unlike ``flash_release_callback``, which only
ever fires for ``flash``-kind releases. It carries ``(uid, kind,
display)``: on an acquire, ``kind`` is the newly-held lock's kind and
``display`` is a rendered, human-readable holder description (never the
raw :class:`HolderRef` -- a peer that only needs to *show* "locked by pid
4821" must never be handed something it could replay to actually act on
the lock); on a release, both ``kind`` and ``display`` are ``None``,
signaling "no longer locked" to a cache keyed the same way. This is
where ``registry.peering`` (ticket 005) attaches its PUB-socket
``lock_state`` publish call for ticket 009's assembly to wire up --
mirrors sprint.md Decision 3 ("remote lock display is a replicated
cache, not a live cross-host call"); this module itself never imports or
knows about ``peering``/ZeroMQ.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

__all__ = [
    "LockManager",
    "LockStatus",
    "LockHeldError",
    "HolderRef",
    "KIND_SERIAL",
    "KIND_RELAY",
    "KIND_FLASH",
    "KIND_DEBUG",
    "LOCK_KINDS",
    "format_lock_suffix",
]

# Lock kinds, per sprint.md's brief §3.5/spec §3.6, cross-cutting §3.
KIND_SERIAL = "serial"
KIND_RELAY = "relay"
KIND_FLASH = "flash"
KIND_DEBUG = "debug"

LOCK_KINDS = (KIND_SERIAL, KIND_RELAY, KIND_FLASH, KIND_DEBUG)


@dataclass(frozen=True)
class HolderRef:
    """Identifies a lock holder -- either a local, PID-tied client or a
    remote, session-tied one (ticket 002, sprint.md Decision 2).

    ``origin`` is ``"local"`` or ``"remote"``. A local holder is
    constructed by ``api.py`` from the ``SO_PEERCRED``/``LOCAL_PEERPID``
    pid it reads off the connection: ``HolderRef(origin="local",
    ref=str(pid), pid=pid)`` -- ``ref`` duplicates ``pid`` as a string so
    every origin has a stable, hashable-by-equality identity to match on
    (see :meth:`LockManager.release`), not just the origins that happen
    to have a pid. A remote holder (ticket 006's ``registry.remote_api``,
    not constructed anywhere in this ticket's scope) is constructed as
    ``HolderRef(origin="remote", ref=session_id, host=peer_display_host)``
    -- ``pid`` stays ``None`` since "a PID means nothing across hosts"
    (brief open decision #3).

    Frozen and equality-comparable (the dataclass default) so
    :meth:`LockManager.release` can match a caller-supplied ``HolderRef``
    against the stored one field-for-field, rather than trusting just one
    field the way the pre-ticket-002 bare-``pid`` design did.
    """

    origin: str
    ref: str
    pid: int | None = None
    host: str | None = None


@dataclass(frozen=True)
class LockStatus:
    """A device's current lock holder -- what kind of lock, held by whom.

    Returned by :meth:`LockManager.status` for a locked device, and
    carried by :class:`LockHeldError` on a failed acquire, so a caller
    (the API, ticket 008) can build UC-006's "locked for flash by pid
    4821" message from either path without a second lookup.

    ``holder`` (a :class:`HolderRef`) is the source of truth; ``pid`` is
    a read-only convenience property over ``holder.pid`` (``None`` for a
    remote holder) kept so the local-only call sites that only ever cared
    about a bare pid -- ``daemon.py``, ``api.py``'s wire-protocol dict,
    and every pre-ticket-002 test -- don't all need to learn about
    :class:`HolderRef` just to read it back out.

    ``label``/``since`` (sprint 008, ticket 002) are per-*acquisition*,
    display-only metadata -- not part of :class:`HolderRef`, which is
    frozen, equality-matched by :meth:`LockManager.release`, and reused
    across every lock one connection acquires (see this module's
    docstring and sprint.md's Design Rationale, Decision 1, for why they
    live here instead). ``label`` is the caller-supplied string from
    ``lock``'s optional ``label`` field (``None`` if omitted); ``since``
    is always set, from :class:`LockManager`'s injected ``now_fn``, at
    the moment :meth:`LockManager.acquire` grants the lock.
    """

    kind: str
    holder: HolderRef
    label: str | None = None
    since: float = 0.0

    @property
    def pid(self) -> int | None:
        return self.holder.pid


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

    ``holder`` is unchanged in shape from before ticket 002 -- still a
    :class:`LockStatus`, still exposing ``.kind``/``.pid`` for every
    existing caller -- plus, via that same ``LockStatus``, the new
    ``.holder`` (a :class:`HolderRef`) for a caller that needs the full
    identity (host, origin) rather than just the pid.
    """

    def __init__(self, uid: str, holder: LockStatus) -> None:
        self.uid = uid
        self.holder = holder
        super().__init__(
            f"{uid} already locked for {holder.kind} by {_describe_holder(holder.holder)}"
        )


def _describe_holder(holder: HolderRef) -> str:
    """Human-readable holder description for :class:`LockHeldError`'s
    message -- ``"pid 4821"`` for a local holder (unchanged from
    pre-ticket-002 wording, since that's still the only origin any
    caller constructs today) and ``"session <ref> on <host>"`` for a
    remote one.
    """
    if holder.origin == "local":
        return f"pid {holder.pid}"
    return f"session {holder.ref} on {holder.host}"


def _format_age(seconds: float) -> str:
    """A short, human-scale elapsed-time string -- seconds under a
    minute, minutes under an hour, hours beyond that. Not a general
    duration formatter (no days -- a stale lock this old is already
    ``mbregistry unlock --force`` (ticket 003) territory, not a display
    nicety), just enough resolution for an operator glancing at
    ``mbregistry list`` or a peer's replicated display string.
    """
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    return f"{hours}h"


def format_lock_suffix(label: str | None, since: float | None, *, now: float) -> str:
    """The optional ``" (<label>, <age>)"``/``" (<age>)"`` suffix sprint
    008 ticket 002's Design Rationale (Decision 2) describes for both a
    local lock cell (``registry.render``'s ``_state_cell``) and a peer's
    replicated ``remote_lock_display`` string
    (``registry.peering.publish_lock_event``, which bakes it into
    ``display`` before publishing) -- one shared, pure formatter so the
    two never drift on wording.

    ``now`` is injected rather than read internally (``time.time()``),
    keeping this function pure and independently testable -- each caller
    supplies its own idea of "now" (``render``'s caller-supplied
    timestamp; ``peering``'s own clock at publish time).

    Returns ``""`` (no suffix at all) when ``since`` is ``None`` -- an
    unlocked device, or a caller (a pre-ticket-002 device dict/test
    fixture) that never set it. Once a real acquisition timestamp is
    present, the elapsed age is always shown (e.g. ``" (12m)"``), with
    ``label`` prepended when set (``" (alice-laptop, 12m)"``).
    """
    if since is None:
        return ""
    age = _format_age(now - since)
    if label:
        return f" ({label}, {age})"
    return f" ({age})"


class LockManager:
    """The exclusive per-device lock table -- in-memory only, never
    persisted (see module docstring).

    ``flash_release_callback``, if given, is called with a uid exactly
    once whenever a ``flash``-kind lock on that uid is released (via
    either :meth:`release` or :meth:`sweep`) -- this is where
    ``daemon``'s (ticket 006) flash-triggered re-probe hook attaches.

    ``lock_display_callback``, if given, is called with ``(uid, kind,
    display, label, since)`` (sprint 008, ticket 002 extended the
    original ``(uid, kind, display)`` signature to carry the new
    per-acquisition fields alongside it, rather than adding a second
    callback) on every successful :meth:`acquire` and every actual
    release (any kind, via either :meth:`release` or :meth:`sweep`) --
    see the module docstring's "Lock-display hook" note. ``label``/
    ``since`` are ``None`` both on a release and on an acquire that
    supplied no ``label``/whose ``since`` isn't yet known -- in practice
    ``since`` is always a real timestamp on a successful acquire, never
    ``None`` there. Defaults to ``None`` (no-op) so every pre-ticket-005
    caller/test is unaffected.

    ``now_fn`` (sprint 008, ticket 002) mirrors ``store``/``identity``/
    ``usbwatch``'s existing injectable-clock convention -- called once
    per :meth:`acquire` to stamp that acquisition's ``LockStatus.since``,
    so a test can inject a scripted clock instead of real wall-clock
    time. Defaults to :func:`time.time`.
    """

    def __init__(
        self,
        *,
        flash_release_callback: Callable[[str], None] | None = None,
        lock_display_callback: Callable[
            [str, str | None, str | None, str | None, float | None], None
        ]
        | None = None,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self._locks: dict[str, LockStatus] = {}
        self._flash_release_callback = flash_release_callback
        self._lock_display_callback = lock_display_callback
        self._now_fn = now_fn

    def acquire(
        self, uid: str, kind: str, holder: HolderRef, *, label: str | None = None
    ) -> bool:
        """Grant an exclusive lock of ``kind`` on ``uid`` to ``holder``.

        Returns ``True`` if ``uid`` was unlocked and the lock is now
        held by ``holder``. Raises :class:`LockHeldError` (never returns
        ``False`` -- see the exception's own docstring for why) if
        ``uid`` is already locked by a different holder -- local or
        remote, checked against the single lock table regardless of
        origin (Decision 2: one table is what makes a local and a remote
        request mutually exclusive on the same uid). State is left
        untouched on failure: the existing holder keeps its lock.

        ``label`` (sprint 008, ticket 002), if given, is stored on the
        new :class:`LockStatus` verbatim -- display-only, never used for
        holder identity or the :meth:`release` equality check. ``since``
        is stamped fresh from :attr:`_now_fn` on every call, per
        *acquisition* -- not cached on ``holder``, so two locks acquired
        by the same connection (same ``HolderRef``) get their own,
        independent ``since``.

        Fires ``lock_display_callback(uid, kind, display, label, since)``
        (see the module docstring's "Lock-display hook" note) on
        success, never on a :class:`LockHeldError` failure.
        """
        current = self._locks.get(uid)
        if current is not None:
            raise LockHeldError(uid, current)
        since = self._now_fn()
        self._locks[uid] = LockStatus(kind=kind, holder=holder, label=label, since=since)
        if self._lock_display_callback is not None:
            self._lock_display_callback(uid, kind, _describe_holder(holder), label, since)
        return True

    def release(self, uid: str, holder: HolderRef) -> bool:
        """Release ``uid``'s lock, but only if ``holder`` matches the
        current holder by full :class:`HolderRef` equality.

        A no-op (not an error) if ``uid`` is unlocked or held by a
        different holder -- per the ticket's "not an error that could be
        used to steal a lock" requirement. Returns whether a release
        actually happened. The flash-release callback (when ``uid`` held
        a ``flash``-kind lock) only fires on an actual release, never on
        the no-op path.
        """
        current = self._locks.get(uid)
        if current is None or current.holder != holder:
            return False
        self._release(uid, current)
        return True

    def force_release(self, uid: str) -> LockStatus | None:
        """Release ``uid``'s lock regardless of who holds it (sprint 008,
        ticket 003) -- the manual, operator-only override ``mbregistry
        unlock --force`` needs.

        A separate method, never a bypass parameter on :meth:`release` --
        :meth:`release`'s own holder-equality check is completely
        untouched, so no existing caller of ``release`` can accidentally
        skip it. A no-op (not an error) if ``uid`` is already unlocked --
        returns ``None`` in that case, exactly like :meth:`status`.

        On an actual release, funnels through the same shared
        :meth:`_release` mechanics :meth:`release`/:meth:`sweep` already
        use (the flash-release callback, then the lock-display callback
        with ``(uid, None, None, None, None)``), so a forced release is
        indistinguishable, downstream (a ``watch`` client's ``lock_state``
        event, a peer's replicated display), from an ordinary one. Returns
        the :class:`LockStatus` that was released -- unlike
        :meth:`release`'s bare ``bool`` -- so a caller (``api.py``'s
        ``force_unlock`` op) can report what it broke (kind, holder,
        label, since) without a second lookup.
        """
        current = self._locks.get(uid)
        if current is None:
            return None
        self._release(uid, current)
        return current

    def sweep(self, is_alive: Callable[[HolderRef], bool]) -> list[str]:
        """Release every lock whose holder ``is_alive`` reports as dead;
        leave live-holder locks untouched.

        Returns the uids that were released, for a caller that wants to
        log or react beyond the flash-release callback.
        ``is_alive`` is injected (see module docstring) so this can be
        unit-tested with a fake liveness function and, separately,
        proven against a real one
        (``tests/registry/locks/test_locks.py``'s subprocess integration
        test). It receives the full :class:`HolderRef`, not just a pid,
        so a caller (``api.py``, ticket 008) can dispatch on
        ``holder.origin`` -- today only ``"local"`` holders ever exist,
        so that dispatch is exercised, but the seam itself is
        origin-agnostic ahead of ticket 006's remote liveness path.
        """
        dead_uids = [
            uid for uid, status in self._locks.items() if not is_alive(status.holder)
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
        flash-release callback if ``holder`` was flash-kind, then fire
        the lock-display callback (any kind) with
        ``(uid, None, None, None, None)`` -- "no longer locked" (sprint
        008 ticket 002 widened the trailing pair from ``(None, None)`` to
        ``(None, None, None, None)`` to match :meth:`acquire`'s five-arg
        call).

        Both public release paths (:meth:`release`, :meth:`sweep`) funnel
        through here so neither callback can be fired from one path and
        skipped from the other.
        """
        del self._locks[uid]
        if holder.kind == KIND_FLASH and self._flash_release_callback is not None:
            self._flash_release_callback(uid)
        if self._lock_display_callback is not None:
            self._lock_display_callback(uid, None, None, None, None)
