"""Shared "not yet implemented" behavior for console-script stubs.

``mbdeploy``, ``mbserial``, and ``mbrelay`` don't have implementations yet
(sprint 002 and sprint 004 of the mbtools roadmap — see
``clasi/sprints/``). Each of their stub packages calls :func:`stub_main`
from its console-script ``main()`` rather than repeating the same
print-and-exit three times.
"""

from __future__ import annotations

import sys


def stub_main(program: str, where: str) -> int:
    """Print a "not yet implemented" message for ``program`` to stderr.

    ``where`` names the sprint (and, when useful, the ticket) that will
    implement it, e.g. ``"sprint 002"``. Returns the exit status the
    caller should pass to :func:`sys.exit` — this function never exits by
    itself, so it stays trivially testable (call it, assert on the return
    value and captured stderr, no ``pytest.raises(SystemExit)`` needed).
    """
    print(f"{program}: not yet implemented — see mbtools {where}", file=sys.stderr)
    return 1
