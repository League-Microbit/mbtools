"""deploy.flash — thin re-export shim over the shared flash implementation.

The real implementation (``flash_hex`` and its transient/locked-signature
matching, mass-erase recovery, and streamed-subprocess helpers) moved to
``mbtools.registry.flashlogic`` in sprint 003 (see that sprint's ticket
003, Decision 4): both the local flash path (this module, unchanged for
every caller -- ``deploy.cli``) and the new remote flash path
(``registry.remote_api``) need to call the exact same implementation, so
it now lives in one place both can reach without ``mbtools.deploy``
depending on the registry's caller-facing pieces and vice versa.

This module exists only so every pre-existing ``from mbtools.deploy.flash
import flash_hex`` call site keeps working unchanged -- it is a pure
re-export, not a copy: ``flash_hex`` and ``DEFAULT_MCU`` here are the
exact same objects as ``mbtools.registry.flashlogic``'s (see
``test_default_mcu_reused_from_registry_flash``-style identity tests in
``tests/deploy/test_deploy_flash.py`` and
``tests/registry/flash/test_flashlogic.py``). If you're reading this
looking for the actual retry/mass-erase logic, it's in
``mbtools.registry.flashlogic``, not here.
"""

from __future__ import annotations

from mbtools.registry.flashlogic import DEFAULT_MCU, flash_hex

__all__ = [
    "DEFAULT_MCU",
    "flash_hex",
]
