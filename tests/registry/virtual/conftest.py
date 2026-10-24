import shutil
import tempfile

import pytest

from mbtools.registry.eventbus import EventBus
from mbtools.registry.locks import LockManager
from mbtools.registry.store import Store

from virtual_helpers import FakeEndpoint, make_server_factory


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "devices.db")
    yield s
    s.close()


@pytest.fixture
def locks():
    return LockManager()


@pytest.fixture
def socket_dir():
    d = tempfile.mkdtemp(prefix="mbvirt-")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def eventbus():
    return EventBus()


@pytest.fixture
def make_server(socket_dir, store, locks, eventbus):
    events: list[tuple[str, object]] = []

    def event_callback(event_type, record):
        events.append((event_type, record))
        # mirror cli.assemble_registry: also publish the wire event on the bus
        from mbtools.registry.peering import daemon_event_payload

        payload = daemon_event_payload(event_type, record)
        if payload is not None:
            eventbus.publish({"type": event_type, "host": "testhost", **payload})

    _make, servers = make_server_factory(
        socket_dir, store, locks, eventbus=eventbus, event_callback=event_callback
    )
    _make.events = events
    yield _make
    for srv in servers:
        srv.stop()


@pytest.fixture
def endpoints():
    made = []

    def _make(**kw):
        ep = FakeEndpoint(**kw)
        made.append(ep)
        return ep

    yield _make
    for ep in made:
        ep.close()
