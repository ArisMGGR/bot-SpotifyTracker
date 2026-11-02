"""The always-online Discord bot: answers slash commands from one user.

Run with: python -m spotify_tracker.discord_bot
(install_bot_service.sh installs it as spotify-tracker-bot.service.)

The systemd check timer (or cron) keeps doing the scheduled checks; this
only answers commands, and only
from DISCORD_USER_ID, in the server DISCORD_GUILD_ID:
  /check           run a check now
  /status          last check, API calls used today, next scheduled check
  /channel         change the channel the alerts go to
  /pause, /resume  stop and restart the scheduled checks
Replies are visible only to that user.
"""

import logging
import sys

import discord
from discord import app_commands
from dotenv import dotenv_values

from spotify_tracker import bot_actions
from spotify_tracker.config import Config, env_path, load_config
from spotify_tracker.env_file import set_env_value

logger = logging.getLogger("spotify_tracker.discord_bot")


class AllowedUserTree(app_commands.CommandTree):
    """Runs a command only for the configured user."""

    def __init__(self, client: discord.Client, allowed_user_id: int):
        super().__init__(client)
        self.allowed_user_id = allowed_user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.allowed_user_id:
            return True
        await interaction.response.send_message("This bot only takes commands from its owner.", ephemeral=True)
        logger.info("Ignored a command from user %s", interaction.user.id)
        return False


class TrackerBot(discord.Client):
    def __init__(self, cfg: Config):
        # Slash commands need no privileged intents; guilds is enough to see
        # the server's channels and the bot's own permissions in them.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents)
        self.guild_object = discord.Object(id=int(cfg.discord_guild_id))
        self.tree = AllowedUserTree(self, int(cfg.discord_user_id))
        self._add_commands()

    def _add_commands(self) -> None:
        guild = self.guild_object

        @self.tree.command(name="check", description="Check for new releases now", guild=guild)
        async def check(interaction: discord.Interaction) -> None:
            # A check can take minutes; defer so Discord doesn't time out.
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await bot_actions.run_check(), ephemeral=True)

        @self.tree.command(name="status", description="Show the tracker's status", guild=guild)
        async def status(interaction: discord.Interaction) -> None:
            # Read fresh: the setup may have changed it while the bot runs.
            channel_id = dotenv_values(env_path()).get("DISCORD_CHANNEL_ID")
            mention = f"<#{channel_id}>" if channel_id else "not chosen (run the setup or /channel)"
            await interaction.response.send_message(bot_actions.status_text(mention), ephemeral=True)

        @self.tree.command(name="channel", description="Change the channel new releases are posted in", guild=guild)
        @app_commands.describe(channel="The channel for the alerts")
        async def channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
            permissions = channel.permissions_for(channel.guild.me)
            if not (permissions.view_channel and permissions.send_messages):
                await interaction.response.send_message(
                    f"I can't post in {channel.mention}: give me the View Channel and Send Messages permissions there.",
                    ephemeral=True,
                )
                return
            # The scheduled check reads config.env on every run.
            set_env_value(env_path(), "DISCORD_CHANNEL_ID", str(channel.id))
            logger.info("Alert channel changed to #%s (%s)", channel.name, channel.id)
            await interaction.response.send_message(f"New releases will be posted in {channel.mention}.", ephemeral=True)

        @self.tree.command(name="pause", description="Stop the scheduled checks", guild=guild)
        async def pause(interaction: discord.Interaction) -> None:
            bot_actions.set_paused(True)
            logger.info("Scheduled checks paused")
            await interaction.response.send_message(
                "Scheduled checks are paused. /check still works; /resume restarts them.", ephemeral=True
            )

        @self.tree.command(name="resume", description="Restart the scheduled checks", guild=guild)
        async def resume(interaction: discord.Interaction) -> None:
            bot_actions.set_paused(False)
            logger.info("Scheduled checks resumed")
            await interaction.response.send_message("Scheduled checks are running again.", ephemeral=True)

    async def setup_hook(self) -> None:
        # Registering the commands for one server makes them show up at once
        # (global commands can take an hour).
        await self.tree.sync(guild=self.guild_object)

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s; answering commands from user %s", self.user, self.tree.allowed_user_id)


# Exit status for settings problems. The systemd unit doesn't restart on it,
# since restarting can't fix config.env.
CONFIG_ERROR = 2


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        cfg = load_config()
    except SystemExit as err:
        print(err, file=sys.stderr)
        sys.exit(CONFIG_ERROR)
    if cfg.notifier != "discord":
        print("NOTIFIER in config.env isn't discord; run 'python -m spotify_tracker.setup' to switch.", file=sys.stderr)
        sys.exit(CONFIG_ERROR)
    try:
        bot = TrackerBot(cfg)
    except ValueError:
        print("DISCORD_GUILD_ID and DISCORD_USER_ID in config.env must be numeric IDs.", file=sys.stderr)
        sys.exit(CONFIG_ERROR)
    try:
        # discord.py logs through the root logger set up above.
        bot.run(cfg.discord_bot_token, log_handler=None)
    except discord.LoginFailure as err:
        print(f"Discord rejected DISCORD_BOT_TOKEN in config.env: {err}", file=sys.stderr)
        sys.exit(CONFIG_ERROR)


if __name__ == "__main__":
    main()
