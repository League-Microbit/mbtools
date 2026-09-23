from __future__ import annotations

from mbtools.common import PortInfo


def test_port_info_is_a_frozen_dataclass_with_expected_fields():
    info = PortInfo(uid="abc123", port="/dev/ttyACM0", vid=0x0D28, pid=0x0204)
    assert info.uid == "abc123"
    assert info.port == "/dev/ttyACM0"
    assert info.vid == 0x0D28
    assert info.pid == 0x0204
