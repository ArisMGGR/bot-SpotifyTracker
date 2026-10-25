"""Talks to Discord's REST API: sends alerts and lists a server's channels.

Plain HTTP with requests, so the scheduled check needs no connection to
Discord's gateway. The always-online bot that answers commands is
spotify_tracker.discord_bot.
"""

import html
import re
import time

import requests

API_BASE = "https://discord.com/api/v10"
# Discord requires bot User-Agents in this form.
USER_AGENT = "DiscordBot (https://github.com/ArisMGGR/bot-SpotifyNewTracks, 1.0)"

# Channel types that can hold the alerts: text and announcement channels.
TEXT_CHANNEL_TYPES = (0, 5)
CATEGORY_TYPE = 4

# View Channel + Send Messages + Embed Links, for the invite link.
BOT_PERMISSIONS = 1024 | 2048 | 16384

MESSAGE_LIMIT = 2000

# A 429 asks us to wait this long at most before one retry; anything longer
# is reported as a failed send, which the next run retries.
MAX_RETRY_AFTER = 10


class DiscordError(Exception):
    """A failed Discord request. status is the HTTP status, or None if
    Discord couldn't be reached."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def invite_url(application_id: str) -> str:
    """Link that adds the bot (with slash commands) to a server."""
    return (
        "https://discord.com/oauth2/authorize"
        f"?client_id={application_id}&scope=bot+applications.commands&permissions={BOT_PERMISSIONS}"
    )


# Characters Discord markdown would act on.
_MARKDOWN_SPECIAL = re.compile(r"([\\*_~`|>#\[\]()-])")
_URL = re.compile(r"(https?://\S+)")
_BOLD = re.compile(r"(</?b>)")


def _escape_markdown(text: str) -> str:
    # URLs stay as they are, or the backslashes would end up in the link.
    parts = _URL.split(text)
    return "".join(part if _URL.fullmatch(part) else _MARKDOWN_SPECIAL.sub(r"\\\1", part) for part in parts)


def html_to_markdown(text: str) -> str:
    """Turns an alert as main builds it (Telegram HTML: escaped text and <b>
    tags, nothing else) into Discord markdown."""
    out = []
    for part in _BOLD.split(text):
        if part in ("<b>", "</b>"):
            out.append("**")
        else:
            out.append(_escape_markdown(html.unescape(part)))
    return "".join(out)


class DiscordAPI:
    def __init__(self, bot_token: str):
        self._bot_token = bot_token
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bot {bot_token}", "User-Agent": USER_AGENT})

    def _redact(self, text: str) -> str:
        return text.replace(self._bot_token, "<bot-token>") if self._bot_token else text

    def _request(self, method: str, path: str, **kwargs):
        for attempt in range(2):
            try:
                response = self._session.request(method, API_BASE + path, timeout=15, **kwargs)
            except requests.RequestException as err:
                # "from None" keeps the original exception out of the logged
                # traceback, like TelegramNotifier.
                raise DiscordError(self._redact(f"{type(err).__name__}: {err}")) from None
            if response.status_code == 429 and attempt == 0:
                try:
                    retry_after = float(response.json().get("retry_after", 0))
                except ValueError:
                    retry_after = MAX_RETRY_AFTER + 1
                if retry_after <= MAX_RETRY_AFTER:
                    time.sleep(retry_after)
                    continue
            if not response.ok:
                raise DiscordError(
                    self._redact(f"Discord API error {response.status_code}: {response.text}"),
                    status=response.status_code,
                )
            return response.json() if response.content else None
        raise AssertionError("unreachable")

    def current_user(self) -> dict:
        """The bot's own user; its id is the application id for invite links."""
        return self._request("GET", "/users/@me")

    def guild(self, guild_id: str) -> dict:
        return self._request("GET", f"/guilds/{guild_id}")

    def text_channels(self, guild_id: str) -> list[dict]:
        """The server's text and announcement channels in the order Discord
        shows them, each with a "category" name ("" outside any category)."""
        channels = self._request("GET", f"/guilds/{guild_id}/channels")
        categories = {c["id"]: c for c in channels if c.get("type") == CATEGORY_TYPE}

        def order(channel: dict) -> tuple:
            category = categories.get(channel.get("parent_id"))
            # Channels outside a category come first, as in the Discord app.
            return (
                category is not None,
                category.get("position", 0) if category else 0,
                channel.get("position", 0),
            )

        result = []
        for channel in sorted((c for c in channels if c.get("type") in TEXT_CHANNEL_TYPES), key=order):
            category = categories.get(channel.get("parent_id"))
            result.append({"id": channel["id"], "name": channel["name"], "category": category["name"] if category else ""})
        return result

    def send_message(self, channel_id: str, content: str) -> None:
        self._request(
            "POST",
            f"/channels/{channel_id}/messages",
            # No @everyone or role pings from release names.
            json={"content": content[:MESSAGE_LIMIT], "allowed_mentions": {"parse": []}},
        )


class DiscordNotifier:
    """Sends alerts to one channel. Takes the same text as TelegramNotifier."""

    def __init__(self, bot_token: str, channel_id: str):
        self._api = DiscordAPI(bot_token)
        self._channel_id = channel_id

    def send_message(self, text: str) -> None:
        self._api.send_message(self._channel_id, html_to_markdown(text))
