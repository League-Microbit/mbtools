"""mbtools.registry.daemon — the attach/detach → probe → store pipeline
and the re-probe rules.

Per sprint.md's Architecture (module "daemon"), this is the orchestrator
that wires :mod:`mbtools.registry.usbwatch`, :mod:`mbtools.registry.identity`,
:mod:`mbtools.registry.store`, and :mod:`mbtools.registry.locks` into the
running service. :class:`Daemon` holds no *persistent* state of its own —
everything it touches lives in ``store`` or ``locks`` — its job is the
*pipeline* (scan, diff, probe, persist) and the *policy* (when a probe is
allowed to happen at all).

**Re-probe rule** (SUC-001/UC-001's core invariant): a device already
probed and still attached is never reopened. The pipeline only probes a
uid when :meth:`mbtools.registry.store.Store.needs_probe` says so (true
exactly for a never-probed or just-reattached device) — or when this
module's own flash-pending tracking says a flash-triggered re-probe is
due (see below). An already-``connected`` device staying attached across
scans is never touched again.

**Flash-triggered re-probe**: :class:`Daemon` owns its own
:class:`~mbtools.registry.locks.LockManager` (constructed in
:meth:`Daemon.__init__`, exposed as :attr:`Daemon.locks` for callers —
ticket 007's flash op and ticket 008's API dispatch against this same
instance) and registers :meth:`Daemon._on_flash_release` as its
``flash_release_callback``. When a ``flash``-kind lock on a uid releases,
that uid is recorded in :attr:`Daemon._flash_pending` with a deadline.
Every cycle, a flash-pending uid is probed as soon as it is seen attached
again — regardless of what :meth:`Store.needs_probe` says, since a flash
can leave the DAPLink interface enumerated throughout (no detach/reattach
cycle to trip the store's own "reattach resets last_probe" rule) — and if
it never reappears before its deadline, it is marked
``connected_no_firmware`` (the store's existing "blank-equivalent" state,
reused here rather than inventing a new one) instead of waiting forever.

**Detach handling**: a uid that drops out of a scan has any lock it holds
force-released (a detach is not a graceful release — UC-002's
postcondition "any lock is released") by reading the current holder's
:class:`~mbtools.registry.locks.HolderRef` off :meth:`LockManager.status`
and passing it back to :meth:`LockManager.release` — legitimate because
this is the daemon's own privileged bookkeeping, not a client-supplied
identity a caller could use to steal someone else's lock. Every local
caller only ever holds local (PID-tied) locks in this ticket's scope, so
this is unchanged in effect from the pre-ticket-002 pid-based release;
it is now holder-generalized only because :meth:`LockManager.release`'s
signature is. The record is then marked ``disconnected``.
Flash-pending tracking is left untouched across a detach — the very next
scan that sees the uid reattach clears it (see above); the ticket's
"detach-vs-flash disambiguation" is, concretely, that an ordinary detach
never has a flash-pending entry to preserve, so this shared code path
behaves identically for both cases without needing to branch on which one
it is.

**Locked devices are never probed.** Before opening a port for any uid
(new attach, still-attached-and-flash-pending, or reattach),
:meth:`LockManager.status` is checked; a locked uid is skipped for this
cycle and retried on a later one once it is unlocked — the probe would
otherwise fight over the very port a lock exists to protect.

**Event hook (ticket 005)**: :class:`Daemon` accepts an optional
``event_callback`` (mirroring :class:`~mbtools.registry.locks.LockManager`'s
own ``flash_release_callback`` convention — constructor-injected, defaults
to ``None``, a no-op when unset so every existing caller/test is
unaffected) fired with ``("attach" | "detach" | "identity", record)`` on
every attach, detach, and completed probe (identity-change) — including
the flash-reprobe-timeout give-up path, since that is also a completed
probe (``apply_probe_result(uid, None)``) from a peer's point of view.
This is where ``registry.peering`` (ticket 005) attaches its
PUB-socket publish call for ticket 009's assembly to wire up; this class
itself never imports or knows about ``peering``/ZeroMQ. Every callback
fires only *after* the triggering ``store``/``locks`` writes are
committed and, for the attach/detach batch in :meth:`run_once`, only
after :attr:`_lock` is released — the same "no I/O under the shared
lock" reasoning the module docstring's "Concurrency" note already applies
to probing itself, extended to cover a callback that will end up doing a
network send.

**Concurrency (ticket 009's assembly)**: this class's own thread (the
poll loop calling :meth:`run_once`) and the API's per-connection threads
(ticket 008) both touch the same ``store``/``locks`` instances with no
common guard between the two modules — flagged as a known gap in
``docs/design/registry-api.md``'s "Known limitations". ``mbregistry run``
(ticket 009) closes that gap by constructing one shared
``threading.RLock`` at assembly time and passing it to both this class's
``lock`` parameter and
:class:`~mbtools.registry.api.RegistryAPIServer`'s. :class:`Daemon` holds
it only around the short, in-memory-or-single-sqlite-statement bookkeeping
steps (the attach/detach diff and store writes in :meth:`run_once`, and
the eligibility check plus the post-probe store write in
:meth:`_maybe_probe`) — never around :func:`mbtools.registry.identity.probe`
itself, which opens a real serial port and can block for over a second.
Holding a shared lock across that would stall every API call for the
duration of every probe, which is worse than the race the lock exists to
close. A caller that constructs a bare :class:`Daemon` without ``lock=``
(every test in this module, and any future single-threaded use) gets a
private ``RLock`` of its own — harmless, since nothing else shares it.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from mbtools.common import PortInfo
from mbtools.registry import identity
from mbtools.registry.locks import LockManager
from mbtools.registry.store import (
    STATE_DISCONNECTED,
    DeviceRecord,
    Store,
    format_vid_pid,
)
from mbtools.registry.usbwatch import PortWatcher

__all__ = ["Daemon", "DEFAULT_INTERVAL_S", "DEFAULT_FLASH_REPROBE_TIMEOUT_S"]

logger = logging.getLogger(__name__)

#: Default bound on how long a flash-triggered re-probe waits for the
#: device to re-enumerate before giving up (UC-003's error flow) — chosen
#: generously relative to a DAPLink reset/re-enumeration cycle without
#: being so long a genuinely failed flash hangs the record in limbo.
DEFAULT_FLASH_REPROBE_TIMEOUT_S = 10.0

#: Default poll interval for :meth:`Daemon.run`.
DEFAULT_INTERVAL_S = 2.0


class Daemon:
    """Orchestrates one scan-diff-probe cycle at a time.

    ``usbwatch`` and ``store`` are injected — the caller (a test, or the
    real assembly ticket 009's ``mbregistry run`` builds) owns their
    lifecycle. ``locks`` is *not* injected: :class:`Daemon` always
    constructs its own :class:`~mbtools.registry.locks.LockManager` so it
    can wire its flash-release callback (and, as of ticket 009, a
    ``lock_display_callback`` — see below) at construction, the only
    point ``LockManager`` accepts either — exposed as :attr:`locks` so
    ticket 007/008 (and tests) can acquire/release/inspect locks against
    the exact instance this daemon watches.

    ``serial_factory``/``probe_timeout_s``/``settle_s`` are forwarded
    verbatim to :func:`mbtools.registry.identity.probe` on every probe
    call — the same test-only escape hatch ``identity.probe`` itself
    documents (a test passes a ``serial_factory`` returning
    ``mbtools.testing.fakes.FakeSerial`` instances and ``settle_s=0``;
    production code leaves both at their defaults).

    ``now_fn`` is this module's own injectable clock (mirroring
    ``store``'s ``now_fn`` and ``usbwatch``'s ``comports_fn``), used only
    for flash-pending deadline bookkeeping — a test passes a deterministic
    stepped clock instead of depending on real wall-clock time to exercise
    the timeout path without sleeping.

    ``lock`` is the shared ``threading.RLock`` ticket 009's ``mbregistry
    run`` constructs once and passes to both this class and
    :class:`~mbtools.registry.api.RegistryAPIServer` — see the module
    docstring's "Concurrency" note. Defaults to a private ``RLock`` of
    this instance's own when omitted, so single-threaded callers (every
    test in this module) are unaffected.

    ``event_callback`` is the optional attach/detach/identity hook
    described in the module docstring's "Event hook" note. Defaults to
    ``None`` (no-op) so every pre-ticket-005 caller/test is unaffected.

    ``lock_display_callback`` (ticket 009) is forwarded verbatim into
    this daemon's own :class:`~mbtools.registry.locks.LockManager`
    construction — :func:`mbtools.registry.cli.assemble_registry` passes
    ``registry.peering.PeerDiscovery.publish_lock_event`` here so every
    lock-acquire/lock-release this daemon's ``LockManager`` sees is also
    published onto the peering event bus (sprint.md Decision 3's
    replicated lock-display cache). Defaults to ``None`` (no-op), so
    every pre-ticket-009 caller/test is unaffected.
    """

    def __init__(
        self,
        *,
        usbwatch: PortWatcher,
        store: Store,
        serial_factory: Callable[..., Any] | None = None,
        probe_timeout_s: float = 1.6,
        settle_s: float | None = None,
        flash_reprobe_timeout_s: float = DEFAULT_FLASH_REPROBE_TIMEOUT_S,
        now_fn: Callable[[], float] = time.monotonic,
        lock: threading.RLock | None = None,
        event_callback: Callable[[str, DeviceRecord], None] | None = None,
        lock_display_callback: Callable[[str, str | None, str | None], None]
        | None = None,
    ) -> None:
        self._usbwatch = usbwatch
        self._store = store
        self._serial_factory = serial_factory
        self._probe_timeout_s = probe_timeout_s
        self._settle_s = settle_s
        self._flash_reprobe_timeout_s = flash_reprobe_timeout_s
        self._now = now_fn
        self._lock = lock if lock is not None else threading.RLock()
        self._event_callback = event_callback

        #: uid -> deadline (per ``now_fn``) by which a flash-triggered
        #: re-probe must see the device re-enumerate, or it gives up.
        #: Ephemeral, in-memory only — lost on restart, same as
        #: ``locks``' own table; a daemon that crashed mid-flash-wait has
        #: no record of the wait to resume, which is acceptable per
        #: sprint.md's Open Questions (no stronger guarantee is asked
        #: for).
        self._flash_pending: dict[str, float] = {}

        self.locks = LockManager(
            flash_release_callback=self._on_flash_release,
            lock_display_callback=lock_display_callback,
        )

    # -- flash-release hook ------------------------------------------------

    def _on_flash_release(self, uid: str) -> None:
        """Registered as :class:`LockManager`'s ``flash_release_callback``.

        Fires exactly once per ``flash``-kind lock release (the
        guarantee is ``LockManager``'s own — see its ``_release``
        docstring). Records a deadline; does not probe here — probing
        only happens once the device is actually seen attached again, in
        :meth:`run_once`.
        """
        deadline = self._now() + self._flash_reprobe_timeout_s
        self._flash_pending[uid] = deadline
        logger.info(
            "daemon: flash lock released for %s, awaiting re-enumeration by %.3f",
            uid,
            deadline,
        )

    # -- event hook ------------------------------------------------------

    def _fire_events(self, event_type: str, records: list[DeviceRecord]) -> None:
        """Call :attr:`_event_callback` once per ``record`` in ``records``,
        a no-op if no callback was registered.

        Always called *after* the triggering ``store`` writes are
        committed and outside :attr:`_lock` — see the module docstring's
        "Event hook" note.
        """
        if self._event_callback is None:
            return
        for record in records:
            self._event_callback(event_type, record)

    # -- the pipeline --------------------------------------------------

    def run_once(self) -> None:
        """Perform exactly one scan-diff-probe cycle.

        1. Snapshot currently-attached devices via ``usbwatch.scan()``.
        2. For each attached uid: ``store.upsert_attached`` if it wasn't
           already known as attached, then probe it if eligible (see
           :meth:`_maybe_probe`).
        3. For each uid that was attached last cycle but is gone now:
           force-release any lock it holds, then ``store.mark_disconnected``.
        4. For each flash-pending uid that is still absent and past its
           deadline: give up and mark it ``connected_no_firmware``.

        "Attached last cycle" is read from ``store`` (any *locally-owned*
        record -- ``host is None`` -- whose ``state`` isn't
        ``disconnected``) rather than kept as a separate in-memory set —
        per the module's "no persistent state of its own" boundary, and
        because it makes a daemon restart naturally idempotent: the very
        next scan re-attaches every currently-present uid against whatever
        the store already believes, and ``store.upsert_attached``'s own
        "not a reattach unless previously disconnected" rule (ticket 004)
        takes it from there.

        The ``host is None`` filter (sprint 005 ticket 011) matters
        because ``store`` also holds *remote*-owned rows this host learned
        about via ``registry.peering`` — a uid this host has never itself
        scanned, but knows about because some peer publishes it. Without
        the filter, a uid physically attached to this host but still
        mirrored here under a peer's stale ownership claim (``host`` !=
        ``None``, ``state`` not yet ``disconnected``) would already count
        as "previously attached" and never reach ``store.upsert_attached``
        below, so this host could never reclaim local ownership of its own
        physically-attached board — the exact gap that let one of
        ``torture``'s three relays stay stuck as ``host=hodr`` even after
        this ticket's store/peering-level ownership fix landed (see the
        ticket's Implementation Notes for the hardware trace). The same
        filter also stops this host's own scan from ever treating a
        uid it merely *mirrors* from a peer (never physically here) as
        "gone missing" below — that uid is never in ``current`` (this
        host's own USB scan) either, so an unfiltered
        ``previously_attached`` would wrongly diff it into ``detached``
        and fire a bogus ``EVENT_DETACH`` for a device this host never
        owned, every single cycle.

        The attach/detach diff and every store/locks write it makes runs
        under :attr:`_lock` (see the module docstring's "Concurrency"
        note) — this is all in-memory work plus single, atomic sqlite
        statements, never a real port open, so holding the shared lock
        for it doesn't stall the API. Probing (:meth:`_maybe_probe`) is
        deliberately done in a second pass, after that block releases the
        lock, so the port I/O itself never runs with the shared lock
        held. ``event_callback`` (see the module docstring's "Event hook"
        note) is fired for this cycle's attach/detach/identity records
        after that same block releases the lock too — a callback that
        does a network send must never run while the shared lock is
        held.
        """
        now = self._now()
        current = self._usbwatch.scan()
        attached: list[DeviceRecord] = []
        detached: list[DeviceRecord] = []
        timed_out: list[DeviceRecord] = []

        with self._lock:
            previously_attached = {
                record.uid
                for record in self._store.list_devices()
                if record.host is None and record.state != STATE_DISCONNECTED
            }

            for uid, info in current.items():
                if uid not in previously_attached:
                    record = self._store.upsert_attached(
                        uid, info.port, format_vid_pid(info.vid, info.pid)
                    )
                    attached.append(record)

            for uid in previously_attached - current.keys():
                status = self.locks.status(uid)
                if status is not None:
                    self.locks.release(uid, status.holder)
                record = self._store.mark_disconnected(uid)
                detached.append(record)

            for uid, deadline in list(self._flash_pending.items()):
                if uid not in current and now >= deadline:
                    logger.warning(
                        "daemon: %s never re-enumerated after flash within timeout; "
                        "marking no-firmware",
                        uid,
                    )
                    record = self._store.apply_probe_result(uid, None)
                    timed_out.append(record)
                    del self._flash_pending[uid]

        self._fire_events("attach", attached)
        self._fire_events("detach", detached)
        self._fire_events("identity", timed_out)

        for uid, info in current.items():
            self._maybe_probe(uid, info)

    def _maybe_probe(self, uid: str, info: PortInfo) -> None:
        """Probe ``uid`` on ``info.port`` if, and only if, it is eligible.

        Eligible means: the store's own re-probe rule says so
        (:meth:`Store.needs_probe`), or this uid is awaiting its
        flash-triggered re-probe (:attr:`_flash_pending`) — and, either
        way, the device is not currently locked (a locked device is, by
        construction, either in active use or mid-flash; the passive
        pipeline must never open its port). A locked-but-eligible uid is
        simply skipped for this cycle and retried on the next one.

        The eligibility check and the post-probe store write are each
        wrapped in :attr:`_lock` (see the module docstring's
        "Concurrency" note); the actual :func:`~mbtools.registry.identity.probe`
        call — the one part of this method that opens a real port and can
        block for over a second — runs with the lock released, so it
        never stalls the API. This narrows, but does not eliminate, the
        pre-existing race between "checked unlocked" and "port opened": a
        client could acquire a lock in that gap. That race already existed
        before this method held any lock at all (there was no shared lock
        to close it with); this change only stops it from stalling
        unrelated API calls, it does not add a new guarantee against that
        specific interleaving. ``event_callback`` (see the module
        docstring's "Event hook" note) fires with the post-probe record
        after this second ``with self._lock`` block releases it, for the
        same "no network I/O under the shared lock" reason.

        ``identity.probe`` is called with ``reset_first=identity
        .is_relay(<uid's pre-probe stored role>)`` — the role the uid's
        record held *before* this probe, read from the store inside the
        same locked eligibility check above, never after the probe
        completes (the post-probe role is exactly what a misidentified
        re-probe would get wrong — see sprint.md's Architecture and this
        ticket). This runs identically whether ``eligible`` came from
        ``needs_probe`` (an ordinary reattach) or from
        ``uid in self._flash_pending`` (a flash-triggered re-probe): a
        relay's stored role forces a BREAK before ``HELLO`` either way,
        and a non-relay uid (or one with no prior record at all) never
        sees one — unaffected, byte for byte, by this change.
        """
        with self._lock:
            eligible = self._store.needs_probe(uid) or uid in self._flash_pending
            if not eligible or self.locks.status(uid) is not None:
                return
            pre_probe_record = self._store.get(uid)

        reset_first = identity.is_relay(
            pre_probe_record.role if pre_probe_record is not None else None
        )

        result = identity.probe(
            info.port,
            self._probe_timeout_s,
            serial_factory=self._serial_factory,
            settle_s=self._settle_s,
            reset_first=reset_first,
        )

        with self._lock:
            record = self._store.apply_probe_result(uid, result)
            self._flash_pending.pop(uid, None)

        self._fire_events("identity", [record])

    # -- run loop --------------------------------------------------------

    def run(
        self,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        stop: Callable[[], bool] | None = None,
    ) -> None:
        """Run :meth:`run_once` on an ``interval_s`` cadence until ``stop()``
        returns ``True``.

        ``stop`` defaults to ``None``, meaning "run forever" (the real
        ``mbregistry run``, ticket 009's job, supplies one tied to a
        signal handler or similar). Tests drive :meth:`run_once` directly
        cycle-by-cycle instead of calling this method, per sprint.md's
        Test Strategy ("tests use a very short [interval] or drive cycles
        manually rather than sleeping") — this method exists to satisfy
        the ticket's "a single run() loop" acceptance criterion and to
        give ticket 009 something to call, not because any test in this
        ticket exercises real sleeping at length.
        """
        while stop is None or not stop():
            self.run_once()
            if stop is not None and stop():
                break
            time.sleep(interval_s)
