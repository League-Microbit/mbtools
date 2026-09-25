"""Tests for ticket 008-006's top-level ``mbregistry --version`` flag.

robot-console checks a minimum ``mbregistry`` version before spawning or
using it (this ticket's own motivation). The matching ``"version"`` key
in ``--ready-json``'s payload -- which must agree with this flag -- is
covered alongside the rest of ``--ready-json``'s shape in
``test_cli_spawn.py`` (``test_run_registry_ready_json_includes_version_key``),
since that module already has the fully-mocked
``assemble_registry``/``assemble_relay_pool``/``assemble_names_api``
fixtures needed to exercise it without real sockets/ports.

No real sockets/ports here either: ``--version`` is exercised via
``main()`` (the ``action="version"`` argparse action exits before any
subcommand runs, so no ``--socket``/``--db`` is even needed).
"""

from __future__ import annotations

import importlib.metadata

import pytest

from mbtools.registry import cli as cli_module
from mbtools.registry.cli import build_parser, main


def test_mbtools_version_helper_matches_importlib_metadata():
    assert cli_module._mbtools_version() == importlib.metadata.version("mbtools")


def test_version_flag_prints_mbregistry_and_version_and_exits_ok(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0

    out = capsys.readouterr().out
    assert out.strip() == f"mbregistry {cli_module._mbtools_version()}"


def test_version_flag_works_without_a_subcommand():
    """The top-level ``--version`` flag must not require ``command`` to
    be supplied, even though every subparser (``list``/``run``/...) is
    otherwise ``required=True`` -- argparse's ``action="version"`` exits
    before that check runs."""
    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["--version"])
    assert excinfo.value.code == 0
