#!/usr/bin/env bash
# packaging/deb/build-deb.sh -- build dist/mbtools_<version>_<arch>.deb
#
# The package is self-contained: it carries its own CPython (uv's
# python-build-standalone) and a venv holding mbtools and every dependency,
# all under /usr/lib/mbtools. It doesn't depend on the host's python3, so
# one .deb per architecture installs on Ubuntu 22.04/24.04 and Debian 12/13
# alike.
#
# The venv has to be built at its final path (/usr/lib/mbtools), because
# venv scripts and the interpreter symlink hold absolute paths. So this
# script needs write access to /usr/lib/mbtools (it uses sudo to create
# and chown it), and is meant for a throwaway CI runner or a build box,
# not a host that already has the package installed.
#
# Usage: packaging/deb/build-deb.sh            (run from anywhere in the repo)
# Needs: uv, dpkg-deb, sudo. Output: dist/mbtools_<version>_<arch>.deb

set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO"

PYTHON_VERSION="${MBTOOLS_PYTHON_VERSION:-3.13}"
PREFIX=/usr/lib/mbtools
VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -1)"
ARCH="$(dpkg --print-architecture)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> building mbtools $VERSION for $ARCH"

rm -rf dist
uv build --wheel --out-dir dist

sudo rm -rf "$PREFIX"
sudo mkdir -p "$PREFIX"
sudo chown "$(id -u):$(id -g)" "$PREFIX"

# Bundled interpreter + venv, at the final install path.
UV_PYTHON_INSTALL_DIR="$PREFIX/python" uv python install "$PYTHON_VERSION"
PY="$(UV_PYTHON_INSTALL_DIR="$PREFIX/python" uv python find --managed-python "$PYTHON_VERSION")"
uv venv --python "$PY" "$PREFIX/venv"
# --compile-bytecode: the package ships the .pyc files, so the root daemon
# never writes __pycache__ of its own into a dpkg-owned tree.
uv pip install --python "$PREFIX/venv/bin/python" --link-mode copy --compile-bytecode dist/mbtools-*.whl
"$PREFIX/venv/bin/mbregistry" --version

# Trim what a headless daemon never uses from the bundled interpreter: the
# stdlib test suite, Tk/IDLE, static libraries and C headers.
PYHOME="$(dirname "$(dirname "$(readlink -f "$PY")")")"
rm -rf "$PYHOME"/lib/python3.*/test "$PYHOME"/lib/python3.*/idlelib \
  "$PYHOME"/lib/python3.*/tkinter "$PYHOME"/lib/python3.*/turtledemo \
  "$PYHOME"/lib/python3.*/lib-dynload/_tkinter* "$PYHOME"/lib/libtcl* "$PYHOME"/lib/libtk* \
  "$PYHOME"/lib/tcl* "$PYHOME"/lib/tk* "$PYHOME"/include
find "$PYHOME" -name '*.a' -delete
"$PREFIX/venv/bin/mbregistry" --version

# -- stage the package tree --------------------------------------------------
mkdir -p "$STAGE/usr/lib" "$STAGE/usr/bin" "$STAGE/DEBIAN" \
  "$STAGE/usr/lib/systemd/system" "$STAGE/usr/lib/udev/rules.d" \
  "$STAGE/usr/share/doc/mbtools"
cp -a "$PREFIX" "$STAGE/usr/lib/"
for cmd in mbregistry mbdeploy mbserial mbrelay; do
  ln -s "$PREFIX/venv/bin/$cmd" "$STAGE/usr/bin/$cmd"
done

# The unit and udev rule come from mbtools itself, so the package ships
# exactly what `mbregistry service install --system` would write.
"$PREFIX/venv/bin/python" - "$STAGE" "$PREFIX" <<'EOF'
import sys
from pathlib import Path
from mbtools.registry.service import render_systemd_unit, render_udev_rule

stage, prefix = Path(sys.argv[1]), sys.argv[2]
unit = render_systemd_unit(f"{prefix}/venv/bin/python -m mbtools.registry.cli service run")
(stage / "usr/lib/systemd/system/mbregistry.service").write_text(unit)
(stage / "usr/lib/udev/rules.d/99-mbregistry-cmsis-dap.rules").write_text(render_udev_rule())
EOF

cp README.md "$STAGE/usr/share/doc/mbtools/"
cp packaging/deb/postinst packaging/deb/prerm packaging/deb/postrm "$STAGE/DEBIAN/"
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

INSTALLED_KB="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
sed -e "s/@VERSION@/$VERSION/" -e "s/@ARCH@/$ARCH/" -e "s/@INSTALLED_KB@/$INSTALLED_KB/" \
  packaging/deb/control.in > "$STAGE/DEBIAN/control"

OUT="dist/mbtools_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT"
echo "==> built $OUT"
dpkg-deb --info "$OUT"
