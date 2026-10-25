"""Interactive first-time setup: create config.env and run the Spotify login.

Run with: python -m spotify_tracker.setup
(install.sh runs it for you.)

0. Unless run with the project's own Python, creates ./venv if missing,
   installs requirements.txt into it and continues inside it (see
   spotify_tracker.bootstrap). --no-venv uses the current Python as it is.
1. Renames a .env from before the rename to config.env, or creates config.env
   from config.env.example if there is neither.
2. Asks whether alerts go to Telegram or Discord.
3. Goes through the settings that choice needs: asks for a value for each one
   still empty or a template placeholder (Enter skips it), and asks whether
   to keep each one already set (Enter keeps it; secrets are shown only by
   their last 4 characters).
4. For Discord, connects with the bot token, lists the server's text channels
   and lets you pick the one the alerts go to.
5. Offers the Spotify login if there is no SPOTIFY_REFRESH_TOKEN yet, or if
   the client ID or secret was just changed (the old token only works with
   the old app); otherwise asks whether to keep the existing login.
6. On a machine with systemd: for Discord, offers to install the bot as
   spotify-tracker-bot.service (install_bot_service.sh), or to restart it so
   it picks up the settings if it is installed already. For Telegram, offers
   to remove a bot service left from an earlier Discord setup.
   --no-bot-service skips this step.

With --non-interactive (or when not run from a terminal) steps 2 to 6 are
skipped, and the settings still missing are listed.
"""

import argparse
import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    # Before the imports below, which need the dependencies installed.
    from spotify_tracker.bootstrap import ensure_venv

    if not {"--no-venv", "-h", "--help"} & set(sys.argv[1:]):
        ensure_venv(sys.argv[1:])

from dotenv import dotenv_values, load_dotenv

from spotify_tracker import config, login
from spotify_tracker.discord_api import DiscordAPI, DiscordError, invite_url
from spotify_tracker.env_file import set_env_value

# (name, prompt, secret). Secrets are read without echoing them.
SPOTIFY_PROMPTS = [
    ("SPOTIFY_CLIENT_ID", "Spotify Client ID", False),
    ("SPOTIFY_CLIENT_SECRET", "Spotify Client secret", True),
]
NOTIFIER_PROMPTS = {
    "telegram": [
        ("TELEGRAM_BOT_TOKEN", "Telegram bot token", True),
        ("TELEGRAM_CHAT_ID", "Telegram chat ID", False),
    ],
    # DISCORD_CHANNEL_ID is picked from a list instead (pick_discord_channel).
    "discord": [
        ("DISCORD_BOT_TOKEN", "Discord bot token", True),
        ("DISCORD_GUILD_ID", "Discord server ID", False),
        ("DISCORD_USER_ID", "Discord user ID allowed to use the bot's commands", False),
    ],
}

# The template's stand-in values all start with this.
PLACEHOLDER_PREFIX = "your_"

BOT_SERVICE = "spotify-tracker-bot.service"
SERVICE_SCRIPT = config.REPO_DIR / "install_bot_service.sh"


def _is_missing(value: str | None) -> bool:
    return not value or value.startswith(PLACEHOLDER_PREFIX)


def _notifier(values: dict) -> str:
    notifier = (values.get("NOTIFIER") or config.DEFAULT_NOTIFIER).strip().lower()
    return notifier if notifier in config.NOTIFIERS else config.DEFAULT_NOTIFIER


def ensure_config_file() -> None:
    """Make sure config.env exists, keeping any settings from a legacy .env."""
    if config.ENV_PATH.exists():
        if config.LEGACY_ENV_PATH.exists():
            print(f"Note: {config.LEGACY_ENV_PATH} is ignored now that {config.ENV_PATH} exists; you can delete it.")
        return
    if config.LEGACY_ENV_PATH.exists():
        config.LEGACY_ENV_PATH.rename(config.ENV_PATH)
        print(f"Renamed {config.LEGACY_ENV_PATH} to {config.ENV_PATH}.")
    else:
        shutil.copyfile(config.TEMPLATE_PATH, config.ENV_PATH)
        print(f"Created {config.ENV_PATH} from {config.TEMPLATE_PATH.name}.")


def missing_settings() -> list[str]:
    values = dotenv_values(config.ENV_PATH)
    names = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REFRESH_TOKEN"]
    names += config.NOTIFIER_SETTINGS[_notifier(values)]
    return [name for name in names if _is_missing(values.get(name))]


def _yes(prompt: str) -> bool:
    """A [Y/n] question: Enter means yes."""
    return input(prompt).strip().lower() in ("", "y", "yes")


def _shown(value: str, secret: bool) -> str:
    """A setting as printed in a prompt: secrets only by their last 4 characters."""
    if not secret:
        return value
    return f"...{value[-4:]}" if len(value) > 8 else "(set)"


