#!/usr/bin/env bash
#
# Installer for the Spotify New Tracks notifier and its scheduled check.
#
# What it does:
#   1. Creates a Python virtualenv in ./venv (if missing) and installs
#      requirements.txt into it.
#   2. Runs the first-time setup (python -m spotify_tracker.setup): renames
#      an old .env to config.env or creates config.env from
#      config.env.example, and, when run from a terminal, asks for any
#      missing settings, runs the Spotify login and, for Discord, offers to
#      install the bot as the systemd service spotify-tracker-bot.service.
#   3. Schedules scripts/run_check.sh to run hourly; it only performs a real
#      check once ~25 hours have elapsed (see that file). On a machine with
#      systemd that's the timer spotify-tracker-check.timer
#      (install_check_timer.sh); elsewhere, or with --cron, a crontab entry.
#      Installing one removes the other, so only one schedule ever runs.
#      Your current PATH (and CHECK_INTERVAL_HOURS, if set) is saved to
#      schedule.env, which the wrapper reads, so the tools found now (flock,
#      python3) are found by the scheduled run too; re-run this script if
#      they move.
#
# Re-running it is safe: the timer is updated, and the crontab entry is
# tagged with a marker comment so it's replaced instead of duplicated.
# Files with the names an older version used (cron.env, logs/cron.log,
# state/last_cron_run) are renamed to the current ones.
#
# Usage:
#   ./install.sh              # full setup + schedule (systemd timer if available)
#   ./install.sh --cron       # full setup + crontab entry instead of the timer
#   ./install.sh --systemd    # full setup + systemd timer (fails without systemd)
#   ./install.sh --uninstall  # remove the schedule (leaves venv alone)
#   ./install.sh --no-venv    # skip venv/pip
#   ./install.sh --no-setup   # skip the config.env setup and login
#   ./install.sh --no-bot-service  # setup doesn't offer the Discord bot service
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MARKER="# spotify-tracker-check (managed by install.sh)"
# Entries from before the rename carry this one; they're removed too.
OLD_MARKER="# spotify-tracker-cron (managed by install_cron.sh)"
WRAPPER="${REPO_DIR}/scripts/run_check.sh"
SCHEDULE_LOG="${REPO_DIR}/logs/schedule.log"
SCHEDULE_ENV="${REPO_DIR}/schedule.env"
TIMER_SCRIPT="${REPO_DIR}/install_check_timer.sh"
# SYSTEMD_UNIT_DIR as in install_check_timer.sh, which the tests point elsewhere.
TIMER_UNIT="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}/spotify-tracker-check.timer"

# Hourly poll; the wrapper enforces the real ~25h gap.
CRON_SCHEDULE="0 * * * *"

DO_VENV=1
DO_SETUP=1
DO_BOT_SERVICE=1
# auto, systemd or cron.
SCHEDULER=auto

# Drop our crontab entry, keeping every other line. A machine without crontab
# has nothing to remove.
remove_cron_entry() {
    command -v crontab >/dev/null 2>&1 || return 0
    local current kept
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "${current}" | grep -qF -e "${MARKER}" -e "${OLD_MARKER}" -e "${WRAPPER}"; then
        # grep -v fails when it keeps no lines, which pipefail would turn
        # into an exit, so filter first.
        kept="$(printf '%s\n' "${current}" | grep -vF -e "${MARKER}" -e "${OLD_MARKER}" -e "${WRAPPER}" || true)"
        printf '%s\n' "${kept}" | crontab -
        echo "Removed the Spotify tracker crontab entry."
    fi
}

# The check sd_booted() makes: /run/systemd/system exists only under systemd.
# Overridable so the tests can fake either answer.
has_systemd() {
    command -v systemctl >/dev/null 2>&1 && [ -d "${SYSTEMD_RUN_DIR:-/run/systemd/system}" ]
}

