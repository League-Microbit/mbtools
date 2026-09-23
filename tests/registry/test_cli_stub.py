from __future__ import annotations

import pytest

from mbtools.registry.cli import main


def test_mbregistry_stub_exits_nonzero_and_names_ticket(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "mbregistry" in captured.err
    assert "ticket 009" in captured.err
