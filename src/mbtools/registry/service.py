"""mbtools.registry.service — install/uninstall/status the ``mbregistry``
service, at whichever platform/scope is asked for.

Per sprint.md's Architecture (Step 3, module ``registry.service``), this
module owns plist/unit/udev-rule *rendering*, the command-runner seam,
and per-platform/scope *orchestration* — everything responsibility 4
("orchestrating install/uninstall/status per platform+scope") in Step 2
needs, sitting between ``registry.cli`` (presentation, calls in) and
``registry.paths`` (location knowledge, called out to). See that
section's component diagram for the shape of the whole graph this module
sits in.

**This ticket (006-001) is the foundation only.** It adds exactly one
thing — the command-execution seam every real orchestration function in
tickets 006-002 (macOS launchd) and 006-003 (Linux systemd) will call
through, instead of shelling out itself. Nothing here renders a plist or
unit file, and nothing here is called from anywhere real yet (not even
``registry.cli``) — those are 002/003's and 004's jobs. Matches
sprint.md's Design Rationale ("a single injectable command-runner seam,
not per-tool mocks"): one seam, a real implementation by default, a
dry-run double that produces the exact same "here's what would run" text
``--dry-run`` needs, and every future orchestration function takes (or
defaults) a runner parameter — the same injectable-seam convention
``registry.flash``'s ``runner``/``registry.usbwatch``'s ``PortWatcher``
already use in this package.

No test in this module's own test suite invokes a real external command
— see :class:`SubprocessCommandRunner`'s docstring for why that
guarantee is safe to state even though it is the "real" implementation.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from typing import IO, Protocol, runtime_checkable

__all__ = [
    "CommandRunner",
    "SubprocessCommandRunner",
    "DryRunCommandRunner",
    "default_runner",
]


@runtime_checkable
class CommandRunner(Protocol):
    """The one command-execution seam every ``registry.service``
    orchestration function (tickets 006-002/003) calls through, instead
    of shelling out itself. Deliberately narrow — a single method, no
    knowledge of *which* command it is running (``launchctl``,
    ``systemctl``, ``udevadm``, ``usermod``, ``loginctl`` are all just
    ``argv`` to this interface) — so :class:`DryRunCommandRunner` can
    stand in for :class:`SubprocessCommandRunner` at exactly the same
    call sites, and a test can inject its own fake the same way
    ``registry.flash.FlashOp``'s ``runner`` parameter already does.
    """

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` and return a
        :class:`subprocess.CompletedProcess`-shaped result (``args``,
        ``returncode``, ``stdout``, ``stderr``) so a caller that needs
        the output — e.g. ``launchctl print``/``systemctl --user
        is-active`` for :func:`~mbtools.registry.service` status
        reporting (ticket 006-002/003) — can read it. A caller that only
        cares whether the command succeeded checks ``.returncode``
        itself; this seam never raises on a nonzero exit by itself (see
        :class:`SubprocessCommandRunner`'s own ``check`` parameter for
        where that choice is made).
        """
        ...


class SubprocessCommandRunner:
    """The real, production :class:`CommandRunner`: shells out via
    :func:`subprocess.run`.

    ``check=True`` (the default) raises :class:`subprocess.CalledProcessError`
    on a nonzero exit, matching this package's other real-implementation
    seams (e.g. ``registry.flash``'s pyocd invocation treats a nonzero
    exit as a flash failure, not a silent return) — an orchestration
    function that wants to inspect a failing exit code instead of having
    it raised (e.g. "is the unit loaded" status checks, which are
    expected to fail cleanly when nothing is installed) constructs its
    own instance with ``check=False``.

    Never constructed or ``.run()``-called by this ticket's own tests —
    only :class:`DryRunCommandRunner` and hand-rolled fakes are, per the
    Test Strategy's "no test invokes a real external command." A future
    ticket (006-002/003) is free to unit-test this class itself against
    a harmless real command (e.g. ``["true"]``/``["echo", "hi"]``) if it
    wants coverage of the ``subprocess.run`` call itself, but that is not
    this ticket's job.
    """

    def __init__(self, *, check: bool = True) -> None:
        self._check = check

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            check=self._check,
            capture_output=True,
            text=True,
        )


class DryRunCommandRunner:
    """``--dry-run``'s :class:`CommandRunner`: never executes ``argv`` —
    prints ``would run: <command>`` to ``stream`` (stderr by default, so
    it never pollutes a machine-readable stdout, e.g. ``status``'s
    ``--json`` output) and returns a synthetic, always-zero-exit
    :class:`subprocess.CompletedProcess` instead.

    This is what lets every orchestration function in tickets 002/003
    have exactly one code path for "render, write, run commands" — told
    to print instead of execute, rather than a parallel dry-run branch
    that could silently drift from what real execution does (sprint.md's
    Design Rationale, same section as :class:`CommandRunner`'s own
    docstring). The synthetic result's always-zero ``returncode`` means
    an orchestration function must not branch on the *result* of a
    dry-run command to decide what to do next — it already knows it is
    in dry-run mode by construction (it chose this runner), so nothing
    in 002/003 should need to.
    """

    def __init__(self, *, stream: IO[str] | None = None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        print(f"would run: {shlex.join(argv)}", file=self._stream)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def default_runner(*, dry_run: bool = False) -> CommandRunner:
    """The one place that turns ``--dry-run`` into "which
    :class:`CommandRunner`": :class:`DryRunCommandRunner` when
    ``dry_run`` is true, :class:`SubprocessCommandRunner` otherwise.
    Tickets 002/003's orchestration functions take a
    ``runner: CommandRunner | None = None`` parameter (matching
    ``registry.flash.FlashOp``'s ``runner``-parameter convention) and
    call this to fill it in when no runner is injected — a test always
    injects its own fake instead, so this function itself is only ever
    exercised by production code and by this ticket's own unit tests
    asserting it picks the right class.
    """
    return DryRunCommandRunner() if dry_run else SubprocessCommandRunner()
