#!/bin/sh
# install-mbtools.sh -- install or update mbtools from its GitHub release.
#
#   curl -fsSL https://raw.githubusercontent.com/League-Microbit/mbtools/main/packaging/deb/install-mbtools.sh | sudo sh
#   sudo sh install-mbtools.sh [VERSION]      # e.g. 0.20260926.1; default: latest
#
# Idempotent: does nothing when that version is already installed, so it is
# safe to run from cron/a systemd timer/ansible on every fleet host. The
# package itself restarts mbregistry.service after an upgrade.
set -eu

REPO="League-Microbit/mbtools"
VERSION="${1:-latest}"
ARCH="$(dpkg --print-architecture)"

if [ "$(id -u)" -ne 0 ]; then
    echo "install-mbtools: run as root (sudo)" >&2
    exit 1
fi

if [ "$VERSION" = "latest" ]; then
    URL="https://github.com/$REPO/releases/latest/download/mbtools_${ARCH}.deb"
else
    URL="https://github.com/$REPO/releases/download/v$VERSION/mbtools_${VERSION}_${ARCH}.deb"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
DEB="$TMP/mbtools.deb"

echo "install-mbtools: fetching $URL"
curl -fsSL -o "$DEB" "$URL"

NEW="$(dpkg-deb -f "$DEB" Version)"
OLD="$(dpkg-query -W -f '${Version}' mbtools 2>/dev/null || true)"
if [ "$NEW" = "$OLD" ]; then
    echo "install-mbtools: mbtools $OLD already installed"
    exit 0
fi

echo "install-mbtools: installing mbtools $NEW (was: ${OLD:-not installed})"
DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "$DEB"
