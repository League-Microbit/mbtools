#!/usr/bin/env bash
# scripts/deploy-test-host.sh <host>
#
# Ticket 010's repeatable deploy path: build a wheel from the current
# working tree and install it into a dedicated venv on one of the
# hardware test targets listed in CLAUDE.md's "Hardware test targets"
# (meili, loki, hodr, magni, braeburn), over SSH.
#
# Usage:
#   scripts/deploy-test-host.sh <host>
#
# What it does, in order:
#   1. `uv build --wheel` the working tree locally into dist/.
#   2. scp the wheel to ~/mbtools-deploy/ on <host>.
#   3. Ensure `uv` is present on <host> -- installs it via the official
#      installer (curl -LsSf https://astral.sh/uv/install.sh | sh) if
#      `uv` is not already on the host's PATH. braeburn already has uv
#      (per CLAUDE.md); the Nolanet nodes (meili/loki/hodr/magni)
#      typically do not.
#   4. `uv venv` + `uv pip install <wheel>` into a venv on the host.
#      Idempotent -- re-running replaces the venv's mbtools install with
#      the freshly built wheel without disturbing anything else on the
#      host. Normally this venv lives at ~/mbtools-venv. If the host's
#      home filesystem reports under 200MB available (observed on
#      meili, whose root filesystem was already at 100% from unrelated
#      pre-existing docker/containerd usage -- out of this script's
#      scope to reclaim), it falls back to a tmpfs location
#      (/tmp/mbtools-venv-<user>) instead, printing a warning. A tmpfs
#      venv does not survive a reboot; that's an acceptable tradeoff for
#      a hardware-acceptance test run (see docs/acceptance/001-hardware.md
#      for which hosts used the fallback).
#
# This script only builds and installs the mbtools package into a venv.
# It does not start, stop, enable, or disable any service, and it does
# not touch the old mbdeploy install -- those are ticket 010's own
# separate steps (see docs/acceptance/001-hardware.md).
#
# Requires: `uv` on the machine running this script (to build the
# wheel), `ssh`/`scp` access to <host> per CLAUDE.md ("Hardware test
# targets" -- user eric, passwordless sudo, BatchMode-capable).

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <host>" >&2
    echo "  <host> one of: meili loki hodr magni braeburn" >&2
    exit 1
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
fi

HOST="${1:?usage: deploy-test-host.sh <host>}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 ${HOST}"

echo "==> [$HOST] building wheel from working tree" >&2
cd "$REPO_ROOT"
uv build --wheel >&2

WHEEL="$(ls -t dist/*.whl | head -n1)"
WHEEL_NAME="$(basename "$WHEEL")"
echo "==> [$HOST] built $WHEEL_NAME" >&2

# Low-space fallback: if $HOME's filesystem has under 200MB available,
# use tmpfs locations instead (see usage comment above), for the wheel
# staging dir, the venv itself, AND uv's own binary (it also normally
# installs under $HOME/.local/bin -- observed failing with the same
# "No space left on device" on meili until this was added). 200MB
# comfortably covers the wheel plus pyocd's dependency closet (~55MB
# observed) plus uv's own binary (~30MB).
AVAIL_KB="$($SSH 'df -Pk "$HOME" | tail -1' | awk '{print $4}')"
if [ "${AVAIL_KB:-999999}" -lt 204800 ]; then
    echo "==> [$HOST] only ${AVAIL_KB}KB available under \$HOME -- falling back to tmpfs" >&2
    REMOTE_DIR='/tmp/mbtools-deploy-$(id -un)'
    REMOTE_VENV='/tmp/mbtools-venv-$(id -un)'
    UV_BIN_DIR='/tmp/mbtools-uv-bin-$(id -un)'
    UV_CACHE_DIR='/tmp/mbtools-uv-cache-$(id -un)'
else
    REMOTE_DIR='$HOME/mbtools-deploy'
    REMOTE_VENV='$HOME/mbtools-venv'
    UV_BIN_DIR='$HOME/.local/bin'
    UV_CACHE_DIR='$HOME/.cache/uv'
fi

echo "==> [$HOST] copying wheel" >&2
$SSH "mkdir -p ${REMOTE_DIR}"
REMOTE_DIR_EXPANDED="$($SSH "echo ${REMOTE_DIR}")"
scp -o BatchMode=yes -q "$WHEEL" "${HOST}:${REMOTE_DIR_EXPANDED}/${WHEEL_NAME}"

echo "==> [$HOST] ensuring uv is present" >&2
if ! $SSH "command -v uv >/dev/null 2>&1 || [ -x ${UV_BIN_DIR}/uv ]"; then
    echo "==> [$HOST] uv not found, installing via official installer" >&2
    # UV_NO_MODIFY_PATH: this script never relies on the installer's own
    # rcfile edit (it always sets PATH explicitly below) -- and on a
    # low-space host that edit fails outright (observed on meili: the
    # installer unconditionally tries to append to $HOME/.bashrc/.profile
    # even when UV_INSTALL_DIR points elsewhere, and that write fails
    # with an I/O error when $HOME's filesystem is full).
    $SSH "UV_INSTALL_DIR=${UV_BIN_DIR} UV_NO_MODIFY_PATH=1 sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'"
fi

echo "==> [$HOST] creating venv and installing mbtools" >&2
$SSH bash -s <<REMOTE
set -euo pipefail
export PATH="${UV_BIN_DIR}:\$HOME/.local/bin:\$PATH"
export UV_CACHE_DIR="${UV_CACHE_DIR}"
cd "${REMOTE_DIR}"
uv venv --clear "${REMOTE_VENV}"
uv pip install --python "${REMOTE_VENV}/bin/python" --force-reinstall "${WHEEL_NAME}"
"${REMOTE_VENV}/bin/python" -m mbtools.registry.cli --help >/dev/null
echo "==> [$HOST] mbtools installed at ${REMOTE_VENV}"
REMOTE

REMOTE_VENV_EXPANDED="$($SSH "echo ${REMOTE_VENV}")"
echo "==> [$HOST] deploy complete: ${REMOTE_VENV_EXPANDED}/bin/mbregistry" >&2
