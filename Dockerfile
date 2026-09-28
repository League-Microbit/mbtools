# mbregistry container image (sprint 009).
#
# Runs mbregistry as the long-running daemon by default; mbdeploy/
# mbserial/mbrelay are also on PATH for `docker run --entrypoint ...`
# or `docker exec` against a running container. Single-stage: this
# image has no build tooling worth stripping out in a second stage
# (pyocd/zeroconf/pyzmq are pure-Python-installable wheels on the
# platforms this project targets), so multi-stage would only add
# complexity for no real size win -- revisit if that stops being true.
#
# See compose.yaml for the supported run recipe (host networking, USB
# device access, persistent volumes) and docs/docker.md (ticket
# 009-003) for the full mechanics/limitations writeup.

FROM python:3.13-slim

# libusb-1.0-0 is pyocd's CMSIS-DAP v2 USB backend dependency; udev is
# needed for USB device enumeration. Same two packages
# packaging/deb/control.in already depends on for the same reason, so
# this isn't a fresh guess -- it's the existing native-install
# dependency list, confirmed against pyocd's own install requirements.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libusb-1.0-0 \
        udev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src

# Build from the repo source (this project isn't published to PyPI).
# .dockerignore keeps the build context to just what's needed here.
COPY . .
RUN pip install --no-cache-dir .

# Container runs as root (default) -- matches mbregistry.service's own
# privilege level, needed for raw USB/CMSIS-DAP access.

ENTRYPOINT ["mbregistry"]
CMD ["run"]
