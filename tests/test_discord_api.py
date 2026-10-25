import json
import unittest
from unittest import mock

import requests

from spotify_tracker import discord_api
from spotify_tracker.discord_api import DiscordAPI, DiscordError, DiscordNotifier, html_to_markdown

TOKEN = "MTIz.TEST-TOKEN.not-real"


def _response(status: int, body) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body).encode() if body is not None else b""
    return response


class HtmlToMarkdownTest(unittest.TestCase):
    def test_bold_tags_become_markdown(self):
        self.assertEqual(html_to_markdown("<b>Bastille</b> just released:"), "**Bastille** just released:")

    def test_entities_are_decoded_and_markdown_is_escaped(self):
        self.assertEqual(html_to_markdown("<b>Simon &amp; Garfunkel_*</b>"), "**Simon & Garfunkel\\_\\***")

    def test_urls_are_left_alone(self):
        url = "https://open.spotify.com/album/4a_b*c"
        self.assertEqual(html_to_markdown(f"Released: 2026\n{url}"), f"Released: 2026\n{url}")


class DiscordAPITest(unittest.TestCase):
    def setUp(self):
        self.api = DiscordAPI(TOKEN)

    def _patch_request(self, *responses):
        return mock.patch.object(self.api._session, "request", side_effect=list(responses))

    def test_sends_the_bot_token_and_a_discord_user_agent(self):
        headers = self.api._session.headers
        self.assertEqual(headers["Authorization"], f"Bot {TOKEN}")
        self.assertTrue(headers["User-Agent"].startswith("DiscordBot ("))

    def test_text_channels_are_listed_in_discord_order_with_categories(self):
        channels = [
            {"id": "c2", "type": 4, "name": "Music", "position": 1},
            {"id": "c1", "type": 4, "name": "General", "position": 0},
            {"id": "t3", "type": 0, "name": "releases", "parent_id": "c2", "position": 0},
            {"id": "v1", "type": 2, "name": "Voice", "parent_id": "c1", "position": 0},
            {"id": "t2", "type": 0, "name": "chat", "parent_id": "c1", "position": 1},
            {"id": "t1", "type": 5, "name": "news", "parent_id": "c1", "position": 0},
            {"id": "t0", "type": 0, "name": "welcome", "position": 0},
        ]
        with self._patch_request(_response(200, channels)) as request:
            result = self.api.text_channels("42")
        self.assertEqual(request.call_args.args[1], f"{discord_api.API_BASE}/guilds/42/channels")
        self.assertEqual(
            [(c["name"], c["category"]) for c in result],
            [("welcome", ""), ("news", "General"), ("chat", "General"), ("releases", "Music")],
        )

    def test_messages_cant_ping_anyone(self):
        with self._patch_request(_response(200, {"id": "m1"})) as request:
            self.api.send_message("7", "@everyone new album")
        payload = request.call_args.kwargs["json"]
        self.assertEqual(payload["content"], "@everyone new album")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_long_messages_are_cut_to_discords_limit(self):
        with self._patch_request(_response(200, {})) as request:
            self.api.send_message("7", "x" * 2500)
        self.assertEqual(len(request.call_args.kwargs["json"]["content"]), discord_api.MESSAGE_LIMIT)

    def test_a_short_rate_limit_is_waited_out_once(self):
        with (
            self._patch_request(_response(429, {"retry_after": 0.5}), _response(200, {})) as request,
            mock.patch.object(discord_api.time, "sleep") as sleep,
        ):
            self.api.send_message("7", "hi")
        sleep.assert_called_once_with(0.5)
        self.assertEqual(request.call_count, 2)

    def test_a_long_rate_limit_fails_the_send(self):
        with self._patch_request(_response(429, {"retry_after": 600})):
            with self.assertRaises(DiscordError) as ctx:
                self.api.send_message("7", "hi")
        self.assertEqual(ctx.exception.status, 429)

    def test_errors_carry_the_status_and_never_the_token(self):
        with self._patch_request(_response(401, {"message": f"401: Unauthorized {TOKEN}"})):
            with self.assertRaises(DiscordError) as ctx:
                self.api.guild("42")
        self.assertEqual(ctx.exception.status, 401)
        self.assertNotIn(TOKEN, str(ctx.exception))

    def test_connection_errors_have_no_status(self):
        with self._patch_request(requests.ConnectionError("down")):
            with self.assertRaises(DiscordError) as ctx:
                self.api.guild("42")
        self.assertIsNone(ctx.exception.status)
        self.assertIn("ConnectionError", str(ctx.exception))


class DiscordNotifierTest(unittest.TestCase):
    def test_alerts_are_posted_to_the_channel_as_markdown(self):
        notifier = DiscordNotifier(TOKEN, "99")
        with mock.patch.object(notifier._api, "send_message") as send:
            notifier.send_message("\U0001F3B5 <b>Bastille</b> just released:")
        send.assert_called_once_with("99", "\U0001F3B5 **Bastille** just released:")

    def test_invite_url_asks_for_the_bot_and_its_commands(self):
        url = discord_api.invite_url("123")
        self.assertIn("client_id=123", url)
        self.assertIn("scope=bot+applications.commands", url)


if __name__ == "__main__":
    unittest.main()
