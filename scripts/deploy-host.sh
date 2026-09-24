#!/usr/bin/env bash
# scripts/deploy-host.sh <host>
#
# Ticket 007's idempotent fleet-deployment tooling: goes beyond
# scripts/deploy-test-host.sh's build-wheel-and-install-into-venv (sprint
# 004's precedent, which this script calls rather than reimplementing --
# see its own usage comment) by also running `mbregistry install-service`
# (systemd unit + udev rule) and the systemctl/udevadm/usermod follow-up
# commands that command prints rather than runs, so a run against a host
# ends with a running, restart-on-failure `mbregistry.service` and correct
# USB permissions -- not just an installed package.
#
# Usage:
#   scripts/deploy-host.sh <host>
#
# Fixed host list -- exactly these six, no flag or default lets this
# script target anything else:
#   meili loki hodr magni braeburn torture
#
# meili/loki/hodr/magni are the Nolanet test nodes (Linux/systemd).
# braeburn is the macOS test target: mbregistry has no service-manager
# story there (module docstring in registry/cli.py, "macOS foreground dev
# use") -- this script installs the wheel and restarts the existing
# foreground `mbregistry run` process in place (via sudo, matching how it
# is already run on that host today) instead of running install-service,
# which would try to write a systemd unit that host has no systemctl to
# load.
#
# torture is the sixth host (stakeholder decision, 2026-09-24 -- sprint
# 005's own sprint.md still says "never torture"; this script's inclusion
# of it is a deliberate, recorded scope change, not an oversight -- see
# ticket 007's Implementation Notes). It is the former production relay
# host: it still runs the legacy `mbrelay.service`
# (/usr/local/bin/mbrelay, ports 8760/8761), which currently holds all of
# its RADIOBRIDGE relays. Before doing anything else, this script's
# torture-only, one-time (but idempotent -- safe to re-run) step stops
# and disables that unit, WITHOUT deleting its unit file or the
# /usr/local/bin/mbrelay binary -- both are left on disk for rollback.
# mbregistry then takes over the relays with the same install-service
# path every other Linux host gets, and comes up with the robot-console
# compatibility pool enabled (the default -- `mbregistry run` starts it
# unless `--no-relay-pool` is passed, which this script never passes).
# Anything that used to reach torture's relays over the legacy
# mbrelay/8760 protocol must rediscover it over mDNS (_mbregistry._tcp)
# instead; that migration is outside this script's job.
#
# Never point this script at a host outside the six above. Never re-flash
# torture's relays with other firmware as a side effect of testing this
# script (project CLAUDE.md's standing hardware-testing rule) -- this
# script never touches board firmware, only the host-side daemon.
#
# Idempotency (the hard requirement -- ticket 007's Acceptance Criteria):
# re-running this script against an already-provisioned, already-healthy
# host must not disrupt the running service and must not duplicate the
# udev rule/systemd unit. This script tracks a content hash of the
# working tree (`git rev-parse HEAD` + `git diff HEAD`) in a small marker
# file on the host (`~/.mbtools-deploy-state`); when the host's marker
# already matches the local tree, the disruptive steps -- stopping
# mbregistry.service (or, on braeburn, killing the running process),
# rebuilding the wheel, and reinstalling the venv -- are skipped
# entirely, so the running daemon is never touched. `install-service` and
# its systemctl/udevadm/usermod follow-ups are still run every time (they
# write deterministic content and are no-ops on an already-correct,
# already-running host -- the same "print, don't disrupt" idempotency
# `install-service` itself already documents), which also self-heals any
# unit/udev-rule drift without needing a rebuild. When nothing changed,
# this script asserts the service's own `ActiveEnterTimestamp` (Linux) or
# process start time (braeburn) is identical before and after, and fails
# loudly if it is not -- that assertion is this script's own proof that
# a same-state re-run did not restart anything.
#
# Requires: `uv`/`git` on the machine running this script (used by
# scripts/deploy-test-host.sh, which this script calls), `ssh`/`scp`
# access to <host> per CLAUDE.md's hardware test hosts table (user
# `eric`, passwordless sudo, BatchMode-capable).

set -euo pipefail

usage() {
    echo "usage: $(basename "$0") <host>" >&2
    echo "  <host> one of: meili loki hodr magni braeburn torture" >&2
    exit 1
}

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    usage
fi

HOST="${1:?usage: deploy-host.sh <host>}"

# Fixed host list -- deliberately not a flag/env override (ticket 007
# Acceptance Criteria: "no flag or default that would let it target an
# arbitrary hostname without deliberate editing"). Editing this array is
# the only way to add a host.
ALLOWED_HOSTS=(meili loki hodr magni braeburn torture)
_host_ok=0
for h in "${ALLOWED_HOSTS[@]}"; do
    if [ "$HOST" = "$h" ]; then
        _host_ok=1
        break
    fi
done
if [ "$_host_ok" -ne 1 ]; then
    echo "deploy-host.sh: refusing unknown host '${HOST}' -- must be one of: ${ALLOWED_HOSTS[*]}" >&2
    exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 ${HOST}"
