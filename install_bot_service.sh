#!/usr/bin/env bash
#
# Installs the Discord bot (python -m spotify_tracker.discord_bot) as the
# systemd service spotify-tracker-bot.service, started now and at boot.
# python -m spotify_tracker.setup (which install.sh runs) offers to run
# this for you when you pick Discord.
#
# The service runs as the user who ran this script, from this directory, with
# ./venv/bin/python (or the system python3). Afterwards:
#   sudo systemctl restart spotify-tracker-bot.service
#   sudo systemctl status spotify-tracker-bot.service
#   journalctl -u spotify-tracker-bot.service -f
#
# Usage:
#   ./install_bot_service.sh              # install (or update) and (re)start
#   ./install_bot_service.sh --uninstall  # stop, disable and remove it
#   ./install_bot_service.sh --print      # only print the unit file
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="spotify-tracker-bot.service"
# Overridable so the tests can install into a temporary directory.
UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
UNIT_PATH="${UNIT_DIR}/${SERVICE}"
# Run as the real user even when this script itself runs under sudo.
RUN_USER="${SUDO_USER:-$(id -un)}"

# sudo only when not root already.
if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
else
    SUDO=(sudo)
fi

run_root() {
    # The ${...+...} form keeps an empty array legal under set -u in the
    # bash 3.2 that macOS ships.
    ${SUDO[@]+"${SUDO[@]}"} "$@"
}

ACTION=install
for arg in "$@"; do
    case "${arg}" in
        --uninstall) ACTION=uninstall ;;
        --print) ACTION=print ;;
        -h|--help)
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [ -x "${REPO_DIR}/venv/bin/python" ]; then
    PYTHON="${REPO_DIR}/venv/bin/python"
else
    PYTHON="$(command -v python3 || true)"
    if [ -z "${PYTHON}" ]; then
        echo "ERROR: no ./venv and no python3 on PATH; run python3 -m spotify_tracker.setup first." >&2
        exit 1
    fi
fi

# systemd expands % specifiers in unit files, so a literal % is written %%.
unit_escape() {
    printf '%s' "${1//%/%%}"
}

unit_file() {
    cat <<EOF
[Unit]
Description=Spotify New Tracks Discord bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(unit_escape "${RUN_USER}")
WorkingDirectory=$(unit_escape "${REPO_DIR}")
ExecStart="$(unit_escape "${PYTHON}")" -m spotify_tracker.discord_bot
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
# Exit status 2 means config.env is incomplete; restarting won't fix that.
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
EOF
}

if [ "${ACTION}" = print ]; then
    unit_file
    exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found; the bot service needs systemd (Linux)." >&2
    echo "       Run the bot by hand instead: ${PYTHON} -m spotify_tracker.discord_bot" >&2
    exit 1
fi

if [ "${ACTION}" = uninstall ]; then
    run_root systemctl disable --now "${SERVICE}" 2>/dev/null || true
    run_root rm -f "${UNIT_PATH}"
    run_root systemctl daemon-reload
    echo "Removed ${SERVICE}."
    exit 0
fi

echo "Installing ${UNIT_PATH} (runs as ${RUN_USER}) ..."
unit_file | run_root tee "${UNIT_PATH}" >/dev/null
run_root systemctl daemon-reload
run_root systemctl enable "${SERVICE}" >/dev/null
# restart, not start, so re-running this picks up a changed unit or code.
run_root systemctl restart "${SERVICE}"

echo
echo "Started ${SERVICE}. Manage it with:"
echo "  sudo systemctl restart ${SERVICE}"
echo "  sudo systemctl status ${SERVICE}"
echo "  journalctl -u ${SERVICE} -f"
