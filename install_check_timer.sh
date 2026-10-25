#!/usr/bin/env bash
#
# Installs the scheduled check as a systemd timer instead of a crontab entry:
# spotify-tracker-check.timer fires hourly and starts
# spotify-tracker-check.service, which runs scripts/run_check.sh. As with
# cron, the wrapper only performs a real check once ~25 hours have elapsed,
# and honours the Discord bot's /pause and the check lock.
# ./install.sh runs this for you on a machine with systemd (and removes the
# crontab entry, if any).
#
# The service runs as the user who ran this script, from this directory. The
# wrapper reads the PATH and interval that install.sh saved to schedule.env.
# Afterwards:
#   systemctl list-timers spotify-tracker-check.timer
#   sudo systemctl start spotify-tracker-check.service   # tick now
#   journalctl -u spotify-tracker-check.service
#
# Usage:
#   ./install_check_timer.sh              # install (or update) and start
#   ./install_check_timer.sh --uninstall  # stop, disable and remove it
#   ./install_check_timer.sh --print      # only print the unit files
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="spotify-tracker-check.service"
TIMER="spotify-tracker-check.timer"
WRAPPER="${REPO_DIR}/scripts/run_check.sh"
# Overridable so the tests can install into a temporary directory.
UNIT_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"
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

# systemd expands % specifiers in unit files, so a literal % is written %%.
unit_escape() {
    printf '%s' "${1//%/%%}"
}

service_file() {
    cat <<EOF
[Unit]
Description=Spotify New Tracks scheduled check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$(unit_escape "${RUN_USER}")
WorkingDirectory=$(unit_escape "${REPO_DIR}")
ExecStart=/usr/bin/env bash "$(unit_escape "${WRAPPER}")"
EOF
}

timer_file() {
    cat <<EOF
[Unit]
Description=Hourly tick for the Spotify New Tracks check

[Timer]
OnCalendar=hourly
# Catch up on a tick missed while the machine was off.
Persistent=true

[Install]
WantedBy=timers.target
EOF
}

if [ "${ACTION}" = print ]; then
    echo "# ${UNIT_DIR}/${SERVICE}"
    service_file
    echo
    echo "# ${UNIT_DIR}/${TIMER}"
    timer_file
    exit 0
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "ERROR: systemctl not found; the check timer needs systemd (Linux)." >&2
    echo "       Use a crontab entry instead: ./install.sh --cron" >&2
    exit 1
fi

if [ "${ACTION}" = uninstall ]; then
    run_root systemctl disable --now "${TIMER}" 2>/dev/null || true
    run_root systemctl stop "${SERVICE}" 2>/dev/null || true
    run_root rm -f "${UNIT_DIR}/${SERVICE}" "${UNIT_DIR}/${TIMER}"
    run_root systemctl daemon-reload
    echo "Removed ${TIMER} and ${SERVICE}."
    exit 0
fi

chmod +x "${WRAPPER}"

echo "Installing ${UNIT_DIR}/${TIMER} (checks run as ${RUN_USER}) ..."
service_file | run_root tee "${UNIT_DIR}/${SERVICE}" >/dev/null
timer_file | run_root tee "${UNIT_DIR}/${TIMER}" >/dev/null
run_root systemctl daemon-reload
run_root systemctl enable "${TIMER}" >/dev/null
# restart, not start, so re-running this picks up a changed timer.
run_root systemctl restart "${TIMER}"

echo
echo "Started ${TIMER} (ticks hourly; runs a real check every ~25h). Manage it with:"
echo "  systemctl list-timers ${TIMER}"
echo "  sudo systemctl start ${SERVICE}   # tick now"
echo "  journalctl -u ${SERVICE}"
echo "Check output is appended to: ${REPO_DIR}/logs/schedule.log"