# /tmp rather than $HOME: meili's root filesystem is observed at 100% full
# (CLAUDE.md, deploy-test-host.sh's own tmpfs-fallback comment), so even a
# few-byte marker file write there can fail with "No space left on
# device". /tmp is tmpfs-backed on the fallback path deploy-test-host.sh
# already relies on and doesn't survive a reboot -- same accepted
# tradeoff that script's own docstring already documents: a reboot makes
# the next run look like "changed" and do a full (harmless) rebuild
# rather than silently skipping a redeploy it can't actually prove is
# still current.
MARKER='/tmp/.mbtools-deploy-state-$(id -un)'

_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum | awk '{print $1}'
    else
        shasum -a 256 | awk '{print $1}'
    fi
}

echo "==> [$HOST] computing local tree state hash" >&2
cd "$REPO_ROOT"
LOCAL_HASH="$( { git rev-parse HEAD; git diff HEAD; } | _sha256 )"
echo "==> [$HOST] local state: ${LOCAL_HASH}" >&2

REMOTE_HASH="$($SSH "cat ${MARKER} 2>/dev/null || true")"
if [ "$REMOTE_HASH" = "$LOCAL_HASH" ]; then
    NEED_REBUILD=0
    echo "==> [$HOST] remote marker matches local state -- skipping rebuild/reinstall" >&2
else
    NEED_REBUILD=1
    echo "==> [$HOST] remote marker (${REMOTE_HASH:-none}) does not match -- will rebuild/reinstall" >&2
fi

IS_MACOS=0
if [ "$HOST" = "braeburn" ]; then
    IS_MACOS=1
fi

# ---------------------------------------------------------------------
# torture only: one-time (idempotent) legacy mbrelay.service retirement,
# before mbregistry ever starts on this host.
# ---------------------------------------------------------------------
if [ "$HOST" = "torture" ]; then
    LEGACY_STATE="$($SSH 'systemctl is-active mbrelay.service 2>/dev/null || true'; )"
    LEGACY_ENABLED="$($SSH 'systemctl is-enabled mbrelay.service 2>/dev/null || true'; )"
    if [ "$LEGACY_STATE" = "active" ] || [ "$LEGACY_ENABLED" = "enabled" ]; then
        echo "==> [$HOST] stopping and disabling legacy mbrelay.service (unit file and binary left in place)" >&2
        $SSH 'sudo systemctl stop mbrelay.service || true; sudo systemctl disable mbrelay.service || true'
    else
        echo "==> [$HOST] legacy mbrelay.service already stopped and disabled -- skipping" >&2
    fi
fi

# ---------------------------------------------------------------------
# braeburn: no service manager -- manage the foreground process by hand.
# ---------------------------------------------------------------------
if [ "$IS_MACOS" -eq 1 ]; then
    RUN_PATTERN="mbtools-venv/bin/mbregistry run"
    BEFORE_START="$($SSH "ps -o lstart= -p \$(pgrep -f '${RUN_PATTERN}' | head -n1) 2>/dev/null || true")"

    if [ "$NEED_REBUILD" -eq 1 ]; then
        # Preserve the exact args the process is currently running with
        # (socket/db paths), falling back to the module docstring's
        # documented macOS invocation if nothing is running yet.
        PREV_ARGS="$($SSH "ps -o command= -p \$(pgrep -f '${RUN_PATTERN}' | head -n1) 2>/dev/null || true")"
        if [ -z "$PREV_ARGS" ]; then
            RUN_ARGS="--socket /tmp/mbregistry/api.sock --db \$HOME/.local/state/mbregistry/devices.db"
        else
            # Strip everything up to and including "mbregistry run".
            RUN_ARGS="${PREV_ARGS#*mbregistry run}"
        fi

        echo "==> [$HOST] stopping running mbregistry process (if any)" >&2
        $SSH "sudo pkill -TERM -f '${RUN_PATTERN}' || true"
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            if [ -z "$($SSH "pgrep -f '${RUN_PATTERN}' || true")" ]; then
                break
            fi
            sleep 1
        done

        echo "==> [$HOST] rebuilding and reinstalling via deploy-test-host.sh" >&2
        "$REPO_ROOT/scripts/deploy-test-host.sh" "$HOST"

        VENV_BIN="\$HOME/mbtools-venv/bin"
        echo "==> [$HOST] relaunching mbregistry run in the background" >&2
        $SSH "nohup sudo ${VENV_BIN}/mbregistry run ${RUN_ARGS} >/tmp/mbregistry-deploy.log 2>&1 < /dev/null & disown"
        sleep 2
        if [ -z "$($SSH "pgrep -f '${RUN_PATTERN}' || true")" ]; then
            echo "==> [$HOST] ERROR: mbregistry did not come back up after redeploy (see /tmp/mbregistry-deploy.log on host)" >&2
            exit 1
        fi
        $SSH "echo '${LOCAL_HASH}' > ${MARKER}"
    else
        if [ -z "$BEFORE_START" ]; then
            echo "==> [$HOST] ERROR: state hash matched but no mbregistry process is running -- refusing to assume idempotency" >&2
            exit 1
        fi
        echo "==> [$HOST] no changes -- confirming the running process was not disturbed" >&2
        AFTER_START="$($SSH "ps -o lstart= -p \$(pgrep -f '${RUN_PATTERN}' | head -n1) 2>/dev/null || true")"
        if [ "$BEFORE_START" != "$AFTER_START" ]; then
            echo "==> [$HOST] ERROR: process start time changed on a no-op run (before='${BEFORE_START}' after='${AFTER_START}')" >&2
            exit 1
        fi
    fi

    echo "==> [$HOST] deploy complete (macOS, foreground process, no service manager)" >&2
    exit 0
