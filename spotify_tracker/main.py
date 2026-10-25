"""Entry point: checks the artists you follow on Spotify and notifies on new releases."""

import argparse
import calendar
import html
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from tqdm import tqdm

from spotify_tracker.config import REPO_DIR, Config, load_config
from spotify_tracker.discord_api import DiscordNotifier
from spotify_tracker.spotify_client import (
    MAX_APPEARS_ON_CALLS,
    RELEASE_GROUPS,
    AppearsOnBudgetError,
    SpotifyClient,
    SpotifyDailyLimitError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)
from spotify_tracker.state import StateStore
from spotify_tracker.telegram import TelegramNotifier


class TqdmLoggingHandler(logging.Handler):
    """Writes log lines via tqdm so they appear above an active progress bar
    instead of mangling it."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

_handler = TqdmLoggingHandler()
_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger("spotify_tracker")


# Each run also writes a copy of its console output to a file in this
# directory, one file per calendar day. Defaults are anchored at the
# repository root (like config.env), not the working directory, so running from
# elsewhere can't silently start over with an empty state file.
DEFAULT_LOG_DIR = REPO_DIR / "logs"
DEFAULT_STATE_FILE = REPO_DIR / "state.json"


def _begin_run_log(log_dir: Path) -> logging.Handler:
    """Attach the current day's log file so this run's console output is also
    saved to logs/<YYYY-MM-DD>.log. Runs on the same day append to the same
    file. Returns the handler to pass to _end_run_log when the run finishes."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"{time.strftime('%Y-%m-%d')}.log"

    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger.addHandler(handler)
    logger.info("Logging this run to %s", path)
    return handler


def _end_run_log(handler: logging.Handler) -> None:
    """Detach and close a day's log file opened by _begin_run_log."""
    logger.removeHandler(handler)
    handler.close()


FOLLOWED_CACHE_MAX_AGE = 24 * 3600  # refresh the followed-artists list daily

# Only notify about releases at most this old. Anything older that shows up
# as "unseen" is Spotify catalog noise (relinked/duplicate IDs, regional
# editions, availability changes), not a new release — absorb it silently.
NOTIFY_MAX_AGE_DAYS = 30

# Extra buffer past Spotify's Retry-After before touching the API again.
# Any request during an active penalty extends the penalty, so resuming
# even slightly early restarts the whole block.
RATE_LIMIT_SAFETY_MARGIN = 120  # seconds

# Never record a penalty shorter than this. Spotify's Retry-After can be
# small or missing while the block actually lasts much longer, and one
# request sent mid-penalty restarts the whole block — so when in doubt,
# stay away longer.
RATE_LIMIT_MIN_PENALTY = 30 * 60  # seconds


# Spotify's daily quota for this app is ~105 requests; stop at 100 to keep a
# margin before Spotify itself starts blocking. The count spans every run of
# the calendar day (persisted in state) and resets at UTC midnight.
DAILY_CALL_LIMIT = 100

# Reserve one extra call on top of an artist's estimated cost, to cover a
# token refresh that might land mid-fetch.
COST_SAFETY_MARGIN = 1

# Every check makes one request per release group, whatever the catalog size,
# so every artist costs the same, unless their "Appears On" list is checked too.
ARTIST_CHECK_COST = len(RELEASE_GROUPS) + COST_SAFETY_MARGIN


def _escape(text: str) -> str:
    """Escape text for a Telegram message sent with parse_mode=HTML."""
    return html.escape(text, quote=False)


def wants_appears_on(artist: dict, appears_on_artists: frozenset[str]) -> bool:
    """True if APPEARS_ON_ARTISTS names this artist (by name or Spotify ID)."""
    return artist["id"].casefold() in appears_on_artists or artist["name"].casefold() in appears_on_artists


def estimated_cost(state: StateStore, artist_id: str, with_appears_on: bool) -> int:
    """Predicted number of Spotify calls to check this artist. Albums and
    singles cost a fixed amount; an "Appears On" list costs one call per 10
    releases, so those artists are budgeted from their last measured cost."""
    if not with_appears_on:
        return ARTIST_CHECK_COST
    last = state.last_cost(artist_id) or 0
    return max(last + COST_SAFETY_MARGIN, ARTIST_CHECK_COST + 1)


