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


def test_unable_to_claim_interface_is_treated_as_transient():
    """The registry's own identity read briefly opens the CMSIS-DAP
    interface (now isolated in a subprocess, released on exit). A flash
    landing in that window sees 'Unable to claim interface'; it must be
    retried once, not treated as a hard failure."""
    assert flashlogic._looks_transient(
        "0000517 C Unable to claim interface for probe 99063602...6e052820 [__main__]"
    )
    # ...and it must not be mistaken for a locked-device (mass-erase) signature.
    assert not flashlogic._looks_locked("Unable to claim interface for probe")


_IOREG_WITH_CHROME = '''
    | |   "USB Serial Number" = "9906360200052820647cd2d582b01c8c000000006e052820"
    | |   "IOUserClientCreator" = "pid 10178, Google Chrome"
    | |   "USB Serial Number" = "9906360200052820deadbeefdeadbeef000000006e052820"
    | |   "IOUserClientCreator" = "pid 999, Something Else"
'''

_IOREG_ONLY_OUR_PYTHON = '''
    | |   "USB Serial Number" = "9906360200052820647cd2d582b01c8c000000006e052820"
    | |   "IOUserClientCreator" = "pid 67936, Python"
'''

_IOREG_OTHER_APP = '''
    | |   "USB Serial Number" = "9906360200052820647cd2d582b01c8c000000006e052820"
    | |   "IOUserClientCreator" = "pid 321, OpenOCD"
'''

_UID = "9906360200052820647cd2d582b01c8c000000006e052820"


def test_unable_to_claim_signature_detected():
    assert flashlogic._looks_claim_blocked(
        "0000517 C Unable to claim interface for probe 99063602...6e052820 [__main__]"
    )
    assert not flashlogic._looks_claim_blocked("0030781 I Programming... [loader]")


def _patch_ioreg(monkeypatch, stdout, *, darwin=True):
    monkeypatch.setattr(flashlogic.sys, "platform", "darwin" if darwin else "linux")

    class _Proc:
        pass

    proc = _Proc()
    proc.stdout = stdout
    monkeypatch.setattr(flashlogic.subprocess, "run", lambda *a, **k: proc)


def test_usb_interface_blocker_names_a_browser(monkeypatch):
    _patch_ioreg(monkeypatch, _IOREG_WITH_CHROME)
    msg = flashlogic.usb_interface_blocker(_UID)
    assert msg is not None and "WebUSB" in msg and "browser tab" in msg


def test_usb_interface_blocker_ignores_our_own_python_reads(monkeypatch):
    _patch_ioreg(monkeypatch, _IOREG_ONLY_OUR_PYTHON)
    assert flashlogic.usb_interface_blocker(_UID) is None


def test_usb_interface_blocker_names_a_non_browser_program(monkeypatch):
    _patch_ioreg(monkeypatch, _IOREG_OTHER_APP)
    msg = flashlogic.usb_interface_blocker(_UID)
    assert msg is not None and "OpenOCD" in msg


def test_usb_interface_blocker_is_none_off_macos(monkeypatch):
    _patch_ioreg(monkeypatch, _IOREG_WITH_CHROME, darwin=False)
    assert flashlogic.usb_interface_blocker(_UID) is None
