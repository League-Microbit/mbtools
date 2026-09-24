"""Cross-module import-safety regression test.

Sprint 005 ticket 003's own scope is `registry.api_windows`, but the
team-lead's dispatch for that ticket carried an additional item: ticket
002 left `registry.api.DEFAULT_SOCKET_PATH` computed by calling
`registry.paths.default_socket_path()` unconditionally at module import
time; that function raises `NotImplementedError` on `sys.platform ==
"win32"` (deliberately, per ticket 002's own scope -- see that ticket's
Implementation Notes, "Flag for ticket 005/006"). Since
`registry.client`, `registry.cli`, `deploy.cli`, `relay.cli`, and
`serial.cli` all import `DEFAULT_SOCKET_PATH` from `registry.api` at
*their own* module scope, simply `import`ing any one of them on a real
Windows interpreter would have raised before any Windows-aware code in
this sprint ever ran. Ticket 003 fixes the root cause
(`registry.api.DEFAULT_SOCKET_PATH` is now `None` on `sys.platform ==
"win32"`, not a call that raises -- see that module's own docstring on
the constant) and this test is the regression guard for it, across
every mbtools module, not just the ones ticket 002 happened to name.

**Why "monkeypatch sys.platform and import" doesn't just work naively**:
flipping `sys.platform` on an interpreter that is *actually* built for
macOS/Linux, then doing a genuinely first-time `import` of a module
that itself transitively imports a stdlib module (e.g. `subprocess`)
for the first time, makes *that* stdlib module try to import a
Windows-only C extension (`_winapi`) that plain doesn't exist in this
build -- ``ModuleNotFoundError: No module named '_winapi'``. That
failure is a property of this test machine, not a bug in any mbtools
module (confirmed by hand while writing this test: importing everything
fresh with `sys.platform` pre-set to `"win32"` reliably reproduces it
for `subprocess`-touching modules). The fix used below: import every
module normally first (warms the stdlib/third-party import cache under
the *real* platform), *then* flip `sys.platform`, *then*
`importlib.reload()` only the mbtools modules under test -- their own
`import subprocess`-style statements then hit the already-warm cache
and never re-trigger stdlib's own platform dispatch.

**`mbtools.registry.api_windows` is excluded** from the reload list:
its own module-scope guard (``if sys.platform == "win32": _kernel32 =
ctypes.windll.kernel32 ...``) is *supposed* to reach for
``ctypes.windll``, which is a real capability gap on this machine (not
present on macOS/Linux at all, regardless of caching) -- reloading it
under a merely-simulated ``sys.platform`` raises ``AttributeError``
here even though the exact same code is correct on real Windows. That
module's own import-safety under the *real* (non-simulated) platform is
covered directly in ``tests/registry/api_windows/test_api_windows.py``
(``test_module_imports_cleanly_off_windows``); its real-Windows branch
is first verified by ticket 006's ``windows-latest`` CI job, which runs
on an interpreter where this problem doesn't exist.

**Cleanup discipline**: every test here restores ``sys.platform`` and
reloads every module it touched, in a ``finally`` block, *before*
returning control to the rest of the suite -- a module left holding
simulated-Windows values (e.g. ``registry.api.DEFAULT_SOCKET_PATH is
None``) would silently break unrelated tests that run later in the same
process.
"""

from __future__ import annotations

import contextlib
import importlib
import pkgutil
import sys

import mbtools
import mbtools.registry.api
import mbtools.registry.paths


def _discover_mbtools_modules() -> list[str]:
    names = []
    for info in pkgutil.walk_packages(mbtools.__path__, prefix="mbtools."):
        if info.name == "mbtools.registry.api_windows":
            continue  # see module docstring
        names.append(info.name)
    return sorted(names)


@contextlib.contextmanager
def _simulated_windows(modules):
    """Sets ``sys.platform = "win32"`` for the ``with`` block, then
    always restores the real platform and reloads every module in
    ``modules`` before returning -- see module docstring's "Cleanup
    discipline". Plain ``try``/``finally`` on ``sys.platform`` directly
    (not ``monkeypatch``), so this generator controls the exact
    ordering of "restore platform" vs. "reload modules" itself, rather
    than depending on fixture-teardown ordering between two independent
    fixtures.
    """
    real_platform = sys.platform
    sys.platform = "win32"
    try:
        yield
    finally:
        sys.platform = real_platform
        for module in modules:
            importlib.reload(module)


def test_every_mbtools_module_reimports_cleanly_on_simulated_windows():
    """The general regression guard: every discovered mbtools module
    (except `registry.api_windows` -- see module docstring) must
    re-execute its own top-level code without raising when
    `sys.platform` is `"win32"`.
    """
    module_names = _discover_mbtools_modules()
    modules = [importlib.import_module(name) for name in module_names]  # warm the cache

    failed: list[tuple[str, str]] = []
    with _simulated_windows(modules):
        for module in modules:
            try:
                importlib.reload(module)
            except Exception as exc:  # collecting every failure, not just the first
                failed.append((module.__name__, repr(exc)))

    assert not failed, "modules that raised on simulated-Windows (re)import:\n" + "\n".join(
        f"  {name}: {err}" for name, err in failed
    )


def test_registry_api_default_socket_path_is_none_on_simulated_windows():
    """The concrete regression this ticket fixes -- see module
    docstring's opening paragraph."""
    api = mbtools.registry.api
    with _simulated_windows([api]):
        importlib.reload(api)
        assert api.DEFAULT_SOCKET_PATH is None


def test_registry_paths_default_socket_path_still_raises_on_simulated_windows():
    """Ticket 002's own decision (`paths.default_socket_path()` itself
    still raises `NotImplementedError` when actually *called* on
    Windows) is unchanged by ticket 003's fix -- only `api
    .DEFAULT_SOCKET_PATH`'s *module-scope, unconditional* call to it was
    the bug, not the function's own contract. `default_db_path()`/
    `default_pipe_name()` are unaffected either way (ticket 002 never
    made those raise).
    """
    import pytest

    paths = mbtools.registry.paths
    with _simulated_windows([paths]):
        importlib.reload(paths)
        with pytest.raises(NotImplementedError):
            paths.default_socket_path()
        assert paths.default_db_path() is not None
        assert paths.default_pipe_name() == r"\\.\pipe\mbregistry"


def test_simulated_windows_state_does_not_leak_between_tests():
    """Guards the guard: proves `_simulated_windows`'s own restoration
    actually works, so the three tests above (and any future one using
    it) can't silently poison later tests in this same pytest process
    with a lingering `DEFAULT_SOCKET_PATH is None`.
    """
    api = mbtools.registry.api
    real_platform = sys.platform
    real_default = api.DEFAULT_SOCKET_PATH

    with _simulated_windows([api]):
        importlib.reload(api)
        assert api.DEFAULT_SOCKET_PATH is None

    assert sys.platform == real_platform
    assert api.DEFAULT_SOCKET_PATH == real_default
