"""mbtools — device-registry daemon and client tools for BBC micro:bit fleets.

See ``clasi/sprints/001-mbregistry-core-local-device-registry-daemon/sprint.md``
for the architecture this package is being built toward: a single
``mbregistry`` daemon owning USB enumeration, identity, and locks, with
``mbdeploy``/``mbserial``/``mbrelay`` as clients of it.
"""
