import logging
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

from spotify_tracker import main, spotify_client
from spotify_tracker.config import REPO_DIR, Config
from spotify_tracker.discord_api import DiscordNotifier
from spotify_tracker.main import check_artists, released_recently
from spotify_tracker.spotify_client import SpotifyClient, SpotifyUnavailableError
from spotify_tracker.state import StateStore
from spotify_tracker.telegram import TelegramNotifier


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")


def _release(release_id: str, name: str, release_date: str, album_type: str = "single") -> dict:
    return {
        "id": release_id,
        "name": name,
        "release_date": release_date,
        "album_type": album_type,
        "album_group": album_type,
        "external_urls": {"spotify": f"https://open.spotify.com/album/{release_id}"},
    }


class FakeSpotify:
    """Stands in for SpotifyClient: serves canned releases, never touches the
    network, and counts two calls per artist like the real one."""

    def __init__(self, releases_by_artist: dict[str, list[dict]], daily_calls: int = 0):
        self._releases = releases_by_artist
        self.daily_calls = daily_calls
        self.request_count = 0
        self.fetched: list[str] = []
        self.followed_fetches = 0

    def get_followed_artists(self) -> list[dict]:
        self.followed_fetches += 1
        return []

    def get_latest_releases(self, artist_id: str) -> list[dict]:
        self.fetched.append(artist_id)
        self.request_count += 2
        self.daily_calls += 2
        return [dict(r) for r in self._releases.get(artist_id, [])]


class FakeTelegram:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.sent: list[str] = []

    def send_message(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)


class _QuietProgress:
    """Replaces tqdm so the tests don't draw progress bars."""

    def __init__(self, iterable, **kwargs):
        self._iterable = iterable

    def __iter__(self):
        return iter(self._iterable)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_postfix_str(self, text):
        pass

    def close(self):
        pass


class MakeNotifierTest(unittest.TestCase):
    def _config(self, notifier: str) -> Config:
        return Config(
            spotify_client_id="id",
            spotify_client_secret="secret",
            spotify_refresh_token="refresh",
            notifier=notifier,
            telegram_bot_token="tg",
            telegram_chat_id="1",
            discord_bot_token="dc",
            discord_guild_id="2",
            discord_user_id="3",
            discord_channel_id="4",
            appears_on_artists=frozenset(),
        )

    def test_picks_the_configured_notifier(self):
        self.assertIsInstance(main.make_notifier(self._config("telegram")), TelegramNotifier)
        self.assertIsInstance(main.make_notifier(self._config("discord")), DiscordNotifier)


class CheckArtistsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = StateStore(Path(self._tmp.name) / "state.json")
        for patcher in (
            mock.patch.object(main, "tqdm", _QuietProgress),
            mock.patch.object(main.logger, "disabled", True),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _follow(self, artist_id: str, name: str, known: set[str] | None = None) -> None:
        """Follow an artist; with known given, they're already tracked (past
        their silent baseline) with those release IDs."""
        followed = self.state.cached_followed_artists(float("inf")) or []
        self.state.set_followed_artists(followed + [{"id": artist_id, "name": name}])
        if known is not None:
            self.state.record_check(artist_id, name, known)

    def test_alert_escapes_html_in_names(self):
        self._follow("a1", "Simon & Garfunkel", known=set())
        spotify = FakeSpotify({"a1": [_release("r1", "I <3 You", _days_ago(1))]})
        telegram = FakeTelegram()

        check_artists(spotify, telegram, self.state)

        self.assertEqual(len(telegram.sent), 1)
        message = telegram.sent[0]
        self.assertIn("<b>Simon &amp; Garfunkel</b>", message)
        self.assertIn("<b>I &lt;3 You</b>", message)
        self.assertNotIn("<3", message)

    def test_failed_alert_is_retried_next_run(self):
        self._follow("a1", "Artist", known=set())
        spotify = FakeSpotify({"a1": [_release("r1", "New Song", _days_ago(1))]})

        check_artists(spotify, FakeTelegram(fail=True), self.state)
        self.assertNotIn("r1", self.state.known_release_ids("a1"))

        telegram = FakeTelegram()
        check_artists(spotify, telegram, self.state)
        self.assertEqual(len(telegram.sent), 1)
        self.assertIn("r1", self.state.known_release_ids("a1"))

    def test_first_check_is_a_silent_baseline(self):
        self._follow("a1", "Artist")
        spotify = FakeSpotify({"a1": [_release("r1", "New Song", _days_ago(1))]})
        telegram = FakeTelegram()

        check_artists(spotify, telegram, self.state)

        self.assertEqual(telegram.sent, [])
        self.assertEqual(self.state.known_release_ids("a1"), {"r1"})

    def test_old_unseen_release_is_absorbed_without_alert(self):
        self._follow("a1", "Artist", known=set())
        spotify = FakeSpotify({"a1": [_release("r1", "Old Song", "2001-05-01")]})
        telegram = FakeTelegram()

        check_artists(spotify, telegram, self.state)

        self.assertEqual(telegram.sent, [])
        self.assertIn("r1", self.state.known_release_ids("a1"))

    def test_same_release_under_two_ids_alerts_once(self):
        self._follow("a1", "Artist", known=set())
        date = _days_ago(2)
        spotify = FakeSpotify({"a1": [_release("r1", "Song", date), _release("r2", "SONG", date)]})
        telegram = FakeTelegram()

        check_artists(spotify, telegram, self.state)

        self.assertEqual(len(telegram.sent), 1)
        self.assertEqual(self.state.known_release_ids("a1"), {"r1", "r2"})

    def test_spent_budget_skips_the_run(self):
        # No cached followed list, so only the early budget check stands
        # between this run and a followed-list refresh.
        spotify = FakeSpotify({}, daily_calls=main.DAILY_CALL_LIMIT)

        check_artists(spotify, FakeTelegram(), self.state)

        self.assertEqual(spotify.followed_fetches, 0)
        self.assertEqual(spotify.fetched, [])

    def test_stops_when_budget_cannot_cover_another_artist(self):
        self._follow("a1", "One", known=set())
        self._follow("a2", "Two", known=set())
        # Enough for exactly one more plain check (cost 2 + 1 spare).
        spotify = FakeSpotify({}, daily_calls=main.DAILY_CALL_LIMIT - main.ARTIST_CHECK_COST)

        with (
            mock.patch.object(main.logger, "disabled", False),
            self.assertLogs(main.logger, "INFO") as logs,
        ):
            check_artists(spotify, FakeTelegram(), self.state)

        self.assertEqual(len(spotify.fetched), 1)
        # The run stops at the budget line rather than deferring each
        # remaining artist one by one.
        self.assertTrue(any("deferring the remaining 1 artists" in line for line in logs.output), logs.output)

    def _summary(self, spotify) -> list[str]:
        """The run's log messages from the per-artist summary on."""
        with (
            mock.patch.object(main.logger, "disabled", False),
            self.assertLogs(main.logger, "INFO") as logs,
        ):
            check_artists(spotify, FakeTelegram(), self.state)
        messages = [record.getMessage() for record in logs.records]
        start = next(i for i, m in enumerate(messages) if m.startswith("Artists checked this run"))
        return messages[start + 1 :]

    def test_run_ends_with_each_checked_artist_its_release_and_last_check(self):
        self._follow("a1", "Old Friend", known=set())
        self._follow("a2", "Quiet One", known=set())
        self._follow("a3", "Newcomer")
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.state.checked_at("a1")))
        spotify = FakeSpotify({"a1": [_release("r1", "New Song", _days_ago(1))], "a3": []})

        lines = self._summary(spotify)

        self.assertEqual(
            sorted(lines),
            sorted([
                f"Old Friend - New Song - {last}",
                f"Quiet One -  - {last}",
                "Newcomer -  - never",
            ]),
        )

    def test_summary_is_logged_when_a_rate_limit_stops_the_run(self):
        self._follow("a1", "First", known=set())
        self._follow("a2", "Second", known=set())
        spotify = FakeSpotify({"a1": []})
        real_fetch = spotify.get_latest_releases

        def fetch(artist_id):
            if spotify.fetched:
                raise main.SpotifyRateLimitError(60)
            return real_fetch(artist_id)

        spotify.get_latest_releases = fetch
        with mock.patch.object(main, "record_penalty"):
            lines = self._summary(spotify)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("First -  - "), lines)

    def test_network_outage_stops_the_run_after_one_request(self):
        for i in range(3):
            self._follow(f"a{i}", f"Artist {i}", known=set())
        client = SpotifyClient("id", "secret", "refresh", daily_call_limit=main.DAILY_CALL_LIMIT)

        with (
            mock.patch.object(
                spotify_client.requests, "request", side_effect=requests.ConnectionError("dns down")
            ) as request,
            mock.patch.object(spotify_client.time, "sleep"),
        ):
            with self.assertRaises(SpotifyUnavailableError):
                check_artists(client, FakeTelegram(), self.state)

        self.assertEqual(request.call_count, 1)
        self.assertEqual(client.daily_calls, 1)

    def test_token_rate_limit_stops_the_run_and_records_a_penalty(self):
        for i in range(3):
            self._follow(f"a{i}", f"Artist {i}", known=set())
        client = SpotifyClient("id", "secret", "refresh", daily_call_limit=main.DAILY_CALL_LIMIT)
        response = mock.Mock(status_code=429, headers={"Retry-After": "0"})

        with mock.patch.object(spotify_client.requests, "request", return_value=response) as request:
            check_artists(client, FakeTelegram(), self.state)

        self.assertEqual(request.call_count, 1)
        self.assertGreater(self.state.rate_limited_until(), time.time())


