"""mbtools.registry.claims — let one ``mbregistry`` process at a time treat
a given board UID as its own, on one host.

Per sprint.md's Architecture (Step 3, module "registry.claims"), this is a
brand-new module: nothing else in this package owns "is some other
*process* already touching this board" -- ``registry.locks``' own
``LockManager`` is a same-*process*-owned, cross-*peer*, per-connection
concept (a client session holding a uid), entirely orthogonal to two
*independent OS processes* racing to treat the same physically-attached
board as their own. See sprint.md's Design Rationale ("the cross-instance
claim is a new module, not folded into registry.locks or
registry.usbwatch") for why the two are kept apart.

**Unix**: :func:`try_claim` opens (creating if needed)
``<claims_dir>/<uid>.lock`` and takes a non-blocking exclusive ``flock`` on
it. ``flock`` locks are held by an *open file description*, not a
process, so the OS itself releases the lock the moment every fd
referencing that open file description is closed -- including when a
holding process is killed (``SIGKILL``) without ever running its own
cleanup code. That is the whole mechanism behind this ticket's "a
crashed/killed holder's claim becomes available to another claimant with
no manual cleanup" acceptance criterion: there *is* no manual cleanup,
because the kernel already did it.

**Windows**: :func:`try_claim` is an explicit, always-succeeding no-op
(see its own docstring) -- COM-port opens are already exclusive at the OS
level on Windows, so a second process attempting to open the same port
fails there, with no new mechanism needed from this module. This is
stated explicitly, not left as a silent "not implemented on this
platform" gap, per this ticket's own acceptance criterion.

**``TIOCEXCL`` (belt-and-suspenders, Unix only)**: the ``flock`` above
answers "is another *mbregistry instance* already claiming this uid" --
it says nothing about some *other, unrelated* process (a stray
``pyocd``/``screen``/``minicom`` a developer left open, say) opening the
same serial port underneath a claim holder. :func:`protect_fd` applies
``TIOCEXCL`` (via ``fcntl.ioctl``) to an already-open tty file descriptor,
which asks the kernel to refuse any *other* ``open()`` of that same
device node for as long as this fd stays open. It is a separate, optional
step from claiming the uid itself -- a caller opens the actual serial
port (via :mod:`mbtools.registry.identity`, elsewhere), then calls this
function against that port's fd. Wiring this into the daemon's actual
probe/SWD call sites is left to ticket 003 (the ticket that adds the
second, SWD transport this same claim is meant to guard) -- this ticket
only needs the capability to exist, tested in isolation, per its own
"build registry.claims and its tests standalone first" Implementation
Plan note.

**Claim lifetime / release**: :meth:`ClaimHandle.release` (also reachable
as a plain :func:`release` function, and via the context-manager protocol)
closes the held fd, which is what actually drops the ``flock``. A caller
that never releases explicitly (a crash, or -- deliberately, in
production -- letting the OS reclaim it at process exit) is exactly the
"OS releases it" alternate path this module's docstring and ticket both
call out as an equally valid design; :mod:`mbtools.registry.daemon`
(ticket 002's own wiring) chooses to release explicitly on detach, for
symmetry with its own attach/detach bookkeeping, but nothing here
requires that choice.

**``--only-uid``/``--exclude-uid`` (ticket 002)**: :func:`build_claim_fn`
builds a small ``uid -> ClaimHandle | None`` closure that filters a uid
against an operator's explicit allow/deny list *before* ever calling
:func:`try_claim` -- an excluded uid, or a uid not present in a non-empty
``--only-uid`` set, never reaches the real flock/``TIOCEXCL`` mechanism at
all, decided locally rather than by contention. This is what
:mod:`mbtools.registry.cli` wires into
:func:`mbtools.registry.daemon.Daemon`'s own injectable ``claim_fn``
parameter for a real ``mbregistry run``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from mbtools.registry.paths import claims_dir_path

try:  # Unix only -- absent on Windows, guarded the same way identity.py
    # guards its own optional `serial` import.
    import fcntl
except ImportError:  # pragma: no cover -- exercised only on real Windows
    fcntl = None  # type: ignore[assignment]

try:
    import termios
except ImportError:  # pragma: no cover -- exercised only on real Windows
    termios = None  # type: ignore[assignment]

__all__ = [
    "ClaimHandle",
    "try_claim",
    "release",
    "protect_fd",
    "is_claimable",
    "build_claim_fn",
]


@dataclass
class ClaimHandle:
    """A held claim on ``uid``. Falsy-safe to hold onto directly (there is
    no ``__bool__`` override -- a caller must check ``is None`` on the
    *return value of* :func:`try_claim`, never on a handle it already
    has), closed exactly once even if :meth:`release` is called more than
    once (a second call is a silent no-op, matching ``os.close``-on-an-
    already-closed-fd being the one thing this class guards against by
    tracking its own closed state rather than calling ``os.close`` twice).

    ``_fd`` is ``None`` on the Windows no-op path (there is no real file
    descriptor backing that claim at all) and a real Unix file descriptor
    otherwise, kept open for exactly as long as the claim is held -- the
    open file description is what the kernel's own ``flock`` release
    (on close, including on process-crash) is keyed to.
    """

    uid: str
    _fd: int | None = None

    def release(self) -> None:
        """Drop this claim. Closes the held fd (releasing the ``flock``)
        on Unix; a no-op on the Windows path, which never opened one.
        Safe to call more than once.
        """
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "ClaimHandle":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def try_claim(uid: str, *, claims_dir: Path | None = None) -> ClaimHandle | None:
    """Attempt to claim ``uid`` for this process. Returns a
    :class:`ClaimHandle` on success, ``None`` if another holder already
    has it.

    **Windows** (``sys.platform == "win32"``): always succeeds, no
    filesystem touched at all -- COM-port exclusivity is already native
    (see the module docstring). Checked first, before any directory
    access, so a Windows caller never depends on ``claims_dir``/
    :func:`mbtools.registry.paths.claims_dir_path` existing or being
    writable.

    **Unix**: opens (creating the claims directory and/or lock file if
    either is missing) ``<claims_dir>/<uid>.lock`` and takes a
    non-blocking exclusive ``flock``. ``claims_dir`` defaults to
    :func:`mbtools.registry.paths.claims_dir_path` -- a test passes its
    own ``tmp_path``-backed directory instead, both to avoid touching the
    real host-shared location and to get a fresh, empty directory per
    test.
    """
    if sys.platform == "win32":
        return ClaimHandle(uid=uid, _fd=None)

    directory = claims_dir if claims_dir is not None else claims_dir_path()
    directory.mkdir(mode=0o1777, parents=True, exist_ok=True)
    try:
        # mkdir()'s own `mode` is masked by umask and has no effect when
        # the directory already existed -- chmod unconditionally so the
        # sticky, world-writable bit sprint.md's Open Question 4 asks for
        # is always in force, regardless of umask or who created it
        # first. Best-effort: a directory this process doesn't own (e.g.
        # already created and locked down by a different user) simply
        # keeps whatever mode it has; that is an operator-visible
        # deployment problem, not something to raise/crash a probe cycle
        # over.
        os.chmod(directory, 0o1777)
    except OSError:
        pass

    lock_path = directory / f"{uid}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return ClaimHandle(uid=uid, _fd=fd)


def release(handle: ClaimHandle | None) -> None:
    """Free-function form of :meth:`ClaimHandle.release`, for a caller
    that prefers ``claims.release(handle)`` over a method call -- a
    no-op when ``handle`` is ``None`` (the "claim attempt failed" case),
    so a caller never needs its own ``if handle is not None`` guard just
    to release.
    """
    if handle is not None:
        handle.release()


def protect_fd(fd: int) -> bool:
    """Best-effort ``TIOCEXCL`` on an already-open tty file descriptor
    (see the module docstring's "belt-and-suspenders" note). Returns
    ``True`` if the ioctl succeeded, ``False`` on any failure (not a tty,
    unsupported platform, permission error) or on Windows -- never
    raises, since this is a defense-in-depth extra, not the mechanism
    :func:`try_claim` itself relies on to prove exclusivity.
    """
    if sys.platform == "win32" or fcntl is None or termios is None:
        return False
    try:
        fcntl.ioctl(fd, termios.TIOCEXCL)
    except OSError:
        return False
    return True


def is_claimable(
    uid: str,
    *,
    only_uids: Iterable[str] | None = None,
    exclude_uids: Iterable[str] | None = None,
) -> bool:
    """``--only-uid``/``--exclude-uid``'s own decision rule, factored out
    as a pure predicate so it's testable independent of
    :func:`build_claim_fn`'s closure: ``uid`` is claimable unless it's in
    ``exclude_uids``, or ``only_uids`` is given and non-empty and ``uid``
    isn't in it.
    """
    if exclude_uids is not None and uid in exclude_uids:
        return False
    if only_uids:
        return uid in only_uids
    return True


def build_claim_fn(
    *,
    only_uids: Iterable[str] | None = None,
    exclude_uids: Iterable[str] | None = None,
    claims_dir: Path | None = None,
) -> Callable[[str], ClaimHandle | None]:
    """Build a ``uid -> ClaimHandle | None`` callable suitable for
    :class:`mbtools.registry.daemon.Daemon`'s ``claim_fn`` parameter --
    what :mod:`mbtools.registry.cli`'s real ``mbregistry run`` assembly
    passes, wiring ``--only-uid``/``--exclude-uid`` into the claim-check
    path per this ticket's own Description.

    A uid rejected by :func:`is_claimable` never reaches :func:`try_claim`
    at all (skips the flock attempt entirely, "decided locally rather
    than by contention" per the ticket text) -- indistinguishable from a
    lost race to :class:`~mbtools.registry.daemon.Daemon`, which already
    treats any ``None`` return the same way regardless of *why* the claim
    was denied (retried again next cycle; see ``daemon.py``'s own
    docstring). An excluded/not-included uid is therefore retried forever
    too, harmlessly -- it always returns ``None`` again, exactly the same
    "no new bookkeeping needed" shape the ticket already establishes for
    the ordinary lost-a-race case.

    ``only_uids``/``exclude_uids`` are materialized into local ``set``s
    once, at build time, not re-evaluated per call. ``claims_dir`` is
    forwarded to every :func:`try_claim` call this closure makes -- a
    test builds a closure pointed at its own ``tmp_path``; production
    code (``registry.cli``) leaves it ``None`` to use
    :func:`mbtools.registry.paths.claims_dir_path`'s real, shared
    location.
    """
    only = set(only_uids) if only_uids else None
    exclude = set(exclude_uids) if exclude_uids else set()

    def _claim(uid: str) -> ClaimHandle | None:
        if not is_claimable(uid, only_uids=only, exclude_uids=exclude):
            return None
        return try_claim(uid, claims_dir=claims_dir)

    return _claim