fi

# ---------------------------------------------------------------------
# Linux hosts (meili, loki, hodr, magni, torture): systemd + udev.
# ---------------------------------------------------------------------
BEFORE_ACTIVE_TS="$($SSH 'systemctl show -p ActiveEnterTimestamp --value mbregistry.service 2>/dev/null || true')"
WAS_ACTIVE="$($SSH 'systemctl is-active mbregistry.service 2>/dev/null || true')"

if [ "$NEED_REBUILD" -eq 1 ]; then
    if [ "$WAS_ACTIVE" = "active" ]; then
        # CLAUDE.md's own uv-venv-clear/root-owned-__pycache__ caveat --
        # stop the daemon (it runs as root, no User=) before an in-place
        # venv rebuild can remove everything it may have written.
        echo "==> [$HOST] stopping mbregistry.service before rebuild" >&2
        $SSH 'sudo systemctl stop mbregistry.service'
    fi

    # Merely stopping the daemon does not fix ownership of any
    # __pycache__/etc it already wrote as root while it ran -- `uv venv
    # --clear` (run as the unprivileged operator, inside
    # deploy-test-host.sh below) cannot remove those and fails outright
    # (observed: "Permission denied" removing lib/). Root-remove any
    # leftover venv from a previous run (both the normal and the
    # low-disk tmpfs location) so the unprivileged rebuild always starts
    # from a clean slate -- this is the idempotent tooling "handling it
    # itself rather than relying on an operator remembering it" CLAUDE.md
    # and this ticket's Implementation Notes both call for.
    echo "==> [$HOST] removing any root-owned leftovers from a previous install" >&2
    $SSH 'sudo rm -rf "$HOME/mbtools-venv" "/tmp/mbtools-venv-$(id -un)"'

    echo "==> [$HOST] rebuilding and reinstalling via deploy-test-host.sh" >&2
    "$REPO_ROOT/scripts/deploy-test-host.sh" "$HOST"
    $SSH "echo '${LOCAL_HASH}' > ${MARKER}"
fi

# Locate the venv deploy-test-host.sh just built/confirmed (its own
# low-disk tmpfs fallback means the path isn't fixed -- check both).
VENV_BIN="$($SSH '
    if [ -x "$HOME/mbtools-venv/bin/mbregistry" ]; then
        echo "$HOME/mbtools-venv/bin"
    elif [ -x "/tmp/mbtools-venv-$(id -un)/bin/mbregistry" ]; then
        echo "/tmp/mbtools-venv-$(id -un)/bin"
    fi
')"
if [ -z "$VENV_BIN" ]; then
    echo "==> [$HOST] ERROR: no mbtools venv found after deploy" >&2
    exit 1
fi

# install-service writes deterministic content every time (its own
# docstring's idempotency guarantee) -- always run it, and its printed
# follow-ups, to self-heal any unit/udev drift without needing a rebuild.
echo "==> [$HOST] running install-service" >&2
$SSH "sudo ${VENV_BIN}/mbregistry install-service"

echo "==> [$HOST] applying install-service's printed follow-up commands" >&2
$SSH 'sudo systemctl daemon-reload'
$SSH 'sudo systemctl enable --now mbregistry.service'
$SSH 'sudo udevadm control --reload-rules'
$SSH 'sudo udevadm trigger'
$SSH 'sudo usermod -aG plugdev "${SUDO_USER:-$(whoami)}"'

NOW_ACTIVE="$($SSH 'systemctl is-active mbregistry.service 2>/dev/null || true')"
if [ "$NOW_ACTIVE" != "active" ]; then
    echo "==> [$HOST] ERROR: mbregistry.service is not active after deploy (state: ${NOW_ACTIVE})" >&2
    exit 1
fi

if [ "$NEED_REBUILD" -eq 0 ]; then
    AFTER_ACTIVE_TS="$($SSH 'systemctl show -p ActiveEnterTimestamp --value mbregistry.service 2>/dev/null || true')"
    if [ "$BEFORE_ACTIVE_TS" != "$AFTER_ACTIVE_TS" ]; then
        echo "==> [$HOST] ERROR: mbregistry.service restarted on a no-op run (before='${BEFORE_ACTIVE_TS}' after='${AFTER_ACTIVE_TS}')" >&2
        exit 1
    fi
    echo "==> [$HOST] confirmed: service was not disrupted (ActiveEnterTimestamp unchanged)" >&2
fi

echo "==> [$HOST] deploy complete: mbregistry.service active, ${VENV_BIN}/mbregistry" >&2
