import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values

from spotify_tracker import config, setup

TEMPLATE = (Path(__file__).resolve().parent.parent / "config.env.example").read_text(encoding="utf-8")


class _SetupTestBase(unittest.TestCase):
    """Each test points config's paths at a temporary directory, so the real
    config.env is never read or written. The machine has no systemd unless a
    test sets self.systemd, and install_bot_service.sh never really runs:
    self.service_run records it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "config.env.example").write_text(TEMPLATE, encoding="utf-8")
        for name, path in (
            ("ENV_PATH", self.root / "config.env"),
            ("LEGACY_ENV_PATH", self.root / ".env"),
            ("TEMPLATE_PATH", self.root / "config.env.example"),
        ):
            patcher = mock.patch.object(config, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.unit_path = self.root / "units" / setup.BOT_SERVICE
        self.unit_path.parent.mkdir()
        os.environ["SYSTEMD_UNIT_DIR"] = str(self.unit_path.parent)
        for name, attr, kwargs in (
            ("has_systemd", "systemd", {"return_value": False}),
            ("_run_service_script", "service_run", {"return_value": True}),
        ):
            patcher = mock.patch.object(setup, name, **kwargs)
            setattr(self, attr, patcher.start())
            self.addCleanup(patcher.stop)

    def _values(self) -> dict[str, str | None]:
        return dotenv_values(self.root / "config.env")

    def _run(
        self, *, tty: bool, inputs: list[str] = (), secrets: list[str] = (), argv: list[str] = ()
    ) -> mock.Mock:
        """inputs answer the input() prompts in order, the first being the
        Telegram-or-Discord question ("" keeps Telegram); secrets answer the
        getpass() ones."""
        with (
            mock.patch("sys.stdin.isatty", return_value=tty),
            mock.patch("sys.stdout.isatty", return_value=tty),
            mock.patch("builtins.input", side_effect=list(inputs)) as ask,
            mock.patch("getpass.getpass", side_effect=list(secrets)),
            mock.patch.object(setup.login, "main") as login_main,
            mock.patch("builtins.print") as printed,
        ):
            setup.main(list(argv))
        self.printed = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
        self.prompts = [call.args[0] for call in ask.call_args_list]
        return login_main


class SetupTest(_SetupTestBase):
    def test_creates_config_env_from_the_template(self):
        login_main = self._run(tty=False)
        self.assertEqual((self.root / "config.env").read_text(encoding="utf-8"), TEMPLATE)
        login_main.assert_not_called()

    def test_renames_a_legacy_env_keeping_its_settings(self):
        (self.root / ".env").write_text("SPOTIFY_CLIENT_ID=old-id\n", encoding="utf-8")
        self._run(tty=False)
        self.assertFalse((self.root / ".env").exists())
        self.assertEqual(self._values()["SPOTIFY_CLIENT_ID"], "old-id")

    def test_existing_config_env_is_left_alone(self):
        (self.root / "config.env").write_text("SPOTIFY_CLIENT_ID=kept\n", encoding="utf-8")
        (self.root / ".env").write_text("SPOTIFY_CLIENT_ID=legacy\n", encoding="utf-8")
        self._run(tty=False)
        self.assertEqual(self._values()["SPOTIFY_CLIENT_ID"], "kept")
        self.assertTrue((self.root / ".env").exists())

    def test_prompts_for_placeholders_then_runs_the_login(self):
        login_main = self._run(
            tty=True,
            # client ID, chat ID, then "run the login?"
            inputs=["", "new-id", "42", ""],
            secrets=["new-secret", "bot-token"],
        )
        values = self._values()
        self.assertEqual(values["SPOTIFY_CLIENT_ID"], "new-id")
        self.assertEqual(values["SPOTIFY_CLIENT_SECRET"], "new-secret")
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "bot-token")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "42")
        login_main.assert_called_once()
        # The login reads the client credentials from the environment.
        self.assertEqual(os.environ["SPOTIFY_CLIENT_ID"], "new-id")

    def _write_complete_config(self):
        (self.root / "config.env").write_text(
            "SPOTIFY_CLIENT_ID=old-id\nSPOTIFY_CLIENT_SECRET=old-secret-abcd\nSPOTIFY_REFRESH_TOKEN=token\n"
            "TELEGRAM_BOT_TOKEN=old-bot-token-wxyz\nTELEGRAM_CHAT_ID=\n",
            encoding="utf-8",
        )

    def test_enter_keeps_the_settings_already_set(self):
        self._write_complete_config()
        # Keep ID, secret, bot token; the chat ID is missing; keep the login.
        login_main = self._run(tty=True, inputs=["", "", "", "", "7", ""])
        values = self._values()
        self.assertEqual(values["SPOTIFY_CLIENT_ID"], "old-id")
        self.assertEqual(values["TELEGRAM_BOT_TOKEN"], "old-bot-token-wxyz")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "7")
        login_main.assert_not_called()

    def test_a_setting_already_set_can_be_replaced(self):
        self._write_complete_config()
        # Keep ID and secret, replace the bot token, chat ID, keep the login.
        login_main = self._run(tty=True, inputs=["", "", "", "n", "7", ""], secrets=["new-bot-token"])
        self.assertEqual(self._values()["TELEGRAM_BOT_TOKEN"], "new-bot-token")
        login_main.assert_not_called()

    def test_declining_to_replace_with_an_empty_value_keeps_the_old_one(self):
        self._write_complete_config()
        login_main = self._run(tty=True, inputs=["", "n", "", "", "", "", ""])
        self.assertEqual(self._values()["SPOTIFY_CLIENT_ID"], "old-id")
        login_main.assert_not_called()

    def test_secrets_are_shown_only_by_their_last_characters(self):
        self._write_complete_config()
        with (
            mock.patch("sys.stdin.isatty", return_value=True),
            mock.patch("sys.stdout.isatty", return_value=True),
            mock.patch("builtins.input", side_effect=["", "", "", "", "7", ""]) as ask,
            mock.patch("getpass.getpass", side_effect=[]),
            mock.patch.object(setup.login, "main"),
            mock.patch("builtins.print"),
        ):
            setup.main([])
        prompts = " ".join(call.args[0] for call in ask.call_args_list)
        self.assertIn("old-id", prompts)
        self.assertIn("...abcd", prompts)
        self.assertIn("...wxyz", prompts)
        self.assertNotIn("old-secret", prompts)
        self.assertNotIn("old-bot-token", prompts)

    def test_declining_to_keep_the_login_runs_it_again(self):
        self._write_complete_config()
        login_main = self._run(tty=True, inputs=["", "", "", "", "7", "n"])
        login_main.assert_called_once()

    def test_new_spotify_credentials_offer_a_new_login(self):
        self._write_complete_config()
        # Replace the client ID; then the login is offered, not "keep it?".
        login_main = self._run(tty=True, inputs=["", "n", "new-id", "", "", "7", ""])
        self.assertEqual(self._values()["SPOTIFY_CLIENT_ID"], "new-id")
        login_main.assert_called_once()
        self.assertEqual(os.environ["SPOTIFY_CLIENT_ID"], "new-id")

    def test_skipped_client_credentials_skip_the_login(self):
        login_main = self._run(tty=True, inputs=["", "", ""], secrets=["", ""])
        login_main.assert_not_called()
        self.assertEqual(self._values()["SPOTIFY_CLIENT_ID"], "your_spotify_client_id")

    def test_declining_the_login_skips_it(self):
        login_main = self._run(tty=True, inputs=["", "id", "1", "n"], secrets=["secret", "bot"])
        login_main.assert_not_called()

    def test_a_bot_service_left_from_discord_can_be_removed(self):
        self._write_complete_config()
        self.unit_path.touch()
        # Keep everything, chat ID 7, keep the login, then remove the service.
        self._run(tty=True, inputs=["", "", "", "", "7", "", ""])
        self.service_run.assert_called_once_with("--uninstall")


class ServiceScriptTest(unittest.TestCase):
    def test_runs_install_bot_service_with_bash_and_reports_failure(self):
        with (
            mock.patch.object(setup.subprocess, "run", return_value=mock.Mock(returncode=1)) as run,
            mock.patch("builtins.print"),
        ):
            self.assertFalse(setup._run_service_script("--uninstall"))
        self.assertEqual(run.call_args.args[0], ["bash", str(setup.SERVICE_SCRIPT), "--uninstall"])


CHANNELS = [
    {"id": "100", "name": "general", "category": ""},
    {"id": "200", "name": "releases", "category": "Music"},
]


class DiscordSetupTest(_SetupTestBase):
    """DiscordAPI is replaced, so nothing reaches Discord."""

    def setUp(self):
        super().setUp()
        self.api = mock.Mock()
        self.api.guild.return_value = {"id": "42", "name": "My Server"}
        self.api.text_channels.return_value = CHANNELS
        self.api.current_user.return_value = {"id": "555"}
        patcher = mock.patch.object(setup, "DiscordAPI", return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_spotify_config(self, extra: str = "") -> None:
        (self.root / "config.env").write_text(
            "SPOTIFY_CLIENT_ID=id\nSPOTIFY_CLIENT_SECRET=secret-abcd\nSPOTIFY_REFRESH_TOKEN=token\n" + extra,
            encoding="utf-8",
        )

    def test_choosing_discord_asks_for_its_settings_and_a_channel(self):
        self._write_spotify_config()
        login_main = self._run(
            tty=True,
            # discord; keep ID and secret; server ID, user ID; channel 2;
            # send the test message; keep the Spotify login.
            inputs=["d", "", "", "42", "7", "2", "", ""],
            secrets=["discord-token"],
        )
        values = self._values()
        self.assertEqual(values["NOTIFIER"], "discord")
        self.assertEqual(values["DISCORD_BOT_TOKEN"], "discord-token")
        self.assertEqual(values["DISCORD_GUILD_ID"], "42")
        self.assertEqual(values["DISCORD_USER_ID"], "7")
        self.assertEqual(values["DISCORD_CHANNEL_ID"], "200")
        self.api.text_channels.assert_called_once_with("42")
        self.assertEqual(self.api.send_message.call_args.args[0], "200")
        self.assertIn("#releases  (Music)", self.printed)
        login_main.assert_not_called()

    def _write_discord_config(self) -> None:
        self._write_spotify_config(
            "NOTIFIER=discord\nDISCORD_BOT_TOKEN=discord-token-wxyz\nDISCORD_GUILD_ID=42\n"
            "DISCORD_USER_ID=7\nDISCORD_CHANNEL_ID=100\n"
        )

    def test_the_current_channel_can_be_kept(self):
        self._write_discord_config()
        # Keep: discord, ID, secret, token, server, user, #general, login.
        self._run(tty=True, inputs=["", "", "", "", "", "", "", ""])
        self.assertEqual(self._values()["DISCORD_CHANNEL_ID"], "100")
        self.api.text_channels.assert_called_once_with("42")
        self.api.send_message.assert_not_called()

    def test_a_new_server_asks_for_a_channel_again(self):
        self._write_discord_config()
        # Keep up to the server, which changes to 43; then channel 1, no test.
        self._run(tty=True, inputs=["", "", "", "", "n", "43", "", "1", "n", ""])
        self.assertEqual(self._values()["DISCORD_GUILD_ID"], "43")
        self.assertEqual(self._values()["DISCORD_CHANNEL_ID"], "100")
        self.api.text_channels.assert_called_once_with("43")

    def test_a_rejected_token_is_explained(self):
        self._write_discord_config()
        self.api.guild.side_effect = setup.DiscordError("401", status=401)
        self._run(tty=True, inputs=["", "", "", "", "", "", ""])
        self.assertIn("rejected the bot token", self.printed)

    def test_a_bot_outside_the_server_gets_an_invite_link(self):
        self._write_discord_config()
        self.api.guild.side_effect = setup.DiscordError("404", status=404)
        self._run(tty=True, inputs=["", "", "", "", "", "", ""])
        self.assertIn("client_id=555", self.printed)

    # Keep every setting: notifier, ID, secret, token, server, user, channel, login.
    KEEP_ALL = [""] * 8

    def test_offers_to_install_the_bot_service(self):
        self._write_discord_config()
        self.systemd.return_value = True
        self._run(tty=True, inputs=self.KEEP_ALL + [""])
        self.service_run.assert_called_once_with()

    def test_the_bot_service_install_can_be_declined(self):
        self._write_discord_config()
        self.systemd.return_value = True
        self._run(tty=True, inputs=self.KEEP_ALL + ["n"])
        self.service_run.assert_not_called()
        self.assertIn("./install_bot_service.sh", self.printed)

    def test_an_installed_bot_service_is_restarted(self):
        self._write_discord_config()
        self.systemd.return_value = True
        self.unit_path.touch()
        self._run(tty=True, inputs=self.KEEP_ALL + [""])
        self.assertIn("Restart it", self.prompts[-1])
        self.service_run.assert_called_once_with()

    def test_the_bot_service_waits_for_the_missing_settings(self):
        # No Spotify login yet, so the bot couldn't start.
        self._write_discord_config()
        path = self.root / "config.env"
        path.write_text(path.read_text(encoding="utf-8").replace("REFRESH_TOKEN=token", "REFRESH_TOKEN="), encoding="utf-8")
        self.systemd.return_value = True
        # Keep everything; decline the login.
        self._run(tty=True, inputs=[""] * 7 + ["n"])
        self.service_run.assert_not_called()
        self.assertIn("Not starting the Discord bot service yet: SPOTIFY_REFRESH_TOKEN", self.printed)

    def test_without_systemd_the_bot_is_to_be_run_by_hand(self):
        self._write_discord_config()
        self._run(tty=True, inputs=self.KEEP_ALL)
        self.service_run.assert_not_called()
        self.assertIn("no systemd", self.printed)

    def test_no_bot_service_flag_skips_the_question(self):
        self._write_discord_config()
        self.systemd.return_value = True
        self._run(tty=True, inputs=self.KEEP_ALL, argv=["--no-bot-service"])
        self.service_run.assert_not_called()

    def test_non_interactive_run_points_to_the_service_installer(self):
        self._write_discord_config()
        self.systemd.return_value = True
        self._run(tty=False)
        self.assertIn("isn't installed; install it with: ./install_bot_service.sh", self.printed)

    def test_missing_channel_is_listed_for_discord(self):
        self._write_spotify_config(
            "NOTIFIER=discord\nDISCORD_BOT_TOKEN=t\nDISCORD_GUILD_ID=42\nDISCORD_USER_ID=7\nDISCORD_CHANNEL_ID=\n"
        )
        self._run(tty=False)
        self.assertIn("Still missing", self.printed)
        self.assertIn("DISCORD_CHANNEL_ID", self.printed)
        self.assertNotIn("TELEGRAM", self.printed)


if __name__ == "__main__":
    unittest.main()
