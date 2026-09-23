"""``mbregistry`` — CLI entry point for the device-registry daemon.

Placeholder for this ticket (001): ``mbregistry list``/``run``/
``install-service`` are ticket 009's job (sprint 001, once tickets
002-008 build the daemon this CLI drives). Until then the console script
resolves and fails clearly rather than not existing at all.
"""

from __future__ import annotations

import sys

from mbtools._stub import stub_main


def main() -> None:
    sys.exit(stub_main("mbregistry", "sprint 001 ticket 009"))
