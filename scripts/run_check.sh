#!/usr/bin/env bash
#
# Hourly guard for the Spotify New Tracks notifier, run by the systemd timer
# spotify-tracker-check.timer or a crontab entry (both set up by install.sh).
#
# Cron can't fire "every 25 hours" (its fields are fixed clock times, which
# drift). Instead, install.sh registers this wrapper to run once an hour, and
# the wrapper only actually performs a check once at least ~25 hours have
# elapsed since the last real run. That gives a true ~25h cadence with
# nothing but an hourly tick, from either scheduler.
#
# A timestamp of the last real run is kept in state/last_scheduled_check. A
# lock (flock) prevents two checks from overlapping, including one started by
# the Discord bot's /check. While state/paused exists (the bot's /pause)
# every tick is skipped. Everything the wrapper and the check print is
# appended to logs/schedule.log. The PATH and interval saved by install.sh are
# read from schedule.env. Files under the names an older version used
# (cron.env, logs/cron.log, state/last_cron_run) are renamed on the way.
#
set -euo pipefail

# Resolve the repository root (this script lives in <repo>/scripts/).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

STATE_DIR="${REPO_DIR}/state"
STAMP_FILE="${STATE_DIR}/last_scheduled_check"
LOCK_FILE="${STATE_DIR}/check.lock"
PAUSE_FILE="${STATE_DIR}/paused"
SCHEDULE_ENV="${REPO_DIR}/schedule.env"
SCHEDULE_LOG="${REPO_DIR}/logs/schedule.log"

# Old names, e.g. copied over from an older install after running install.sh.
mkdir -p "${REPO_DIR}/logs" "${STATE_DIR}"
for pair in "cron.env:${SCHEDULE_ENV}" "logs/cron.log:${SCHEDULE_LOG}" "state/last_cron_run:${STAMP_FILE}"; do
    old="${REPO_DIR}/${pair%%:*}"
    new="${pair#*:}"
    if [ -f "${old}" ] && [ ! -e "${new}" ]; then
        mv "${old}" "${new}"
    fi
done

# Log here rather than through a redirect in the crontab entry or unit, so a
# deleted logs/ directory is recreated instead of stopping every scheduled run.
exec >>"${SCHEDULE_LOG}" 2>&1

# The scheduler's PATH is minimal, so use the one install.sh saved (where it
# found flock and python3), plus any interval given to the installer. The
# file is parsed line by line, never sourced. CRON_INTERVAL_HOURS is the
# interval's name before the rename.
SAVED_INTERVAL=""
if [ -f "${SCHEDULE_ENV}" ]; then
    while IFS= read -r line || [ -n "${line}" ]; do
        line="${line%$'\r'}"
        case "${line}" in
            PATH=*) PATH="${line#PATH=}" ;;
            CHECK_INTERVAL_HOURS=*) SAVED_INTERVAL="${line#CHECK_INTERVAL_HOURS=}" ;;
            CRON_INTERVAL_HOURS=*) SAVED_INTERVAL="${SAVED_INTERVAL:-${line#CRON_INTERVAL_HOURS=}}" ;;
        esac
    done < "${SCHEDULE_ENV}"
fi

# Desired gap between real runs, in hours: CHECK_INTERVAL_HOURS (or
# CRON_INTERVAL_HOURS) from the environment, else the one saved by the
# installer, else 25.
INTERVAL_HOURS="${CHECK_INTERVAL_HOURS:-${CRON_INTERVAL_HOURS:-${SAVED_INTERVAL:-25}}}"

# Bash arithmetic would read "010" as octal (8) and let 0 or a negative value
# through as "run on every tick", so accept only a plain positive whole number.
case "${INTERVAL_HOURS}" in
    ''|*[!0-9]*|0*)
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: CHECK_INTERVAL_HOURS must be a positive whole number of hours without leading zeros (got '${INTERVAL_HOURS}')" >&2
        exit 1
        ;;
esac

# Because the guard is only polled once an hour, subtract half of the polling
# interval (30 min) from the threshold so the hourly tick nearest the 25h mark
# fires, keeping the average gap close to 25h rather than always rounding up.
MIN_GAP_SECONDS=$(( INTERVAL_HOURS * 3600 - 1800 ))

# Paused from Discord: skip quietly, like a tick that isn't due, so the
# hourly log isn't spammed. /resume removes the file.
if [ -f "${PAUSE_FILE}" ]; then
    exit 0
fi

# Pick the interpreter: prefer the project venv, else fall back to system.
if [ -x "${REPO_DIR}/venv/bin/python" ]; then
    PYTHON="${REPO_DIR}/venv/bin/python"
else
    PYTHON="$(command -v python3 || command -v python)"
fi

now="$(date +%s)"

# Skip if a real run happened recently enough.
if [ -f "${STAMP_FILE}" ]; then
    last="$(cat "${STAMP_FILE}" 2>/dev/null || echo 0)"
    case "${last}" in
        ''|*[!0-9]*) last=0 ;;
    esac
    elapsed=$(( now - last ))
    if [ "${elapsed}" -lt "${MIN_GAP_SECONDS}" ]; then
        # Not due yet; stay quiet so the hourly log isn't spammed.
        exit 0
    fi
fi

# Without flock the check below would fail as if the lock were held, skipping
# every run with a misleading message, so fail loudly instead.
if ! command -v flock >/dev/null 2>&1; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: flock not found on PATH (${PATH}); install it (util-linux, or 'brew install flock' on macOS), then re-run install.sh so schedule.env records a PATH that includes it" >&2
    exit 1
fi

# Serialize runs: if a previous check is somehow still going, skip this tick.
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] previous run still active; skipping" >&2
    exit 0
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] running check (>= ${INTERVAL_HOURS}h since last run)"

# Record the timestamp before running so a crash mid-run still counts as an
# attempt and we don't hammer the API every hour after a failure.
echo "${now}" > "${STAMP_FILE}"

exec "${PYTHON}" -m spotify_tracker.main "$@"
