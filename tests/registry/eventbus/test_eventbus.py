"""Tests for mbtools.registry.eventbus.EventBus (sprint 008 ticket 001) --
subscribe/publish/unsubscribe, multiple subscribers, and the "no leaked
queue, no exception on the next publish" contract a `watch` connection's
close-handling relies on.
"""

from __future__ import annotations

import queue
import threading

from mbtools.registry.eventbus import EventBus


def test_subscribe_returns_a_fresh_queue_each_time():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    assert q1 is not q2


def test_publish_delivers_to_a_single_subscriber():
    bus = EventBus()
    q = bus.subscribe()
    event = {"type": "attach", "uid": "abc"}

    bus.publish(event)

    assert q.get(timeout=1.0) == event


def test_publish_fans_out_to_every_current_subscriber():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    q3 = bus.subscribe()
    event = {"type": "detach", "uid": "xyz"}

    bus.publish(event)

    for q in (q1, q2, q3):
        assert q.get(timeout=1.0) == event


def test_publish_preserves_order_per_subscriber():
    bus = EventBus()
    q = bus.subscribe()
    events = [{"type": "attach", "uid": str(i)} for i in range(5)]

    for event in events:
        bus.publish(event)

    received = [q.get(timeout=1.0) for _ in events]
    assert received == events


def test_publish_with_no_subscribers_does_not_raise():
    bus = EventBus()
    bus.publish({"type": "attach", "uid": "abc"})  # no subscribers -- no-op


def test_unsubscribe_stops_further_delivery():
    bus = EventBus()
    q = bus.subscribe()
    bus.publish({"type": "attach", "uid": "1"})
    assert q.get(timeout=1.0) == {"type": "attach", "uid": "1"}

    bus.unsubscribe(q)
    bus.publish({"type": "attach", "uid": "2"})

    assert q.empty()


def test_unsubscribe_does_not_affect_other_subscribers():
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()

    bus.unsubscribe(q1)
    bus.publish({"type": "attach", "uid": "1"})

    assert q1.empty()
    assert q2.get(timeout=1.0) == {"type": "attach", "uid": "1"}


def test_unsubscribe_is_idempotent_and_never_raises():
    bus = EventBus()
    q = bus.subscribe()
    bus.unsubscribe(q)
    bus.unsubscribe(q)  # already removed -- must not raise


def test_unsubscribe_of_a_never_subscribed_queue_never_raises():
    bus = EventBus()
    stray = queue.Queue()
    bus.unsubscribe(stray)  # never subscribed -- must not raise


def test_publish_after_unsubscribe_never_raises_for_remaining_subscribers():
    """The "no exception logged on the next publish" acceptance
    criterion: a connection that disconnects and unsubscribes must never
    poison a later publish() for whoever is still listening."""
    bus = EventBus()
    q1 = bus.subscribe()
    q2 = bus.subscribe()
    bus.unsubscribe(q1)

    bus.publish({"type": "detach", "uid": "1"})  # must not raise

    assert q2.get(timeout=1.0) == {"type": "detach", "uid": "1"}


def test_concurrent_subscribe_publish_unsubscribe_does_not_deadlock_or_raise():
    """A basic thread-safety smoke test -- publish/subscribe/unsubscribe
    all called concurrently from several threads must never raise or
    hang (the subscriber-set lock is the whole point of this module's
    own "implementation-level detail" callout)."""
    bus = EventBus()
    errors: list[Exception] = []
    stop = threading.Event()

    def _publisher():
        while not stop.is_set():
            bus.publish({"type": "attach", "uid": "x"})

    def _churner():
        try:
            for _ in range(200):
                q = bus.subscribe()
                bus.publish({"type": "attach", "uid": "y"})
                bus.unsubscribe(q)
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    publisher = threading.Thread(target=_publisher)
    churners = [threading.Thread(target=_churner) for _ in range(4)]
    publisher.start()
    for t in churners:
        t.start()
    for t in churners:
        t.join(timeout=10.0)
    stop.set()
    publisher.join(timeout=2.0)

    assert not errors