for arg in "$@"; do
    case "${arg}" in
        --uninstall)
            remove_cron_entry
            if [ -f "${TIMER_UNIT}" ]; then
                bash "${TIMER_SCRIPT}" --uninstall
            fi
            rm -f "${SCHEDULE_ENV}" "${REPO_DIR}/cron.env"
            echo "Removed the scheduled checks."
            echo "(The Discord bot service is separate: ./install_bot_service.sh --uninstall)"
            exit 0
            ;;
        --systemd)
            SCHEDULER=systemd
            ;;
        --cron)
            SCHEDULER=cron
            ;;
        --no-venv)
            DO_VENV=0
            ;;
        --no-setup)
            DO_SETUP=0
            ;;
        --no-bot-service)
            DO_BOT_SERVICE=0
            ;;
        -h|--help)
            # Print only the header comment block (after the shebang).
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *)
            echo "Unknown option: ${arg}" >&2
            exit 1
            ;;
    esac
done

if [ "${SCHEDULER}" = auto ]; then
    if has_systemd; then
        SCHEDULER=systemd
    else
        SCHEDULER=cron
    fi
fi

# Checked before anything is set up, so a machine without cron fails here
# with a clear message rather than after the setup.
if [ "${SCHEDULER}" = cron ] && ! command -v crontab >/dev/null 2>&1; then
    echo "ERROR: crontab not found, and this machine has no systemd for the timer." >&2
    echo "       Install cron (or run on a machine with systemd), then re-run ./install.sh." >&2
    exit 1
fi

# --- 0. Files from before the rename ----------------------------------------
# Copied over from an older install; keep them working under the new names.
migrate() {
    if [ -f "$1" ] && [ ! -e "$2" ]; then
        mkdir -p "$(dirname "$2")"
        mv "$1" "$2"
        echo "Renamed ${1#"${REPO_DIR}"/} to ${2#"${REPO_DIR}"/}"
    fi
}
migrate "${REPO_DIR}/cron.env" "${SCHEDULE_ENV}"
migrate "${REPO_DIR}/logs/cron.log" "${SCHEDULE_LOG}"
migrate "${REPO_DIR}/state/last_cron_run" "${REPO_DIR}/state/last_scheduled_check"
rm -f "${REPO_DIR}/state/cron.lock"

# --- 1. Python environment --------------------------------------------------
if [ "${DO_VENV}" -eq 1 ]; then
    if [ ! -x "${REPO_DIR}/venv/bin/python" ]; then
        echo "Creating virtualenv in ${REPO_DIR}/venv ..."
        python3 -m venv "${REPO_DIR}/venv"
    else
        echo "Reusing existing virtualenv at ${REPO_DIR}/venv"
    fi
    echo "Installing dependencies ..."
    "${REPO_DIR}/venv/bin/pip" install --upgrade pip >/dev/null
    "${REPO_DIR}/venv/bin/pip" install -r "${REPO_DIR}/requirements.txt"
fi

# --- 2. Sanity checks -------------------------------------------------------
chmod +x "${WRAPPER}"
mkdir -p "${REPO_DIR}/logs"

if ! command -v flock >/dev/null 2>&1; then
    echo "WARNING: flock not found. scripts/run_check.sh needs it to run, so"
    echo "         every scheduled check will fail until you install it"
    echo "         (util-linux, or 'brew install flock' on macOS)."
fi

# --- 3. Settings and Spotify login -----------------------------------------
# The same interpreter the scheduled run will use (see scripts/run_check.sh).
if [ -x "${REPO_DIR}/venv/bin/python" ]; then
    PYTHON="${REPO_DIR}/venv/bin/python"
else
    PYTHON="python3"
fi

