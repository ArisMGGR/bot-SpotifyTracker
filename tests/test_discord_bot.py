import asyncio
import importlib.util
import unittest
from unittest import mock

from spotify_tracker.config import Config

HAVE_DISCORD = importlib.util.find_spec("discord") is not None
if HAVE_DISCORD:
    import discord

    from spotify_tracker import discord_bot

OWNER = 111
CFG = Config(
    spotify_client_id="id",
    spotify_client_secret="secret",
    spotify_refresh_token="refresh",
    notifier="discord",
    telegram_bot_token="",
    telegram_chat_id="",
    discord_bot_token="token",
    discord_guild_id="222",
    discord_user_id=str(OWNER),
    discord_channel_id="333",
    appears_on_artists=frozenset(),
)


def _interaction(user_id: int = OWNER) -> mock.Mock:
    interaction = mock.Mock()
    interaction.user.id = user_id
    interaction.response.send_message = mock.AsyncMock()
    interaction.response.defer = mock.AsyncMock()
    interaction.followup.send = mock.AsyncMock()
    return interaction


@unittest.skipUnless(HAVE_DISCORD, "needs discord.py")
class TrackerBotTest(unittest.TestCase):
    """The commands are called directly; nothing connects to Discord."""

    def setUp(self):
        self.bot = discord_bot.TrackerBot(CFG)

    def _command(self, name: str):
        return self.bot.tree.get_command(name, guild=self.bot.guild_object)

    def _reply(self, interaction: mock.Mock) -> str:
        return interaction.response.send_message.call_args.args[0]

    def test_the_five_commands_are_registered_for_the_server_only(self):
        names = {c.name for c in self.bot.tree.get_commands(guild=self.bot.guild_object)}
        self.assertEqual(names, {"check", "status", "channel", "pause", "resume"})
        self.assertEqual(self.bot.tree.get_commands(), [])

    def test_only_the_owner_passes_the_check(self):
        self.assertTrue(asyncio.run(self.bot.tree.interaction_check(_interaction())))

        stranger = _interaction(user_id=999)
        self.assertFalse(asyncio.run(self.bot.tree.interaction_check(stranger)))
        self.assertTrue(stranger.response.send_message.call_args.kwargs["ephemeral"])

    def test_check_defers_then_replies_with_the_result(self):
        interaction = _interaction()
        with mock.patch.object(discord_bot.bot_actions, "run_check", mock.AsyncMock(return_value="Check finished.")):
            asyncio.run(self._command("check").callback(interaction))
        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with("Check finished.", ephemeral=True)

    def test_pause_and_resume(self):
        with mock.patch.object(discord_bot.bot_actions, "set_paused") as set_paused:
            asyncio.run(self._command("pause").callback(_interaction()))
            asyncio.run(self._command("resume").callback(_interaction()))
        self.assertEqual(set_paused.call_args_list, [mock.call(True), mock.call(False)])

    def _channel(self, can_send: bool) -> mock.Mock:
        channel = mock.Mock(spec=discord.TextChannel)
        channel.id = 444
        channel.name = "releases"
        channel.mention = "<#444>"
        channel.permissions_for.return_value = mock.Mock(view_channel=True, send_messages=can_send)
        return channel

    def test_channel_is_saved_when_the_bot_can_post_there(self):
        interaction = _interaction()
        with mock.patch.object(discord_bot, "set_env_value") as set_env_value:
            asyncio.run(self._command("channel").callback(interaction, self._channel(can_send=True)))
        self.assertEqual(set_env_value.call_args.args[1:], ("DISCORD_CHANNEL_ID", "444"))
        self.assertIn("<#444>", self._reply(interaction))

    def test_channel_is_refused_when_the_bot_cant_post_there(self):
        interaction = _interaction()
        with mock.patch.object(discord_bot, "set_env_value") as set_env_value:
            asyncio.run(self._command("channel").callback(interaction, self._channel(can_send=False)))
        set_env_value.assert_not_called()
        self.assertIn("can't post", self._reply(interaction))

    def test_main_refuses_a_telegram_config(self):
        telegram_cfg = Config(**dict(CFG.__dict__, notifier="telegram"))
        with (
            mock.patch.object(discord_bot, "load_config", return_value=telegram_cfg),
            mock.patch.object(discord_bot.logging, "basicConfig"),
            mock.patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit) as ctx:
                discord_bot.main()
        self.assertEqual(ctx.exception.code, discord_bot.CONFIG_ERROR)


if __name__ == "__main__":
    unittest.main()
