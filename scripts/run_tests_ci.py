"""CI-only pytest wrapper (ticket 006): forces the process to exit the
instant :func:`pytest.main` returns, instead of falling through to
Python's normal interpreter shutdown.

**Why this exists**: run 35995334473's ``windows-latest`` leg (this
sprint's earlier CI iterations -- see that ticket's Implementation
Notes) printed pytest's own final summary line
(``67 failed, 734 passed, ...``) at 11:52:28 UTC, then the job sat idle
for another ~18 minutes before ``timeout-minutes: 20`` killed it --
*after* pytest had already finished computing and reporting every
result. That gap is Python's own interpreter shutdown
(``threading._shutdown()``) waiting on a live, non-daemon thread this
suite's own fixtures did not fully account for somewhere -- every
server/daemon fixture in this project's own test suite is written to
join its threads before returning (see the project's own testing
conventions), but a third-party dependency (this suite exercises real
background threads for ``zeroconf``, real subprocesses, and, on
Windows, real ``ctypes``-driven OS threads) is exactly the kind of
thing that can leave one behind without this project's own code being
at fault, and finding the exact culprit needs a real Windows debugger
session this project doesn't have access to.

Rather than chase that indefinitely, this wrapper sidesteps the whole
question the same way ``pytest-timeout``'s own ``"thread"`` method
already does for a single hung *test* (dump nothing needed here --
pytest already printed its summary -- then ``os._exit()`` once the
*session* itself is done): call :func:`pytest.main`, flush stdout/
stderr so the CI log has everything pytest already printed, then
``os._exit(code)`` -- skips atexit handlers and any thread join
Python's normal shutdown would otherwise wait on, and returns pytest's
own exit code unchanged to the CI job.

Not used for local development (`uv run pytest ...` directly is fine
there -- a local shell returning promptly after pytest's own summary is
exactly the observed *local* behavior on macOS/Linux; this gap is
CI/Windows-specific). Only the CI workflow (``.github/workflows/ci.yml``)
invokes this script.
"""

from __future__ import annotations

import os
import sys

import pytest


def main() -> None:
    code = pytest.main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(code))


if __name__ == "__main__":
    main()
