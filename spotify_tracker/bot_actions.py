"""What the Discord bot's commands do, without discord.py itself, so it can
be tested on its own. spotify_tracker.discord_bot wires these to commands.

The bot shares state/ with scripts/run_check.sh (the wrapper that the
systemd check timer or cron runs hourly):
- state/check.lock: held during every check, by the wrapper (flock) or by
  /check here, so two checks never run at once.
- state/paused: while it exists the wrapper skips its scheduled checks.
  /check still runs.
- state/last_scheduled_check: when the wrapper last started a check.
"""

import asyncio
import os
import re
import sys
import time
from typing import IO

from dotenv import dotenv_values

from spotify_tracker.config import REPO_DIR, env_path
from spotify_tracker.state import StateStore

STATE_FILE = REPO_DIR / "state.json"
STATE_DIR = REPO_DIR / "state"
LOCK_FILE = STATE_DIR / "check.lock"
PAUSE_FILE = STATE_DIR / "paused"
STAMP_FILE = STATE_DIR / "last_scheduled_check"
SCHEDULE_ENV = REPO_DIR / "schedule.env"
DEFAULT_INTERVAL_HOURS = 25

# Same as main.DAILY_CALL_LIMIT; importing main would set up its logging here.
DAILY_CALL_LIMIT = 100

# Room for the code block around a check's log lines in a 2000-character message.
REPLY_LOG_CHARS = 1800
REPLY_LOG_LINES = 15

_LOG_LINE = re.compile(r"\[(INFO|WARNING|ERROR)\] (.*)")


def try_lock() -> IO | None:
    """Takes the check lock without waiting. Returns the open lock file (close
    it to release), or None if a check is already running."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK_FILE, "w")
    try:
        import fcntl
    except ImportError:
        # No flock on Windows, where the scheduled wrapper doesn't run either.
        return handle
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def summarize_output(output: str) -> str:
    """The last log lines of a check's console output, without timestamps or
    the progress bar."""
    lines = []
    for raw in output.splitlines():
        # tqdm redraws its bar with carriage returns; keep what follows the last.
        match = _LOG_LINE.search(raw.rsplit("\r", 1)[-1])
        if match:
            level, message = match.groups()
            lines.append(message if level == "INFO" else f"{level}: {message}")
    text = "\n".join(lines[-REPLY_LOG_LINES:])
    if len(text) > REPLY_LOG_CHARS:
        text = "..." + text[-REPLY_LOG_CHARS:]
    return text.replace("```", "'''")


async def run_check() -> str:
    """Runs one check (python -m spotify_tracker.main) under the check lock
    and returns the reply: whether it worked and its last log lines."""
    lock = try_lock()
    if lock is None:
        return "A check is already running. Try again when it has finished."
    try:
        # The bot's environment holds config.env as it was when the bot
        # started, and the check's load_dotenv doesn't override it. Leave
        # those settings out, so the check reads config.env as it is now
        # (e.g. a channel picked with /channel since).
        file_keys = dotenv_values(env_path()).keys()
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "spotify_tracker.main",
            cwd=REPO_DIR,
            env={name: value for name, value in os.environ.items() if name not in file_keys},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
    finally:
        lock.close()
    heading = "Check finished." if process.returncode == 0 else f"Check failed (exit code {process.returncode})."
    log = summarize_output(output.decode("utf-8", errors="replace"))
    return f"{heading}\n```\n{log}\n```" if log else heading


def is_paused() -> bool:
    return PAUSE_FILE.exists()


def set_paused(paused: bool) -> None:
    if paused:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        PAUSE_FILE.touch()
    else:
        PAUSE_FILE.unlink(missing_ok=True)


def _interval_hours() -> int | None:
    """The interval scripts/run_check.sh uses, or None if the saved one is
    invalid (the wrapper then exits with an error on every tick)."""
    saved = dotenv_values(SCHEDULE_ENV) if SCHEDULE_ENV.exists() else {}
    # CRON_INTERVAL_HOURS is its name before the rename.
    value = saved.get("CHECK_INTERVAL_HOURS") or saved.get("CRON_INTERVAL_HOURS")
    if not value:
        return DEFAULT_INTERVAL_HOURS
    # The wrapper's rule: a positive whole number without leading zeros.
    return int(value) if re.fullmatch(r"[1-9][0-9]*", value) else None


def _when(timestamp: float) -> str:
    """A time Discord shows in the reader's own time zone, e.g. "3 hours ago"."""
    return f"<t:{int(timestamp)}:R>"


def status_text(channel_mention: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    state = StateStore(STATE_FILE)
    lines = []

    if is_paused():
        lines.append("Scheduled checks: **paused** (/resume to restart them)")
    else:
        lines.append("Scheduled checks: running")
        interval = _interval_hours()
        if interval is None:
            lines.append(
                "Next scheduled check: none, CHECK_INTERVAL_HOURS in schedule.env is invalid (see logs/schedule.log)"
            )
        elif STAMP_FILE.exists():
            try:
                last_run = int(STAMP_FILE.read_text(encoding="utf-8").strip())
            except ValueError:
                last_run = 0
            if last_run:
                due = last_run + interval * 3600
                lines.append(f"Next scheduled check: {_when(max(due, now))}")
        else:
            lines.append("Next scheduled check: at the next hourly tick")

    last_check = state.last_checked_at()
    lines.append(f"Last artist checked: {_when(last_check) if last_check else 'never'}")
    followed = state.cached_followed_artists(float("inf"))
    lines.append(f"Followed artists: {len(followed) if followed is not None else 'not fetched yet'}")
    lines.append(f"Spotify API calls today: {state.api_calls_today()}/{DAILY_CALL_LIMIT}")
    penalty_until = state.rate_limited_until()
    if penalty_until > now:
        lines.append(f"Spotify rate limit: checks paused until {_when(penalty_until)}")
    lines.append(f"Alert channel: {channel_mention}")
    return "\n".join(lines)
