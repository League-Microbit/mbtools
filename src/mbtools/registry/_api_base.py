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
from typing import Any

from mbtools.common import (
    CODE_AMBIGUOUS_NAME,
    CODE_INVALID_REQUEST,
    CODE_LOCKED,
    CODE_NOT_FOUND,
    CODE_NOT_LOCKED,
)
from mbtools.registry.locks import (
    KIND_FLASH,
    LOCK_KINDS,
    HolderRef,
    LockHeldError,
    LockManager,
    LockStatus,
)
from mbtools.registry.store import AmbiguousNameError, DeviceRecord, Store

__all__ = ["BaseAPIServer"]


def _error(code: str, message: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "code": code, "error": message}
    payload.update(extra)
    return payload


def _holder_wire_dict(status: LockStatus) -> dict[str, Any]:
    """Wire-shape a lock's current holder for a ``locked`` response's
    ``holder`` field.

    Local holder: the unchanged 2-key ``{"kind", "pid"}`` shape — every
    existing Unix-socket caller/test predates :class:`HolderRef` and
    asserts this exact shape (ticket 006 acceptance criterion #1: zero
    observable behavior change for any existing Unix-socket caller).
    Remote holder: an additive superset (ticket 006 acceptance criterion
    #8) — ``pid`` stays present (``None``, since "a PID means nothing
    across hosts") and ``origin``/``host`` are added, never replacing or
    renaming the local shape's two keys.
    """
    wire: dict[str, Any] = {"kind": status.kind, "pid": status.pid}
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
        """
        holder = self._locks.status(record.uid)
        d = asdict(record)
        d["lock_kind"] = holder.kind if holder is not None else None
        d["lock_pid"] = holder.pid if holder is not None else None
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

    def _op_lock(
        self, req: dict[str, Any], holder: HolderRef, acquired_uids: set[str]
    ) -> dict[str, Any]:
        token = req.get("uid")
        kind = req.get("kind")
        if not token or not kind:
            return _error(CODE_INVALID_REQUEST, "'lock' requires 'uid' and 'kind'")
        if kind not in LOCK_KINDS:
            return _error(CODE_INVALID_REQUEST, f"unknown lock kind {kind!r}")
        with self._lock:
            record, err = self._resolve_visible(str(token))
            if err is not None:
                return err
            assert record is not None
            try:
                self._locks.acquire(record.uid, kind, holder)
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
