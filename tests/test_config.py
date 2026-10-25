import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spotify_tracker import config, login
from spotify_tracker.main import wants_appears_on

REQUIRED = {
    "SPOTIFY_CLIENT_ID": "id",
    "SPOTIFY_CLIENT_SECRET": "secret",
    "SPOTIFY_REFRESH_TOKEN": "refresh",
    "TELEGRAM_BOT_TOKEN": "bot",
    "TELEGRAM_CHAT_ID": "1",
}


class LoadConfigTest(unittest.TestCase):
    def test_appears_on_artists_are_trimmed_and_casefolded(self):
        env = dict(REQUIRED, APPEARS_ON_ARTISTS=" Bastille , ,EMINEM,bastille ")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = config.load_config()
        self.assertEqual(cfg.appears_on_artists, frozenset({"bastille", "eminem"}))

    def test_name_with_comma_matches_by_id(self):
        artist = {"id": "4V8LLVI7PbaPR0K2TGSxFF", "name": "Tyler, The Creator"}
        env = dict(REQUIRED, APPEARS_ON_ARTISTS="4V8LLVI7PbaPR0K2TGSxFF")
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = config.load_config()
        self.assertTrue(wants_appears_on(artist, cfg.appears_on_artists))

    def test_missing_variables_exit(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                config.load_config()
        self.assertIn("SPOTIFY_REFRESH_TOKEN", str(ctx.exception))

    def test_telegram_is_the_default_notifier(self):
        with mock.patch.dict(os.environ, REQUIRED, clear=True):
            self.assertEqual(config.load_config().notifier, "telegram")

    def test_discord_needs_only_the_discord_settings(self):
        spotify = {k: v for k, v in REQUIRED.items() if k.startswith("SPOTIFY_")}
        discord = {
            "NOTIFIER": "Discord",
            "DISCORD_BOT_TOKEN": "token",
            "DISCORD_GUILD_ID": "1",
            "DISCORD_USER_ID": "2",
            "DISCORD_CHANNEL_ID": "3",
        }
        with mock.patch.dict(os.environ, dict(spotify, **discord), clear=True):
            cfg = config.load_config()
        self.assertEqual(cfg.notifier, "discord")
        self.assertEqual(cfg.discord_channel_id, "3")
        self.assertEqual(cfg.telegram_bot_token, "")

    def test_discord_without_a_channel_exits(self):
        spotify = {k: v for k, v in REQUIRED.items() if k.startswith("SPOTIFY_")}
        env = dict(spotify, NOTIFIER="discord", DISCORD_BOT_TOKEN="t", DISCORD_GUILD_ID="1", DISCORD_USER_ID="2")
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                config.load_config()
        self.assertIn("DISCORD_CHANNEL_ID", str(ctx.exception))
        self.assertNotIn("TELEGRAM", str(ctx.exception))

    def test_unknown_notifier_exits(self):
        with mock.patch.dict(os.environ, dict(REQUIRED, NOTIFIER="slack"), clear=True):
            with self.assertRaises(SystemExit) as ctx:
                config.load_config()
        self.assertIn("slack", str(ctx.exception))


class EnvPathTest(unittest.TestCase):
    def test_env_path_is_the_repository_root(self):
        repo_root = Path(__file__).resolve().parent.parent
        self.assertEqual(config.ENV_PATH, repo_root / "config.env")

    def test_legacy_env_is_used_only_when_config_env_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(config, "ENV_PATH", root / "config.env"),
                mock.patch.object(config, "LEGACY_ENV_PATH", root / ".env"),
            ):
                self.assertEqual(config.env_path(), root / "config.env")
                (root / ".env").write_text("X=1\n", encoding="utf-8")
                self.assertEqual(config.env_path(), root / ".env")
                (root / "config.env").write_text("X=2\n", encoding="utf-8")
                self.assertEqual(config.env_path(), root / "config.env")

    def test_login_writes_the_file_config_reads(self):
        with (
            mock.patch.object(login, "load_client_credentials", return_value=("id", "secret", "uri")),
            mock.patch.object(login, "run_login_flow", return_value="new-refresh-token"),
            mock.patch.object(login, "set_env_value") as set_env_value,
            mock.patch("builtins.print"),
        ):
            login.main()
        set_env_value.assert_called_once_with(config.env_path(), "SPOTIFY_REFRESH_TOKEN", "new-refresh-token")


if __name__ == "__main__":
    unittest.main()