def released_recently(release_date: str | None, max_age_days: int = NOTIFY_MAX_AGE_DAYS) -> bool:
    """True if the release date is within the notification window.

    Spotify dates come in three precisions: "2026", "2026-07", "2026-07-08".
    Coarse dates are read as the *end* of the stated period, erring toward
    notifying rather than dropping a genuinely fresh release.
    """
    if not release_date:
        return False
    parts = release_date.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 12
        day = int(parts[2]) if len(parts) > 2 else calendar.monthrange(year, month)[1]
        released = datetime(year, month, day, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        return False
    return datetime.now(timezone.utc) - released <= timedelta(days=max_age_days)


def get_artists_to_check(spotify: SpotifyClient, state: StateStore) -> list[dict]:
    """Followed artists, most-overdue first, from cache when fresh enough.

    Raises SpotifyRateLimitError if the refresh gets rate limited: a penalty
    is active, and continuing to check artists (even from a cached list)
    would keep poking the API and extend it.
    """
    artists = state.cached_followed_artists(FOLLOWED_CACHE_MAX_AGE)
    if artists is None:
        artists = spotify.get_followed_artists()
        state.set_followed_artists(artists)
        state.save()
        logger.info("Refreshed followed-artists list (%d artists)", len(artists))

    # Least recently checked first, so short rate-limited runs rotate through
    # everyone over time instead of rechecking the same few. Never-checked
    # artists sort first.
    return sorted(artists, key=lambda a: state.checked_at(a["id"]))


def record_penalty(state: StateStore, retry_after: int) -> None:
    """Persist the penalty deadline so later runs (cron or --interval) skip
    the API entirely until it has passed."""
    until = time.time() + max(retry_after, RATE_LIMIT_MIN_PENALTY) + RATE_LIMIT_SAFETY_MARGIN
    state.set_rate_limited_until(until)
    state.save()
    logger.warning(
        "Penalty recorded: no Spotify requests until %s",
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)),
    )


class Notifier(Protocol):
    """TelegramNotifier or DiscordNotifier. Alerts are passed as Telegram HTML
    (escaped text and <b> tags); DiscordNotifier converts them."""

    def send_message(self, text: str) -> None: ...


def make_notifier(config: Config) -> Notifier:
    if config.notifier == "discord":
        return DiscordNotifier(config.discord_bot_token, config.discord_channel_id)
    return TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)


# (artist name, new releases alerted on, when last checked before this run;
# 0 if never).
CheckedArtist = tuple[str, list[str], float]


def log_checked_artists(summary: list[CheckedArtist]) -> None:
    """End the run's output with one line per artist it checked:
    artist - new release(s) (empty if none) - last check before this one."""
    if not summary:
        return
    logger.info("Artists checked this run (artist - new release - last check before this one):")
    for name, releases, previous in summary:
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(previous)) if previous else "never"
        logger.info("%s - %s - %s", name, ", ".join(releases), last)


def check_artists(
    spotify: SpotifyClient,
    notifier: Notifier,
    state: StateStore,
    appears_on_artists: frozenset[str] = frozenset(),
) -> None:
    summary: list[CheckedArtist] = []
    try:
        _check_artists(spotify, notifier, state, appears_on_artists, summary)
    finally:
        # Also when a rate limit or outage stops the run early.
        log_checked_artists(summary)


