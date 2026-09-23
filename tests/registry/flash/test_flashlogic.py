"""Smoke test for mbtools.registry.flashlogic's re-export identity.

Ticket 003's own acceptance criteria: ``flash_hex``/``DEFAULT_MCU`` must
be importable, and identical objects, from both
``mbtools.registry.flashlogic`` (the real implementation, moved here in
this ticket) and ``mbtools.deploy.flash`` (the thin re-export shim left
behind so no existing local-path import needs to change). Behavioral
coverage of ``flash_hex`` itself already lives in
``tests/deploy/test_deploy_flash.py`` and is run, per the ticket's own
Testing section, against both import paths -- this file only guards
against the two paths silently drifting apart (e.g. someone redefining
``flash_hex`` a second time in one module instead of importing it).
"""

from __future__ import annotations

from mbtools.deploy import flash as deploy_flash
from mbtools.registry import flashlogic


def test_flash_hex_is_the_same_object_via_both_import_paths():
    assert deploy_flash.flash_hex is flashlogic.flash_hex


def test_default_mcu_is_the_same_object_via_both_import_paths():
    assert deploy_flash.DEFAULT_MCU is flashlogic.DEFAULT_MCU