class RunOnceTest(unittest.TestCase):
    def test_single_run_crash_is_logged_to_the_day_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        log_dir = Path(tmp.name) / "logs"
        argv = ["main", "--state-file", str(Path(tmp.name) / "state.json"), "--log-dir", str(log_dir)]
        env = {
            "SPOTIFY_CLIENT_ID": "id",
            "SPOTIFY_CLIENT_SECRET": "secret",
            "SPOTIFY_REFRESH_TOKEN": "refresh",
            "TELEGRAM_BOT_TOKEN": "bot",
            "TELEGRAM_CHAT_ID": "1",
        }

        with (
            mock.patch.dict(os.environ, env),
            mock.patch("sys.argv", argv),
            mock.patch.object(main, "check_artists", side_effect=RuntimeError("SENTINEL-CRASH")),
            mock.patch.object(main.logger, "propagate", False),  # keep the console quiet
        ):
            with self.assertRaises(SystemExit) as ctx:
                main.main()

        self.assertEqual(ctx.exception.code, 1)
        [log_file] = log_dir.glob("*.log")
        self.assertIn("SENTINEL-CRASH", log_file.read_text(encoding="utf-8"))


class ParseArgsTest(unittest.TestCase):
    def test_default_paths_are_in_the_repository_root(self):
        args = main.parse_args([])
        self.assertEqual(Path(args.state_file), REPO_DIR / "state.json")
        self.assertEqual(Path(args.log_dir), REPO_DIR / "logs")


class ReleasedRecentlyTest(unittest.TestCase):
    def test_precisions_and_bad_input(self):
        self.assertTrue(released_recently(_days_ago(3)))
        self.assertFalse(released_recently(_days_ago(45)))
        self.assertTrue(released_recently(str(datetime.now(timezone.utc).year)))
        self.assertFalse(released_recently("2001-02"))
        self.assertFalse(released_recently(""))
        self.assertFalse(released_recently(None))
        self.assertFalse(released_recently("0000"))
        self.assertFalse(released_recently("2026-13-01"))


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    unittest.main()