def choose_notifier() -> str:
    """Asks where alerts go, Enter keeping the current choice. Returns it."""
    current = _notifier(dotenv_values(config.ENV_PATH))
    while True:
        answer = input(f"Send alerts with Telegram or Discord? [{current}]: ").strip().lower()
        choice = {"": current, "t": "telegram", "d": "discord"}.get(answer, answer)
        if choice in config.NOTIFIERS:
            break
        print("Please answer telegram or discord.")
    set_env_value(config.ENV_PATH, "NOTIFIER", choice)
    return choice


def prompt_for_settings(notifier: str) -> set[str]:
    """Asks for every setting the notifier needs: a new value for a missing
    one, or whether to keep one that is already set. Returns the names that
    were changed."""
    values = dotenv_values(config.ENV_PATH)
    changed: set[str] = set()
    for name, label, secret in SPOTIFY_PROMPTS + NOTIFIER_PROMPTS[notifier]:
        current = values.get(name)
        ask = getpass.getpass if secret else input
        if _is_missing(current):
            value = ask(f"{label} ({name}, Enter to skip): ").strip()
        elif _yes(f"{label} ({name}) is {_shown(current, secret)}. Keep it? [Y/n]: "):
            continue
        else:
            value = ask(f"New {label} ({name}, Enter to keep the current one): ").strip()
        if value and value != current:
            set_env_value(config.ENV_PATH, name, value)
            changed.add(name)
    return changed


def _explain_discord_error(api: DiscordAPI, guild_id: str, err: DiscordError) -> None:
    if err.status == 401:
        print("Discord rejected the bot token (DISCORD_BOT_TOKEN). Copy it again from the Bot page of your app.")
        return
    if err.status in (403, 404):
        print(f"The bot can't see server {guild_id}: it hasn't been added to it, or the server ID is wrong.")
        try:
            print(f"Add it with this link, then run the setup again:\n  {invite_url(api.current_user()['id'])}")
        except DiscordError:
            pass
        return
    print(f"Couldn't reach Discord: {err}")


def pick_discord_channel(changed: set[str] = frozenset()) -> None:
    """Lists the server's text channels and saves the chosen one as
    DISCORD_CHANNEL_ID. A channel already set can be kept, unless the token
    or server just changed."""
    values = dotenv_values(config.ENV_PATH)
    token, guild_id = values.get("DISCORD_BOT_TOKEN"), values.get("DISCORD_GUILD_ID")
    if _is_missing(token) or _is_missing(guild_id):
        print("Skipping the channel choice: DISCORD_BOT_TOKEN and DISCORD_GUILD_ID are needed first.")
        return

    api = DiscordAPI(token)
    print("Connecting to Discord ...")
    try:
        guild = api.guild(guild_id)
        channels = api.text_channels(guild_id)
    except DiscordError as err:
        _explain_discord_error(api, guild_id, err)
        return
    if not channels:
        print(f"Server {guild['name']} has no text channels to send alerts to.")
        return

    current = values.get("DISCORD_CHANNEL_ID")
    by_id = {c["id"]: c for c in channels}
    if current in by_id and not {"DISCORD_BOT_TOKEN", "DISCORD_GUILD_ID"} & changed:
        if _yes(f"Alerts go to #{by_id[current]['name']} in {guild['name']}. Keep it? [Y/n]: "):
            return

    print(f"Text channels in {guild['name']}:")
    for number, channel in enumerate(channels, start=1):
        category = f"  ({channel['category']})" if channel["category"] else ""
        print(f"  {number}. #{channel['name']}{category}")
    while True:
        answer = input(f"Channel for the alerts [1-{len(channels)}, Enter to skip]: ").strip()
        if not answer:
            print("Skipped. Run the setup again to pick a channel.")
            return
        if answer.isdigit() and 1 <= int(answer) <= len(channels):
            break
        print(f"Please type a number from 1 to {len(channels)}.")
    channel = channels[int(answer) - 1]
    set_env_value(config.ENV_PATH, "DISCORD_CHANNEL_ID", channel["id"])
    print(f"Alerts will go to #{channel['name']}.")

    if _yes("Send a test message there? [Y/n]: "):
        try:
            api.send_message(channel["id"], "Spotify New Tracks is set up: new releases will be posted here.")
            print("Sent. Check the channel.")
        except DiscordError as err:
            if err.status == 403:
                print(f"The bot isn't allowed to post in #{channel['name']}: give it the Send Messages permission there.")
            else:
                print(f"The test message failed: {err}")


