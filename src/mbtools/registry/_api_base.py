"""mbtools.registry._api_base — shared per-op JSON dispatch logic for both
the local Unix-socket API server (``api.RegistryAPIServer``) and the
remote TCP control-plane server (``remote_api.RemoteAPIServer``, ticket
006).

Per sprint.md's Architecture and ticket 006's own Description ("Refactor
``api.RegistryAPIServer``'s per-op dispatch methods ... into a shared
base both the existing Unix-socket server and this new TCP server use,
rather than duplicating that logic"), the two servers differ only in
*transport* (``AF_UNIX`` vs ``AF_INET``), *peer-identity extraction*
(``SO_PEERCRED``/``LOCAL_PEERPID`` pid vs. a per-connection session id),
and which extra ops they support (ticket 007's framed binary stream
sub-protocol, ticket 008's remote ``flash`` op) — not in what
``list``/``find``/``lock``/``unlock``/``mark_flashed`` actually do once a
:class:`~mbtools.registry.locks.HolderRef` and a resolved
:class:`~mbtools.registry.store.DeviceRecord` are in hand. This module is
that shared middle: a mixin, :class:`BaseAPIServer`, holding the five ops
plus the ``_write``/``_device_dict``/``_error`` wire-shaping helpers,
parametrized on hooks a concrete subclass supplies:

- :meth:`BaseAPIServer._holder_for_connection`: turn a raw, just-accepted
  connection into this session's lock-holder identity. ``api.py`` reads
  the kernel-verified peer pid (``SO_PEERCRED``/``LOCAL_PEERPID``) and
  wraps it in a local :class:`~mbtools.registry.locks.HolderRef`;
  ``remote_api.py`` mints a random session id and tags it with the
  connecting client's source IP (see that module's own docstring for why
  that was chosen over a client-declared display host).
- :meth:`BaseAPIServer._list_visible_devices` /
  :meth:`BaseAPIServer._device_visible`: which rows this server's
  ``list``/``find`` (and, through :meth:`_resolve_visible`,
  ``lock``/``unlock``/``mark_flashed`` too) may see. ``api.py`` doesn't
  override either — every device, local or remote-owned, exactly as
  before this ticket (the local Unix socket is this registry's own
  operator-facing view of the whole fleet, not just what it physically
  owns). ``remote_api.py`` overrides both to scope a remote client to
  this registry's own (``host IS NULL``) devices only (ticket 006
  acceptance criterion #3) — a remote client resolving a name this
  registry doesn't own gets ``not_found``, exactly as if the device
  didn't exist here; this API never forwards a request to a third host
  (sprint.md Decision 8).

A plain mixin, not an ``abc.ABC`` — every hook already has a sensible
default (see above) except :meth:`_holder_for_connection`, which raises
:class:`NotImplementedError` loudly enough to catch in any real test if a
subclass forgets it, without forcing an ABC + each subclass's own
unrelated base-class needs into an awkward MRO.

Neither ``_op_flash`` (ticket 008: remote flash, with its own
retry/mass-erase semantics `api.RegistryAPIServer` and later
`remote_api.RemoteAPIServer` need in different shapes) nor the framed
binary stream sub-protocol (ticket 007) live here — both are explicitly
out of ticket 006's scope (see that ticket's own Description) and both
change for reasons independent of the five ops this module does own.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Callable

from mbtools.common import (
    CODE_AMBIGUOUS_NAME,
    CODE_INVALID_REQUEST,
    CODE_LOCKED,
    CODE_NOT_FOUND,
    CODE_NOT_LOCKED,
)
from mbtools.registry.eventbus import EventBus
from mbtools.registry.locks import (
    KIND_FLASH,
    LOCK_KINDS,
    HolderRef,
    LockHeldError,
    LockManager,
    LockStatus,
)
from mbtools.registry.store import AmbiguousNameError, DeviceRecord, Entry, Store

__all__ = ["BaseAPIServer"]


def _entry_dict(entry: Entry) -> dict[str, Any]:
    """Wire-shape one ``name_registry`` :class:`~mbtools.registry.store.Entry`
    for a ``names_get``/``names_set``/``names_list`` response -- every
    dataclass field, ``conflict``/``channel_conflict`` as plain lists
    (JSON has no tuple type).
    """
    return {
        "name": entry.name,
        "channel": entry.channel,
        "group": entry.group,
        "source": entry.source,
        "updated": entry.updated,
        "conflict": list(entry.conflict),
        "channel_conflict": list(entry.channel_conflict),
    }


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    return payload


def _holder_wire_dict(status: LockStatus) -> dict[str, Any]:
    """Wire-shape a lock's current holder for a ``locked`` response's
    ``holder`` field.

    Local holder: the ``{"kind", "pid"}`` shape from before this ticket,
    plus (sprint 008, ticket 002) ``label``/``since`` — every existing
    Unix-socket caller/test predates :class:`HolderRef` and asserts the
    original 2-key shape (ticket 006 acceptance criterion #1: zero
    observable behavior change for any existing Unix-socket caller), so
    the two new keys are strictly additive, exactly like ``origin``/
    ``host`` below. Remote holder: an additive superset (ticket 006
    acceptance criterion #8) — ``pid`` stays present (``None``, since "a
    PID means nothing across hosts") and ``origin``/``host`` are added,
    never replacing or renaming the local shape's two keys.

    ``label`` is ``None`` (never omitted -- ticket 002 acceptance
    criterion #2 allows either "absent" or "null"; this module always
    includes the key) when the lock was acquired with no label. ``since``
    is always the real acquisition timestamp once a lock exists — there
    is no "unset" case for a resolved :class:`LockStatus`.
    """
    wire: dict[str, Any] = {
        "kind": status.kind,
        "pid": status.pid,
        "label": status.label,
        "since": status.since,
    }
    if status.holder.origin == "remote":
        wire["origin"] = status.holder.origin
        wire["host"] = status.holder.host
    return wire


class BaseAPIServer:
    """Mixin: the five JSON ops both ``api.RegistryAPIServer`` and
    ``remote_api.RemoteAPIServer`` share, plus their wire-shaping
    helpers.

    Expects a concrete subclass to already have set ``self._store``
    (:class:`~mbtools.registry.store.Store`), ``self._locks``
    (:class:`~mbtools.registry.locks.LockManager`), and ``self._lock`` (a
    ``threading.RLock``, possibly shared with a
    :class:`~mbtools.registry.daemon.Daemon` — see
    ``api.RegistryAPIServer``'s own docstring) before any op method below
    is called. This mixin never constructs any of the three itself,
    mirroring the "don't construct a second ``LockManager``" contract
    every other module in this package already follows.
    """

    _store: Store
    _locks: LockManager
    # A threading.RLock, typed loosely here (this mixin never annotates
    # it directly — every op below just does ``with self._lock:``).

    #: Fired (outside ``self._lock``, after a successful write) by
    #: ``_op_names_set``/``_op_names_clear`` -- sprint 004 ticket 005's
    #: replication wiring. ``None`` by default (every pre-ticket-005
    #: subclass, and any subclass that never opts in, is unaffected); a
    #: concrete subclass that wants ``name_registry`` writes replicated
    #: sets these per-instance in its own ``__init__`` (see
    #: ``api.RegistryAPIServer``'s own docstring for why this mirrors
    #: ``Daemon``/``LockManager`` owning their own callbacks rather than
    #: ``Store`` owning one itself).
    _name_set_callback: "Callable[[Entry], None] | None" = None
    _name_clear_callback: "Callable[[str], None] | None" = None

    #: Sprint 008 ticket 001: the always-present event fan-out ``watch``
    #: subscribes to (see :meth:`_op_watch`/:meth:`_handle_watch`). Every
    #: concrete subclass constructs one (``api.RegistryAPIServer``/
    #: ``remote_api.RemoteAPIServer``'s own ``eventbus`` constructor
    #: parameter, defaulting to a private instance of its own when
    #: omitted, mirroring this mixin's existing ``lock``-parameter
    #: convention) -- no default here, since unlike
    #: ``_name_set_callback``/``_name_clear_callback`` (optional
    #: replication hooks that are fine to leave unwired), ``watch`` has
    #: nothing to dispatch into without a real bus.
    _eventbus: EventBus

    # -- hooks a concrete subclass provides -------------------------------

    def _holder_for_connection(self, conn: Any) -> HolderRef:
        """Turn a just-accepted connection into this session's
        :class:`HolderRef`. Must be overridden — see the module
        docstring."""
        raise NotImplementedError

    def _list_visible_devices(self) -> list[DeviceRecord]:
        """Every device this server's ``list`` may return.

        Default: every device (``api.RegistryAPIServer``'s pre-ticket-006
        behavior, unchanged). Override to scope a remote client's view.
        """
        return self._store.list_devices()

    def _device_visible(self, record: DeviceRecord) -> bool:
        """Is ``record`` in scope for this server's
        ``find``/``lock``/``unlock``/``mark_flashed``?

        Default: always (unchanged local behavior). Override to scope a
        remote client's view.
        """
        return True

    # -- wire-shaping helpers ----------------------------------------------

    @staticmethod
    def _write(wfile: Any, message: dict[str, Any]) -> None:
        wfile.write(json.dumps(message))
        wfile.write("\n")
        wfile.flush()

    def _device_dict(self, record: DeviceRecord) -> dict[str, Any]:
        """A device record, with its current lock status (kind + pid)
        folded in — ``list``/``get``/``find``'s acceptance criterion, so
        a STATE column never needs a second round-trip. Callers hold
        ``self._lock`` already.

        ``record.host is None`` (a locally-owned device): ``lock_kind``/
        ``lock_pid`` come from a live :meth:`LockManager.status` call, as
        before this ticket. ``record.host is not None`` (peer-owned, per
        sprint.md Decision 3): there is no live call to make for a device
        this registry doesn't own, so ``lock_kind``/``lock_pid`` stay
        ``None`` (``registry.render``, ticket 010, reads the record's own
        ``remote_lock_kind``/``remote_lock_display`` cache columns for
        that case instead — already present via ``asdict(record)``
        below). Two more fields are folded in for a remote-owned row,
        looked up from ``store``'s ``peer`` table by ``record.host``:
        ``endpoint`` (``host:remote_api_port``, what ticket 011/012/013's
        remote-client code needs to know where to connect — ``None`` for
        a local row) and ``peer_reachable`` (``store.PeerRecord.reachable``,
        what ``registry.render``'s "peer unreachable" rendering keys off
        — ``True`` for a local row, since there is no peer link to lose;
        ``False`` if ``host`` names a peer this store has no ``peer`` row
        for at all, treated the same as "known unreachable" since
        reachability can't be confirmed either way).

        ``lock_label``/``lock_since`` (sprint 008, ticket 002) fold in
        the same way ``lock_kind``/``lock_pid`` do — a live
        :class:`~mbtools.registry.locks.LockStatus` lookup for a local
        row, ``None`` for a peer-owned one (per that ticket's Design
        Rationale Decision 2, a peer-owned row's label/since text rides
        inside the existing ``remote_lock_display`` string instead —
        never a separate structured field for a remote row).
        """
        d = asdict(record)
        if record.host is None:
            holder = self._locks.status(record.uid)
            d["lock_kind"] = holder.kind if holder is not None else None
            d["lock_pid"] = holder.pid if holder is not None else None
            d["lock_label"] = holder.label if holder is not None else None
            d["lock_since"] = holder.since if holder is not None else None
            d["endpoint"] = None
            d["peer_reachable"] = True
        else:
            d["lock_kind"] = None
            d["lock_pid"] = None
            d["lock_label"] = None
            d["lock_since"] = None
            peer = self._store.get_peer(record.host)
            d["endpoint"] = peer.endpoint if peer is not None else None
            d["peer_reachable"] = peer.reachable if peer is not None else False
        return d

    def _resolve_visible(
        self, token: str
    ) -> tuple[DeviceRecord | None, dict[str, Any] | None]:
        """Resolve ``token`` via ``store.find``, applying this server's
        visibility scope (:meth:`_device_visible`) and translating a
        :class:`~mbtools.registry.store.AmbiguousNameError` into a wire
        error — ticket 001 deliberately left that translation undone
        ("no ``CODE_*`` constant was added in this ticket since nothing
        yet calls ``find()`` from the API layer with error-code
        translation"); this ticket, ``find()``'s first real API-layer
        caller, is where it was always meant to land.

        Returns ``(record, None)`` on success, or ``(None, error_dict)``
        — every caller below returns ``error_dict`` directly on the
        second case.
        """
        try:
            record = self._store.find(token)
        except AmbiguousNameError as exc:
            return None, _error(CODE_AMBIGUOUS_NAME, str(exc), hosts=list(exc.hosts))
        if record is None or not self._device_visible(record):
            return None, _error(CODE_NOT_FOUND, f"no such device: {token!r}")
        return record, None

    # -- shared ops ----------------------------------------------------------

    def _op_list(self) -> dict[str, Any]:
        with self._lock:
            devices = [self._device_dict(r) for r in self._list_visible_devices()]
        return {"ok": True, "devices": devices}

    def _op_find(self, req: dict[str, Any]) -> dict[str, Any]:
        token = req.get("uid") or req.get("token")
        if not token:
            return _error(CODE_INVALID_REQUEST, "'get'/'find' requires 'uid'")
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return err
            assert record is not None
            return {"ok": True, "device": self._device_dict(record)}

    def _op_watch(self) -> dict[str, Any]:
        """``watch()`` (sprint 008 ticket 001): the request/ack half only
        -- no precondition to check (unlike ``lock``/``unlock``, every
        connection may watch), so this always succeeds. A concrete
        subclass's dispatch writes this response, then hands the
        connection to :meth:`_handle_watch` instead of returning to read
        further request lines, exactly the way ``remote_api
        .RemoteAPIServer``'s ``stream`` op already switches a connection
        out of ordinary request/response dispatch (see that module's own
        "Protocol synchronization note").
        """
        return {"ok": True}

    def _handle_watch(self, wfile: Any) -> None:
        """``watch``'s data half: subscribe to :attr:`_eventbus` and
        write every event it produces to ``wfile``, one JSON line each,
        until the connection breaks or is closed from the other end.

        Change-only, per the stakeholder decision recorded in
        sprint.md's Open Questions -- no snapshot is sent on subscribe,
        only events published *after* this call subscribes (``list`` is
        this protocol's own point-in-time snapshot call; a client that
        wants both calls ``list`` first, then ``watch``, per
        ``docs/design/robot-console-integration.md`` §3.1's own two-step
        sequence).

        Always unsubscribes in a ``finally``, whether this method exits
        because writing to ``wfile`` failed (the client disconnected) or
        because of some other I/O error -- an ``EventBus`` subscriber
        that a broken connection never explicitly removes is exactly the
        "leaked queue" this ticket's acceptance criteria rule out.
        """
        q = self._eventbus.subscribe()
        try:
            while True:
                event = q.get()
                self._write(wfile, event)
        except (OSError, ConnectionError):
            pass
        finally:
            self._eventbus.unsubscribe(q)

    def _op_lock(
        self, req: dict[str, Any], holder: HolderRef, acquired_uids: set[str]
    ) -> dict[str, Any]:
        token = req.get("uid")
        kind = req.get("kind")
        if not token or not kind:
            return _error(CODE_INVALID_REQUEST, "'lock' requires 'uid' and 'kind'")
        if kind not in LOCK_KINDS:
            return _error(CODE_INVALID_REQUEST, f"unknown lock kind {kind!r}")
        # Sprint 008, ticket 002: an optional, display-only label -- never
        # used for holder identity/matching (see LockManager.acquire's own
        # docstring). Omitting it behaves exactly as before this ticket.
        label = req.get("label")
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return err
            assert record is not None
            try:
                self._locks.acquire(record.uid, kind, holder, label=label)
            except LockHeldError as exc:
                return _error(
                    CODE_LOCKED,
                    str(exc),
                    holder=_holder_wire_dict(exc.holder),
                )
            acquired_uids.add(record.uid)
            return {"ok": True}

    def _op_unlock(
        self, req: dict[str, Any], holder: HolderRef, acquired_uids: set[str]
    ) -> dict[str, Any]:
        token = req.get("uid")
        if not token:
            return _error(CODE_INVALID_REQUEST, "'unlock' requires 'uid'")
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return err
            assert record is not None
            released = self._locks.release(record.uid, holder)
            acquired_uids.discard(record.uid)
            return {"ok": True, "released": released}

    def _op_mark_flashed(self, req: dict[str, Any], holder: HolderRef) -> dict[str, Any]:
        token = req.get("uid")
        if not token:
            return _error(CODE_INVALID_REQUEST, "'mark_flashed' requires 'uid'")
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return err
            assert record is not None
            uid = record.uid
            status = self._locks.status(uid)
            if status is None or status.kind != KIND_FLASH or status.holder != holder:
                return _error(
                    CODE_NOT_LOCKED,
                    f"{uid}: mark_flashed requires a flash-kind lock held by "
                    "this connection (call 'lock' first)",
                )
            self._store.increment_flash_count(uid)
            return {"ok": True}

    # -- name-registry ops (sprint 004, ticket 005) -----------------------
    #
    # ``name_registry`` rows are not device rows -- no uid, no lock, no
    # per-connection visibility scope (every peer converges on the same
    # fleet-wide table, ticket 002/SUC-004) -- so these four skip
    # ``_resolve_visible`` entirely, unlike every op above. Not wired into
    # ``remote_api.RemoteAPIServer``'s dispatch by this ticket: the one
    # caller that exists so far (``mbrelay``'s CLI, ticket 005) always
    # resolves a name against its own *local* registry connection -- the
    # replicated copy ticket 002 already keeps converged -- never a peer's
    # ``remote_api``, so there is no caller yet to justify that surface.
    # A future ticket adding one extends ``BaseAPIServer`` (these four
    # methods) into ``remote_api.py``'s own dispatch, not a re-port.

    def _op_names_get(self, req: dict[str, Any]) -> dict[str, Any]:
        """``names_get(name)``: the non-creating lookup
        (:meth:`~mbtools.registry.store.Store.get_name`) -- ``entry`` is
        ``null`` (not an error) when ``name`` has no row yet, so a caller
        that wants "is this name registered at all" (``mbrelay connect``'s
        own distinct-error requirement, SUC-001) can tell that apart from
        a real protocol failure.
        """
        name = req.get("name")
        if not name:
            return _error(CODE_INVALID_REQUEST, "'names_get' requires 'name'")
        with self._lock:
            try:
                entry = self._store.get_name(str(name))
            except ValueError as exc:
                return _error(CODE_INVALID_REQUEST, str(exc))
            return {"ok": True, "entry": _entry_dict(entry) if entry is not None else None}

    def _op_names_set(self, req: dict[str, Any]) -> dict[str, Any]:
        """``names_set(name, channel, group)``: explicit assignment
        (:meth:`~mbtools.registry.store.Store.set`), then -- once the
        local write has committed -- :attr:`_name_set_callback`, if this
        server was given one, publishes it for replication (ticket 002's
        ``PeerDiscovery.publish_name_set``, wired by ``registry.cli``'s
        assembly). Fired outside ``self._lock`` (mirrors ``_op_flash``'s
        own "the shared lock only guards the short bookkeeping" note) --
        a ZMQ publish is not sqlite bookkeeping and must not hold up
        every other connection's ``list``/``lock``/... while it runs.
        """
        name = req.get("name")
        channel = req.get("channel")
        group = req.get("group")
        if not name or channel is None or group is None:
            return _error(
                CODE_INVALID_REQUEST, "'names_set' requires 'name', 'channel', 'group'"
            )
        with self._lock:
            try:
                entry = self._store.set(str(name), int(channel), int(group))
            except (ValueError, TypeError) as exc:
                return _error(CODE_INVALID_REQUEST, str(exc))
        if self._name_set_callback is not None:
            self._name_set_callback(entry)
        return {"ok": True, "entry": _entry_dict(entry)}

    def _op_names_clear(self, req: dict[str, Any]) -> dict[str, Any]:
        """``names_clear(name)``: drop the row
        (:meth:`~mbtools.registry.store.Store.clear`), then publish the
        clear for replication the same way :meth:`_op_names_set` does --
        see that method's own docstring.
        """
        name = req.get("name")
        if not name:
            return _error(CODE_INVALID_REQUEST, "'names_clear' requires 'name'")
        with self._lock:
            try:
                self._store.clear(str(name))
            except ValueError as exc:
                return _error(CODE_INVALID_REQUEST, str(exc))
        if self._name_clear_callback is not None:
            self._name_clear_callback(str(name))
        return {"ok": True}

    def _op_names_list(self, req: dict[str, Any]) -> dict[str, Any]:
        """``names_list()``: every row
        (:meth:`~mbtools.registry.store.Store.listing`), each annotated
        with its own ``conflict``/``channel_conflict`` names -- for
        ``mbrelay names list``'s operator-facing view.
        """
        with self._lock:
            entries = self._store.listing()
        return {"ok": True, "entries": [_entry_dict(e) for e in entries]}
