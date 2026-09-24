"""Shared pytest configuration for the ``mbtools`` test suite.

**Ticket 006 (Windows CI)**: the ``windows-latest`` GitHub Actions
runner's own CPython build has no ``socket.AF_UNIX`` at all --
``AttributeError: module 'socket' has no attribute 'AF_UNIX'``, not
merely "``AF_UNIX`` exists but a connect/bind call fails". This is
genuinely POSIX-only, exactly the kind of platform gap the ticket's own
instructions call out by name ("Skip only tests that genuinely need
POSIX-only facilities (AF_UNIX, os.kill semantics, SO_PEERCRED), each
with a reason"). It is not this project's own code that is broken: every
production entry point (``registry.cli``'s ``cmd_run``/``cmd_list``,
and ticket 006's own ``registry.client.resolve_local_api_address``)
already dispatches away from ``AF_UNIX`` on ``sys.platform == "win32"``
and uses :class:`~mbtools.registry.api_windows.WindowsPipeAPIServer`'s
named-pipe transport instead (separately tested in
``tests/registry/api_windows/`` and
``tests/registry/client/test_client_windows.py``). What fails here is
only this suite's own *test* fixtures/helpers that construct a real
``socket.AF_UNIX`` socket or a real
:class:`~mbtools.registry.api.RegistryAPIServer` directly (an
integration-style testing choice made in earlier sprints, before
Windows was a supported platform at all) -- those have no Windows-
native substitute of their own to fall back to; a named pipe is a
different transport with its own, already-separately-tested server
class, not a drop-in stand-in for what these tests exercise.

Every test (or whole module, via ``pytestmark = pytest.mark.requires_af_unix``)
that needs this facility carries the ``requires_af_unix`` marker rather
than a locally-repeated ``@pytest.mark.skipif(...)`` -- one shared,
documented reason here instead of the same condition and rationale
copy-pasted at ~115 call sites across a dozen files.
"""

from __future__ import annotations

import socket

import pytest

#: Whether this platform's Python build actually exposes
#: ``socket.AF_UNIX`` at all. Windows 10 1803+/a recent CPython *can*
#: expose it, but this project's own ``windows-latest`` CI runner's
#: build does not (see module docstring) -- checked via ``hasattr``,
#: never a bare ``sys.platform != "win32"`` guess, so this stays correct
#: even if a future runner image does add support.
HAS_AF_UNIX = hasattr(socket, "AF_UNIX")

_REASON = (
    "needs a real socket.AF_UNIX, which this platform's Python build "
    "does not expose at all (ticket 006: the windows-latest CI runner "
    "has no socket.AF_UNIX attribute) -- WindowsPipeAPIServer is a "
    "separate, separately-tested server/transport, not a substitute for "
    "what this test exercises"
)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_af_unix: needs a real socket.AF_UNIX -- skipped, with "
        "a shared reason (see tests/conftest.py), on a platform/Python "
        "build that lacks it",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if HAS_AF_UNIX:
        return
    skip_marker = pytest.mark.skip(reason=_REASON)
    for item in items:
        if "requires_af_unix" in item.keywords:
            item.add_marker(skip_marker)