def maybe_run_login(changed: set[str] = frozenset()) -> None:
    values = dotenv_values(config.ENV_PATH)
    if _is_missing(values.get("SPOTIFY_CLIENT_ID")) or _is_missing(values.get("SPOTIFY_CLIENT_SECRET")):
        print("Skipping the Spotify login: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are needed first.")
        return
    have_token = not _is_missing(values.get("SPOTIFY_REFRESH_TOKEN"))
    if have_token and {"SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET"} & changed:
        # A refresh token only works with the app it was issued to.
        print("The Spotify app credentials changed, so the saved login (SPOTIFY_REFRESH_TOKEN) won't work with them.")
        have_token = False
    if have_token:
        if _yes("Spotify login is already done (SPOTIFY_REFRESH_TOKEN is set). Keep it? [Y/n]: "):
            return
    elif not _yes("Run the Spotify login now? It opens a browser on this machine. [Y/n]: "):
        print("Skipped. Run 'python -m spotify_tracker.login' later (see README for servers without a browser).")
        return
    # Settings typed above aren't in os.environ yet; the login reads them there.
    load_dotenv(config.ENV_PATH, override=True)
    login.main()


def has_systemd() -> bool:
    # The check sd_booted() makes: this directory exists only under systemd.
    return shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()


def bot_service_installed() -> bool:
    # SYSTEMD_UNIT_DIR as in install_bot_service.sh, which the tests point elsewhere.
    unit_dir = Path(os.environ.get("SYSTEMD_UNIT_DIR") or "/etc/systemd/system")
    return (unit_dir / BOT_SERVICE).exists()


def _run_service_script(*args: str) -> bool:
    """Runs install_bot_service.sh, which uses sudo and may ask for a password."""
    print(f"Running {' '.join([SERVICE_SCRIPT.name, *args])} (it may ask for your sudo password) ...", flush=True)
    return subprocess.run(["bash", str(SERVICE_SCRIPT), *args], cwd=config.REPO_DIR).returncode == 0


def maybe_install_bot_service(notifier: str) -> None:
    """Offers to install (or restart) the Discord bot service, or to remove it
    once alerts go to Telegram."""
    installed = bot_service_installed()
    if notifier != "discord":
        question = f"Alerts go to Telegram, but the Discord bot service ({BOT_SERVICE}) is still installed. Remove it? [Y/n]: "
        if installed and _yes(question):
            if not _run_service_script("--uninstall"):
                print(f"Removing it failed; try again with: ./{SERVICE_SCRIPT.name} --uninstall")
        return

    if not has_systemd():
        print("The Discord bot runs as a systemd service, and this machine has no systemd.")
        print("Run it yourself instead: python -m spotify_tracker.discord_bot")
        return
    missing = missing_settings()
    if missing:
        print(f"Not starting the Discord bot service yet: {', '.join(missing)} still missing.")
        return

    if installed:
        question = f"The Discord bot service ({BOT_SERVICE}) is installed. Restart it so it uses these settings? [Y/n]: "
    else:
        question = f"Install the Discord bot as the service {BOT_SERVICE}, running now and at boot? [Y/n]: "
    if not _yes(question):
        print(f"Skipped. Do it later with: ./{SERVICE_SCRIPT.name}")
        return
    if not _run_service_script():
        print(f"The bot service wasn't installed; try again with: ./{SERVICE_SCRIPT.name}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="only create config.env; don't ask for settings or run the login",
    )
    parser.add_argument(
        "--no-venv",
        action="store_true",
        help="use this Python as it is instead of ./venv (its dependencies must be installed)",
    )
    parser.add_argument(
        "--no-bot-service",
        action="store_true",
        help="don't offer to install, restart or remove the Discord bot's systemd service",
    )
    args = parser.parse_args(argv)

    ensure_config_file()
    # Both ends, not just stdin: Windows reports NUL (stdin from /dev/null) as
    # a terminal, and getpass there reads the console directly and would hang.
    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
    if interactive:
        notifier = choose_notifier()
        changed = prompt_for_settings(notifier)
        if notifier == "discord":
            pick_discord_channel(changed)
        maybe_run_login(changed)
        if not args.no_bot_service:
            maybe_install_bot_service(notifier)

    missing = missing_settings()
    if missing:
        print(
            f"Still missing in {config.ENV_PATH}: {', '.join(missing)}. "
            "Fill them in (or re-run 'python -m spotify_tracker.setup') before the first check."
        )
    else:
        print(f"{config.ENV_PATH} is complete.")
    if (
        not interactive
        and not args.no_bot_service
        and _notifier(dotenv_values(config.ENV_PATH)) == "discord"
        and has_systemd()
        and not bot_service_installed()
    ):
        print(f"The Discord bot service isn't installed; install it with: ./{SERVICE_SCRIPT.name}")


if __name__ == "__main__":
    main()