if [ "${DO_SETUP}" -eq 1 ]; then
    # Interactive only from a terminal; otherwise it just makes sure
    # config.env exists and lists what's missing. A failed login doesn't stop
    # the install: the schedule can go in now and the login be re-run later.
    # With --no-venv, the setup mustn't create ./venv on its own either.
    # (The ${...+...} form keeps an empty array legal under set -u in the
    # bash 3.2 that macOS ships.)
    SETUP_ARGS=()
    if [ "${DO_VENV}" -eq 0 ]; then
        SETUP_ARGS+=(--no-venv)
    fi
    if [ "${DO_BOT_SERVICE}" -eq 0 ]; then
        SETUP_ARGS+=(--no-bot-service)
    fi
    if ! (cd "${REPO_DIR}" && "${PYTHON}" -m spotify_tracker.setup ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}); then
        echo "WARNING: setup did not finish. Re-run it with:"
        echo "           python -m spotify_tracker.setup"
    fi
elif [ ! -f "${REPO_DIR}/config.env" ] && [ ! -f "${REPO_DIR}/.env" ]; then
    echo "WARNING: ${REPO_DIR}/config.env not found. The scheduled run will fail"
    echo "         until you create it (python -m spotify_tracker.setup)."
fi

# --- 4. Save the environment the scheduled run needs ------------------------
# Cron and systemd run jobs with a minimal PATH (often just /usr/bin:/bin), so
# a flock or python3 found here (e.g. Homebrew's /opt/homebrew/bin) would be
# missing in the scheduled run. Save this shell's PATH, the one checked above,
# for the wrapper to read. It goes in a file rather than the crontab line
# because cron silently cuts commands longer than ~1000 characters, which a
# long PATH would exceed. CHECK_INTERVAL_HOURS rides along so an interval
# given to the installer reaches the scheduled run (CRON_INTERVAL_HOURS, its
# old name, still works).
INTERVAL="${CHECK_INTERVAL_HOURS:-${CRON_INTERVAL_HOURS:-}}"
{
    printf 'PATH=%s\n' "${PATH}"
    if [ -n "${INTERVAL}" ]; then
        printf 'CHECK_INTERVAL_HOURS=%s\n' "${INTERVAL}"
    fi
} > "${SCHEDULE_ENV}"

# --- 5a. The systemd timer --------------------------------------------------
if [ "${SCHEDULER}" = systemd ]; then
    bash "${TIMER_SCRIPT}"
    remove_cron_entry
    echo
    echo "Check interval: ${INTERVAL:-25} hours (saved in ${SCHEDULE_ENV})"
    echo "To remove it later: ${REPO_DIR}/install.sh --uninstall"
    exit 0
fi

# --- 5b. Or the crontab entry (idempotent) ----------------------------------
# Quote a path for the crontab command line, which cron hands to /bin/sh:
# single-quote it (so spaces etc. survive) and escape "%", which cron would
# otherwise turn into a newline.
cron_quote() {
    local s="$1"
    s="${s//\'/\'\\\'\'}"
    s="${s//%/\\%}"
    printf "'%s'" "${s}"
}

# The wrapper appends its own output to logs/schedule.log (creating logs/
# first), so the entry needs no redirect into a directory that may have been
# deleted.
CRON_LINE="${CRON_SCHEDULE} /usr/bin/env bash $(cron_quote "${WRAPPER}") ${MARKER}"

# Keep every existing line except a previous entry of ours, then append fresh.
EXISTING="$(crontab -l 2>/dev/null | grep -vF -e "${MARKER}" -e "${OLD_MARKER}" -e "${WRAPPER}" || true)"
{
    if [ -n "${EXISTING}" ]; then
        printf '%s\n' "${EXISTING}"
    fi
    printf '%s\n' "${CRON_LINE}"
} | crontab -

if [ -f "${TIMER_UNIT}" ]; then
    echo "Removing the systemd check timer, replaced by the crontab entry ..."
    bash "${TIMER_SCRIPT}" --uninstall
fi

echo
echo "Installed crontab entry (polls hourly; runs a real check every ~25h):"
echo "  ${CRON_LINE}"
echo
echo "Check interval: ${INTERVAL:-25} hours (saved in ${SCHEDULE_ENV})"
echo "Check output is appended to: ${SCHEDULE_LOG}"
echo "To remove it later: ${REPO_DIR}/install.sh --uninstall"