def _check_artists(
    spotify: SpotifyClient,
    notifier: Notifier,
    state: StateStore,
    appears_on_artists: frozenset[str],
    summary: list[CheckedArtist],
) -> None:
    """check_artists' work; appends each artist it checks to summary."""
    penalty_left = state.rate_limited_until() - time.time()
    if penalty_left > 0:
        logger.warning(
            "Rate-limit penalty from a previous run still active for %.0f more minutes; skipping this run",
            penalty_left / 60,
        )
        return

    if spotify.daily_calls >= DAILY_CALL_LIMIT:
        logger.warning(
            "Daily Spotify call budget already spent (%d/%d); skipping this run until the budget resets",
            spotify.daily_calls,
            DAILY_CALL_LIMIT,
        )
        return

    try:
        artists = get_artists_to_check(spotify, state)
    except SpotifyRateLimitError as err:
        logger.warning(
            "Spotify rate limit hit while refreshing the followed list (retry after %ds); skipping this run",
            err.retry_after,
        )
        record_penalty(state, err.retry_after)
        return
    except SpotifyDailyLimitError as err:
        logger.warning(
            "Daily Spotify call budget reached (%d/%d) while refreshing the followed list; skipping this run",
            err.calls_today,
            err.limit,
        )
        return
    if not artists:
        logger.warning("No followed artists to check (the followed-artists list is empty).")
        return

    if appears_on_artists:
        followed_keys = {a["id"].casefold() for a in artists} | {a["name"].casefold() for a in artists}
        unknown = sorted(appears_on_artists - followed_keys)
        if unknown:
            logger.warning(
                "APPEARS_ON_ARTISTS names artists you don't follow (check the spelling; "
                "an artist whose name contains a comma must be given by Spotify ID): %s",
                ", ".join(unknown),
            )

    logger.info("Checking %d followed artists (most overdue first)", len(artists))
    checked = 0
    budget_deferred = 0
    with tqdm(artists, desc="Checking artists", unit="artist") as progress:
        for index, artist in enumerate(progress):
            artist_id = artist["id"]
            display_name = artist["name"]
            progress.set_postfix_str(display_name)
            is_baseline = not state.has_artist(artist_id)
            previous_check = state.checked_at(artist_id)
            with_appears_on = wants_appears_on(artist, appears_on_artists)

            # Once the day's budget can't cover even a plain check, stop: none
            # of the rest would fit either. They stay most-overdue and go
            # first once the budget resets.
            remaining = DAILY_CALL_LIMIT - spotify.daily_calls
            if ARTIST_CHECK_COST > remaining:
                budget_deferred += len(artists) - index
                logger.info(
                    "Daily budget nearly spent (%d calls left, ~%d needed per artist); deferring the remaining %d artists",
                    remaining,
                    ARTIST_CHECK_COST,
                    len(artists) - index,
                )
                break

            # An artist whose "Appears On" list is checked can cost far more
            # than the rest. Don't start one that won't fit (a check is only
            # recorded once complete, so a half-done one wastes its calls);
            # cheaper artists after it may still fit.
            estimate = estimated_cost(state, artist_id, with_appears_on)
            if estimate > remaining:
                budget_deferred += 1
                logger.info(
                    "Deferring %s: ~%d calls needed but only %d left in today's budget",
                    display_name,
                    estimate,
                    remaining,
                )
                continue

            calls_before_artist = spotify.request_count
            try:
                # Fetch the newest albums and newest singles, plus the whole
                # "Appears On" list for opted-in artists (first, so if it
                # turns out too big for today only one call is spent). If
                # this raises between requests, nothing is recorded and the
                # next run retries this artist from scratch.
                releases = []
                if with_appears_on:
                    appears_on, calls_needed = spotify.get_appears_on(
                        artist_id, reserve_calls=len(RELEASE_GROUPS)
                    )
                    if calls_needed > MAX_APPEARS_ON_CALLS:
                        logger.warning(
                            "%s appears on too many releases to check in full (%d calls, limit %d); checking only the first %d",
                            display_name,
                            calls_needed,
                            MAX_APPEARS_ON_CALLS,
                            len(appears_on),
                        )
                    releases.extend(appears_on)
                releases.extend(spotify.get_latest_releases(artist_id))
            except AppearsOnBudgetError as err:
                # Remember the real cost so later runs budget for it, and try
                # this artist again first once the budget resets.
                state.set_last_cost(artist_id, err.calls_needed + len(RELEASE_GROUPS))
                state.set_api_calls_today(spotify.daily_calls)
                state.save()
                budget_deferred += 1
                logger.info(
                    "Deferring %s: their Appears On list needs %d calls, more than today's budget has left",
                    display_name,
                    err.calls_needed,
                )
                continue
            except SpotifyRateLimitError as err:
                # Any further request during the penalty window extends it, so
                # stop here; already-checked artists are saved and the next run
                # starts with the ones we didn't reach.
                progress.close()
                logger.warning(
                    "Spotify rate limit hit (retry after %ds); stopping early: %d/%d artists checked, %d remaining",
                    err.retry_after,
                    checked,
                    len(artists),
                    len(artists) - checked,
                )
                record_penalty(state, err.retry_after)
                return
            except SpotifyDailyLimitError as err:
                # The estimate fell short (a grown catalog or a mid-fetch token
                # refresh pushed us over). The partial fetch is discarded; stop
                # cleanly and let the remaining artists wait for the reset.
                progress.close()
                logger.warning(
                    "Daily Spotify call budget reached (%d/%d); stopping: %d/%d artists checked",
                    err.calls_today,
                    err.limit,
                    checked,
                    len(artists),
                )
                return
            except SpotifyUnavailableError:
                # Not this artist's fault: the network or token endpoint is
                # down, and skipping ahead would spend a call on every
                # remaining artist for nothing. Already-checked artists are
                # saved; the run fails and the next one resumes from here.
                progress.close()
                logger.error(
                    "Spotify unavailable; stopping: %d/%d artists checked", checked, len(artists)
                )
                raise
            except Exception:
                # One artist failing shouldn't kill the whole run; the next run
                # will pick this artist up again.
                logger.exception("Failed to fetch releases for %s; skipping", display_name)
                continue

            # How many calls this check actually cost, saved with the artist
            # for reference.
            cost = spotify.request_count - calls_before_artist
            current_ids = {release["id"] for release in releases}

            if is_baseline:
                # Record the newest releases as a baseline instead of
                # notifying about them.
                state.record_check(artist_id, display_name, current_ids, cost=cost)
                state.set_api_calls_today(spotify.daily_calls)
                state.save()
                checked += 1
                summary.append((display_name, [], previous_check))
                logger.info(
                    "Baseline recorded for %s (%d releases, %d API calls)",
                    display_name,
                    len(current_ids),
                    cost,
                )
                continue

            known_ids = state.known_release_ids(artist_id)
            new_releases = [r for r in releases if r["id"] not in known_ids]

            # Build up the set to record as we go. A release becomes "known"
            # only once we've actually dealt with it: absorbed as old noise, or
            # successfully notified. A release whose alert fails to send is left
            # out, so it stays unknown and is retried on the next run instead of
            # being silently marked seen (which would drop the alert entirely).
            resolved_ids = set(known_ids)
            absorbed = 0
            # (name, release date) of releases alerted on in this check. With
            # no market filter, Spotify can list one release under several
            # regional IDs; alert once and mark the duplicates known.
            alerted = set()
            alerted_names = []
            for release in new_releases:
                is_appears_on = release.get("album_group") == "appears_on"
                if not released_recently(release.get("release_date")) or (
                    is_appears_on and release.get("album_type") == "compilation"
                ):
                    # Unseen but old: catalog noise, not a new release. Same
                    # for a compilation the artist appears on (a "Pop Hits"
                    # style collection of existing songs). Merge it into the
                    # known set without notifying.
                    absorbed += 1
                    resolved_ids.add(release["id"])
                    continue
                release_key = (release["name"].casefold(), release.get("release_date"))
                if release_key in alerted:
                    resolved_ids.add(release["id"])
                    continue
                # The message is built as Telegram HTML, which rejects a bare
                # "<" or "&", so every value from Spotify is escaped. (Discord
                # gets it converted to markdown.)
                artist_html = _escape(display_name)
                release_html = _escape(release["name"])
                release_kind = _escape(release.get("album_type") or "release")
                if is_appears_on:
                    credited = ", ".join(a["name"] for a in release.get("artists", [])) or "another artist"
                    headline = (
                        f"\U0001F3B5 <b>{artist_html}</b> appears on:\n"
                        f"<b>{release_html}</b> by {_escape(credited)} ({release_kind})\n"
                    )
                else:
                    headline = (
                        f"\U0001F3B5 <b>{artist_html}</b> just released:\n"
                        f"<b>{release_html}</b> ({release_kind})\n"
                    )
                message = (
                    headline
                    + f"Released: {_escape(release.get('release_date') or 'unknown')}\n"
                    + _escape(release.get("external_urls", {}).get("spotify", ""))
                )
                try:
                    notifier.send_message(message)
                except Exception:
                    # Don't mark this release known: leaving it out means the
                    # next run retries the alert rather than dropping it.
                    logger.exception(
                        "Failed to send %s alert for %s - %s; will retry next run",
                        type(notifier).__name__.removesuffix("Notifier"),
                        display_name,
                        release["name"],
                    )
                    continue
                resolved_ids.add(release["id"])
                alerted.add(release_key)
                alerted_names.append(release["name"])
                logger.info("Notified about new release: %s - %s", display_name, release["name"])
            if absorbed:
                logger.info(
                    "Absorbed %d unseen but old release(s) for %s without notifying", absorbed, display_name
                )

            # resolved_ids already holds everything that should stay known:
            # the previously-known IDs, plus this run's absorbed-and-notified
            # releases, minus any whose notification failed. Merge rather than
            # replace: each check only sees the newest page of each group, so
            # older releases must stay known in case they reappear there.
            state.record_check(artist_id, display_name, resolved_ids, cost=cost)
            state.set_api_calls_today(spotify.daily_calls)
            state.save()
            checked += 1
            summary.append((display_name, alerted_names, previous_check))

    if budget_deferred:
        logger.info(
            "Run complete: checked %d artists; deferred %d artists over today's call budget to later runs",
            checked,
            budget_deferred,
        )
    else:
        logger.info("Run complete: checked all %d artists", checked)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track the artists you follow on Spotify and send a Telegram or Discord alert when they release new music."
    )
    parser.add_argument(
        "--state-file",
        default=str(DEFAULT_STATE_FILE),
        help="Path to the JSON file used to remember already-seen releases (default: state.json in the repository root).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=0,
        help="If set, keep running and poll every N seconds instead of checking once and exiting.",
    )
    parser.add_argument(
        "--log-dir",
        default=str(DEFAULT_LOG_DIR),
        help="Directory for log files (one .log per day; default: logs/ in the repository root).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    log_dir = Path(args.log_dir)

    config = load_config()
    spotify = SpotifyClient(
        config.spotify_client_id,
        config.spotify_client_secret,
        config.spotify_refresh_token,
        daily_call_limit=DAILY_CALL_LIMIT,
    )
    notifier = make_notifier(config)
    state = StateStore(Path(args.state_file))

    def run_once(catch_errors: bool) -> None:
        # Mirror this run's console output to the day's logs/<YYYY-MM-DD>.log.
        # check_artists persists state itself on every path that mutates it
        # (per artist, on penalty, and after refreshing the followed list);
        # the finally below persists the day's call total as a backstop.
        log_handler = _begin_run_log(log_dir)
        # Seed the day's running call total from state so the budget is
        # enforced across runs; api_calls_today resets itself on a new day.
        spotify.start_day(state.api_calls_today())
        calls_before = spotify.request_count
        try:
            check_artists(spotify, notifier, state, config.appears_on_artists)
        except Exception:
            # Log the error while the day's file is still attached, so the
            # cause of a failed run is in logs/ in both modes.
            logger.exception("Error while checking artists")
            if not catch_errors:
                # A single run exits non-zero; the traceback is already logged,
                # so don't let the interpreter print it a second time.
                raise SystemExit(1)
            # In the polling loop, keep going after a failed run.
        finally:
            # Persist the day's call total even if the run crashed or stopped
            # early, so the next run inherits an accurate budget.
            state.set_api_calls_today(spotify.daily_calls)
            state.save()
            # Always report the request count — including when a rate limit
            # stopped the run early or it crashed outright. This runs before
            # the exception (if any) propagates, so the number is never lost.
            logger.info("Spotify API requests this run: %d", spotify.request_count - calls_before)
            logger.info(
                "Spotify API calls used today: %d/%d", spotify.daily_calls, DAILY_CALL_LIMIT
            )
            _end_run_log(log_handler)

    if args.interval > 0:
        logger.info("Starting polling loop every %d seconds (Ctrl+C to stop)", args.interval)
        while True:
            run_once(catch_errors=True)
            time.sleep(args.interval)
    else:
        run_once(catch_errors=False)


if __name__ == "__main__":
    main()
