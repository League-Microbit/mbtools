"""mbtools.registry.eventbus — fan one produced event out to every
currently-connected ``watch`` client.

Per sprint 008 ticket 001 (sprint.md's Architecture, Step 3, module
"registry.eventbus"): this is the always-present second consumer of the
four callback hooks that, before this ticket, only ever reached
``PeerDiscovery`` (``Daemon.event_callback``,
``LockManager.lock_display_callback``,
``_api_base.BaseAPIServer._name_set_callback``/``_name_clear_callback``,
plus ``peering.py``'s new ``peer_up``/``peer_down`` source). A ``watch``
client (local socket or remote TCP port, see ``_api_base.BaseAPIServer
._op_watch``/``_handle_watch``) needs change notifications regardless of
whether this registry instance is peering with anyone else, so this
module exists independently of ``registry.peering``/ZeroMQ/sockets —
:class:`EventBus` knows nothing about any of those, and imports none of
them. Who decides *whether* an event also goes out over ZeroMQ PUB is
``registry.cli``'s assembly, not this module (sprint.md Decision 3):
this class only ever fans an already-fully-shaped event dict out to
whoever is currently subscribed.

**Thread safety**: ``publish``/``subscribe``/``unsubscribe`` may be
called from any thread — a ``Daemon`` scan-loop thread, a
``LockManager.acquire``/``release`` caller's thread, a per-connection API
handler thread, or ``peering.py``'s own reachability-callback thread, all
of which already invoke the callback hooks this bus is fed from
concurrently today. A single :class:`threading.Lock` guards the
subscriber set itself (architecture review flagged this as an
implementation-level detail sprint.md deliberately left below its own
module-level description). Delivery to each subscriber is via a
:class:`queue.Queue`, which is independently thread-safe on its own —
the lock here only ever protects *set membership* (who is currently
subscribed), never the per-subscriber queue's own put/get.
"""

from __future__ import annotations

import queue
import threading
from typing import Any

__all__ = ["EventBus"]


class EventBus:
    """A set of live subscriber queues, plus ``publish(event)``.

    :meth:`subscribe` returns a fresh :class:`queue.Queue` that receives
    every event published from that point on (change-only, no replay of
    anything published before the call — per the stakeholder decision
    recorded in sprint.md's Open Questions, ``watch`` is a change feed,
    not a snapshot-then-changes feed). :meth:`unsubscribe` removes it;
    calling it more than once, or with a queue that was never
    subscribed, is a no-op (never raises) — a connection's own
    close-handling ``finally`` block calls this unconditionally, and must
    never itself fail during cleanup.

    :meth:`publish` hands ``event`` to every subscriber current at the
    moment it is called — a subscriber added concurrently during the
    fan-out may or may not see it (no stronger ordering guarantee is
    needed: every caller today publishes one event at a time from a
    single producer thread per event, per the callback hooks this bus is
    fed from).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set["queue.Queue[dict[str, Any]]"] = set()

    def subscribe(self) -> "queue.Queue[dict[str, Any]]":
        q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: "queue.Queue[dict[str, Any]]") -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            q.put(event)
