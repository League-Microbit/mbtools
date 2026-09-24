"""mbtools.registry.console_compat — robot-console compatibility surface.

Per sprint 004 sprint.md's Architecture (Decision 1: robot-console
compatibility is hosted *inside* ``mbregistry`` itself, not a second
daemon and not a robot-console migration), this package holds the two
network-facing modules that keep robot-console's own, unmodified source
working against ``mbregistry``:

- :mod:`mbtools.registry.console_compat.relay_pool` (ticket 006): the
  ``_mbrelay._tcp`` pool-port TCP listener.
- ``mbtools.registry.console_compat.names_api`` (ticket 007, not yet
  built): the ``/names/<name>`` HTTP contract.

Neither module is imported here -- each is wired into
``registry.cli.assemble_registry``/``cmd_run`` individually, mirroring
how ``registry.peering``/``registry.remote_api`` are wired rather than
re-exported through a package ``__init__``.
"""

from __future__ import annotations
