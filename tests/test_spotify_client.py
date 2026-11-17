import unittest
from unittest import mock

import requests

from spotify_tracker import spotify_client
from spotify_tracker.spotify_client import (
    SpotifyClient,
    SpotifyDailyLimitError,
    SpotifyRateLimitError,
    SpotifyUnavailableError,
)


class DailyBudgetTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(spotify_client, "utc_today", return_value="2026-10-23")
        self.today = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = SpotifyClient("id", "secret", "refresh", daily_call_limit=100)

    def test_counts_calls_toward_the_day(self):
        self.client.start_day(10)
        self.client._account_request()
        self.assertEqual(self.client.daily_calls, 11)
        self.assertEqual(self.client.request_count, 1)

    def test_refuses_calls_once_budget_spent(self):
        self.client.start_day(100)
        with self.assertRaises(SpotifyDailyLimitError):
            self.client._account_request()
        self.assertEqual(self.client.request_count, 0)

    def test_total_resets_when_utc_day_changes_mid_run(self):
        self.client.start_day(60)
        for _ in range(5):
            self.client._account_request()
        self.assertEqual(self.client.daily_calls, 65)

        # Midnight UTC passes while the same run keeps going.
        self.today.return_value = "2026-10-24"
        self.assertEqual(self.client.daily_calls, 0)
        self.client._account_request()
        self.assertEqual(self.client.daily_calls, 1)
        self.assertEqual(self.client.request_count, 6)

    def test_new_day_lifts_a_spent_budget(self):
        self.client.start_day(100)
        self.today.return_value = "2026-10-24"
        self.client._account_request()  # must not raise
        self.assertEqual(self.client.daily_calls, 1)


class RequestTest(unittest.TestCase):
    """Requests go through requests.request, mocked here, so nothing reaches
    Spotify."""

    def setUp(self):
        self.client = SpotifyClient("id", "secret", "refresh", daily_call_limit=100)

    def test_token_endpoint_429_is_a_rate_limit(self):
        response = mock.Mock(status_code=429, headers={"Retry-After": "120"})
        with mock.patch.object(spotify_client.requests, "request", return_value=response) as request:
            with self.assertRaises(SpotifyRateLimitError) as ctx:
                self.client.get_latest_releases("a1")

        self.assertEqual(ctx.exception.retry_after, 120)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[:2], ("POST", spotify_client.TOKEN_URL))

    def test_failed_request_still_paces_the_next_one(self):
        with (
            mock.patch.object(spotify_client.requests, "request", side_effect=requests.ConnectionError("down")),
            mock.patch.object(spotify_client.time, "sleep") as sleep,
        ):
            for _ in range(2):
                with self.assertRaises(SpotifyUnavailableError):
                    self.client.get_latest_releases("a1")

        # The first request goes out at once; the retry waits its turn.
        self.assertEqual(sleep.call_count, 1)
        self.assertGreater(sleep.call_args.args[0], spotify_client.MIN_REQUEST_INTERVAL - 1)

    def test_token_error_keeps_spotifys_explanation(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b'{"error":"invalid_grant","error_description":"Refresh token revoked"}'
        with mock.patch.object(spotify_client.requests, "request", return_value=response):
            with self.assertRaises(SpotifyUnavailableError) as ctx:
                self.client.get_followed_artists()

        self.assertIn("invalid_grant", str(ctx.exception))
        self.assertIn("400", str(ctx.exception))
        self.assertIn("python -m spotify_tracker.login", str(ctx.exception))

    def test_dns_failure_is_explained_in_plain_words(self):
        # The exact chain requests raises when the machine is offline.
        import socket

        from urllib3.connection import HTTPSConnection
        from urllib3.exceptions import MaxRetryError, NameResolutionError

        host = "api.spotify.com"
        try:
            try:
                raise socket.gaierror(-3, "Temporary failure in name resolution")
            except socket.gaierror as gai:
                raise NameResolutionError(host, HTTPSConnection(host), gai) from gai
        except NameResolutionError as nre:
            retry = MaxRetryError(None, "/v1/x", nre)
        err = requests.ConnectionError(retry, request=requests.Request("GET", f"https://{host}/v1/x").prepare())

        with mock.patch.object(spotify_client.requests, "request", side_effect=err):
            with self.assertRaises(SpotifyUnavailableError) as ctx:
                self.client.get_latest_releases("a1")

        message = str(ctx.exception)
        self.assertIn("DNS lookup failed", message)
        self.assertIn("api.spotify.com", message)
        self.assertNotIn("HTTPSConnectionPool", message)

    def test_timeout_is_explained_in_plain_words(self):
        with mock.patch.object(spotify_client.requests, "request", side_effect=requests.ReadTimeout("slow")):
            with self.assertRaises(SpotifyUnavailableError) as ctx:
                self.client.get_latest_releases("a1")
        self.assertIn("didn't answer in time", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
