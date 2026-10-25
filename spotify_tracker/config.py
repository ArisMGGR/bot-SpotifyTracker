"""Loads required settings from the environment (and the config.env file)."""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# The repository root. config.env, state.json and logs/ all default to living
# here, so a run finds the same files whatever the working directory.
REPO_DIR = Path(__file__).resolve().parent.parent

# The project's settings file. Read here and written by spotify_tracker.login
# and spotify_tracker.setup, so all of them use the same file.
ENV_PATH = REPO_DIR / "config.env"
# Its name before the rename. Still read when it's the only one there, so an
# install that kept its old .env keeps working; spotify_tracker.setup (run by
# install.sh) renames it to config.env.
LEGACY_ENV_PATH = REPO_DIR / ".env"
TEMPLATE_PATH = REPO_DIR / "config.env.example"


def env_path() -> Path:
    """config.env, or the legacy .env if that is the only one present."""
    if not ENV_PATH.exists() and LEGACY_ENV_PATH.exists():
        return LEGACY_ENV_PATH
    return ENV_PATH


load_dotenv(env_path())

DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"

# Where alerts go, from NOTIFIER in config.env. Telegram when unset, which is
# what every config.env from before the Discord option means.
NOTIFIERS = ("telegram", "discord")
DEFAULT_NOTIFIER = "telegram"
NOTIFIER_SETTINGS = {
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
    # DISCORD_USER_ID is the one user the bot's commands answer.
    "discord": ("DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID", "DISCORD_USER_ID", "DISCORD_CHANNEL_ID"),
}


@dataclass
class Config:
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    notifier: str
    # Only the chosen notifier's settings are required; the others are "".
    telegram_bot_token: str
    telegram_chat_id: str
    discord_bot_token: str
    discord_guild_id: str
    discord_user_id: str
    discord_channel_id: str
    # Artists (names or Spotify IDs, lowercased) whose "Appears On" releases
    # are also checked, from the comma-separated APPEARS_ON_ARTISTS env var.
    # Splitting on commas means a name that itself contains one ("Tyler, The
    # Creator") has to be given by Spotify ID instead.
    appears_on_artists: frozenset[str]


def _require(name: str, missing: list[str]) -> str:
    value = os.getenv(name)
    if not value:
        missing.append(name)
    return value or ""


def load_client_credentials() -> tuple[str, str, str]:
    """Just the pieces needed to run the one-time login flow (no refresh token yet)."""
    missing: list[str] = []
    client_id = _require("SPOTIFY_CLIENT_ID", missing)
    client_secret = _require("SPOTIFY_CLIENT_SECRET", missing)
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", DEFAULT_REDIRECT_URI)

    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + ". Run 'python -m spotify_tracker.setup' (or copy config.env.example to config.env) "
            "and fill in SPOTIFY_CLIENT_ID/SPOTIFY_CLIENT_SECRET first."
        )
    return client_id, client_secret, redirect_uri


def selected_notifier() -> str:
    notifier = (os.getenv("NOTIFIER") or DEFAULT_NOTIFIER).strip().lower()
    if notifier not in NOTIFIERS:
        raise SystemExit(f"NOTIFIER must be one of: {', '.join(NOTIFIERS)} (got '{notifier}').")
    return notifier


def load_config() -> Config:
    missing: list[str] = []
    client_id = _require("SPOTIFY_CLIENT_ID", missing)
    client_secret = _require("SPOTIFY_CLIENT_SECRET", missing)
    refresh_token = _require("SPOTIFY_REFRESH_TOKEN", missing)
    notifier = selected_notifier()
    for name in NOTIFIER_SETTINGS[notifier]:
        _require(name, missing)

    if missing:
        hint = ""
        if "SPOTIFY_REFRESH_TOKEN" in missing:
            hint = " Run 'python -m spotify_tracker.login' once to generate SPOTIFY_REFRESH_TOKEN."
        raise SystemExit(
            "Missing required environment variables: " + ", ".join(missing) + "." + hint
        )

    return Config(
        spotify_client_id=client_id,
        spotify_client_secret=client_secret,
        spotify_refresh_token=refresh_token,
        notifier=notifier,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        discord_guild_id=os.getenv("DISCORD_GUILD_ID", ""),
        discord_user_id=os.getenv("DISCORD_USER_ID", ""),
        discord_channel_id=os.getenv("DISCORD_CHANNEL_ID", ""),
        appears_on_artists=frozenset(
            name.strip().casefold()
            for name in os.getenv("APPEARS_ON_ARTISTS", "").split(",")
            if name.strip()
        ),
    )
