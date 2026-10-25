# Spotify New Tracks Notifier

Watches the artists you follow on Spotify and sends you a Telegram or
Discord message whenever one of them releases a new album or single. With
Discord, a bot also answers commands (`/check`, `/status`, ...) from you.

## Setup

1. **Install dependencies** (Python 3.10+): nothing to do here if you use
   step 5, which creates a virtualenv in `./venv` and installs them into it.
   To install them yourself instead:
   ```bash
   pip install -r requirements.txt
   ```

2. **Create a Spotify app** at https://developer.spotify.com/dashboard:
   - Click **Create app**, fill in any name/description.
   - Under **Redirect URIs**, add exactly: `http://127.0.0.1:8888/callback`
     (if port 8888 is taken, register another, e.g.
     `http://127.0.0.1:9090/callback`, and set the same value as
     `SPOTIFY_REDIRECT_URI` in `config.env`; the login step listens on its port)
   - Check **Web API** and save.
   - On the app's **Settings** page, copy the **Client ID** and (after
     clicking "View client secret") the **Client secret**.

3. **Create a Telegram bot** (to get alerts on Telegram; for Discord see
   [Discord](#discord) and skip step 4): message
   [@BotFather](https://t.me/BotFather), run `/newbot`, and copy the bot
   token it gives you.

4. **Get your chat ID**: message your new bot once, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and
   read the `chat.id` field from the response.

5. **Configure secrets and log in** (one-time):
   ```bash
   python -m spotify_tracker.setup
   ```
   Run with any Python 3.10+, this first creates `./venv` (if missing),
   installs `requirements.txt` into it and carries on inside it. It then
   creates `config.env` from `config.env.example` and asks whether alerts go
   to Telegram or Discord (Enter keeps the current choice, Telegram at
   first). It asks for `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET`, then
   for the chosen service's settings (`TELEGRAM_BOT_TOKEN` and
   `TELEGRAM_CHAT_ID`, or the Discord ones described in
   [Discord](#discord)); press Enter to skip one. Then it offers to run the
   Spotify login, and for Discord, to install the bot's systemd service
   (see [Start the bot service](#3-start-the-bot-service)).
   `./install.sh` runs this step for you.
   An existing `.env` from before the rename is renamed to `config.env`.

   Re-running it is safe: for each setting already in `config.env` it shows
   the current value (secrets only by their last 4 characters) and asks
   whether to keep it, with Enter keeping it; answer `n` to type a new one.
   It also asks whether to keep the existing Spotify login, and offers a new
   login on its own if you change the client ID or secret, since the old
   login only works with the old Spotify app.

   Afterwards run the other commands with the venv's Python
   (`venv/bin/python`, or `venv\Scripts\python` on Windows), or activate the
   venv first. Pass `--no-venv` to skip the virtualenv and use the Python you
   ran it with (its dependencies must already be installed). On
   Debian/Ubuntu, creating the venv needs `sudo apt install python3-venv`.

   To do it by hand instead, copy `config.env.example` to `config.env`, fill
   in the Spotify client ID and secret and the settings of the service set in
   `NOTIFIER`, and continue with step 6.

6. **Authorize the app to read your followed artists** (one-time, done by
   step 5 if you let it):
   ```bash
   python -m spotify_tracker.login
   ```
   This opens your browser to log in and approve access, then saves
   `SPOTIFY_REFRESH_TOKEN` into `config.env` automatically. Spotify sends the
   browser back to `127.0.0.1:8888` on the machine running the command, so
   on a server without a browser either run this step on your own computer
   and copy the `SPOTIFY_REFRESH_TOKEN=` line into the server's `config.env`, or
   forward the port while it runs (`ssh -L 8888:127.0.0.1:8888 <server>`)
   and open the printed link locally. The login gives up after 5 minutes if
   no redirect arrives.

## Usage

Run a single check (good for cron):
```bash
python -m spotify_tracker.main
```

Or keep it running and poll every N seconds (e.g. every hour):
```bash
python -m spotify_tracker.main --interval 3600
```

Every run also saves a copy of its console output to a dated file under
`logs/` in the repo (e.g. `logs/2026-08-06.log`) — one file per day, so with
cron or `--interval` every check on the same day is appended to that day's
log. `state.json` and `logs/` default to the repo root whatever directory
you run from. Change the log location with `--log-dir`:
```bash
python -m spotify_tracker.main --log-dir /var/log/spotify-tracker
```

### How it works

- The program fetches every artist your Spotify account follows
  (`GET /me/following`), so there's no name list to maintain —
  following/unfollowing an artist in Spotify is how you manage what's
  tracked. The list is cached in `state.json` and refreshed at most once
  every 24 hours, so a newly followed artist can take up to a day to be
  picked up.
- Each check of an artist fetches their **10 newest albums and 10 newest
  singles**: one API request each, so every artist costs 2 calls no matter
  how big their catalog is. Albums and singles are requested separately
  because a combined request lists every album before any single, which can
  push a new single many pages deep.
- The first time an artist is checked, those newest releases are recorded as
  a baseline (no notification), so you don't get alerts for music that was
  out before you started tracking them. If a rate limit interrupts the
  check, nothing is recorded and the next run retries that artist.
- Every API request is spaced at least 6 seconds from the previous one
  (at most 5 requests in any rolling 30-second window, the window Spotify
  rate-limits on), regardless of how requests cluster.
- On every later check the newest releases are compared against
  `state.json`, which remembers release IDs already seen.
- Each run reports how many Spotify API requests it made (`Spotify API
  requests this run: N`), printed at the end of the run — and also when a
  rate limit stops the run early or it crashes — so you can see how close a
  run came to the limit. The count includes token refreshes and appears in
  both the console and that day's `logs/` file.
- Each run ends with one line for every artist it checked, in the form
  `artist - new release - last check before this one`, for example
  `Bastille - ODYSSEY - 2026-10-23 09:12`. The middle part is empty when
  there was no new release (`Eminem -  - 2026-10-23 09:15`), and the last
  is `never` for an artist checked for the first time. Several new releases
  are separated by commas. The list is printed even when a rate limit or an
  outage stops the run early.
- Spotify's daily quota for this app is about 105 requests, so the program
  keeps a running total of the calls it makes across every run of the
  calendar day (persisted in `state.json`, reset at UTC midnight) and stops
  at **100** — leaving headroom before Spotify itself starts blocking. Once
  the day's budget is spent, later runs that day exit without touching the
  API and print `Spotify API calls used today: N/100`.
- When fewer calls are left in the day than one more artist needs (2, plus
  1 spare for a token refresh), the run stops checking instead of starting a
  check that would hit the limit halfway. The unchecked artists stay "most
  overdue" and go first once the budget resets. Each artist's actual cost is
  saved as `last_cost` in `state.json`.
- Only unseen releases dated within the last 30 days trigger an alert. Older unseen IDs (Spotify catalog noise: reissues, regional
  editions, relinked duplicates) are absorbed into the state silently. If
  one check finds the same new release twice under different IDs (same name
  and date), you get one alert.
- When Spotify imposes a rate-limit penalty (any 429), the run stops
  immediately — never retrying in-process — and the deadline is saved to
  `state.json`. Every run before that deadline exits without touching the
  API at all. Because `Retry-After` can under-report (or be missing) while
  the block is still active, the recorded penalty is never shorter than
  30 minutes, plus a 2-minute safety margin — requests made during an
  active penalty extend it, so waiting it out fully is the only way to
  recover.
- If Spotify can't be reached at all, or an access token can't be fetched
  (for example a revoked refresh token — the log shows Spotify's reason),
  the run stops with an error instead of spending a call on every
  remaining artist. Artists already checked are kept.
- Releases are **not** filtered by country. With the Greek (`GR`) filter
  the tracker used to apply, Spotify's API left out some releases that
  Spotify's own artist page lists, such as Bastille's collaborations with
  Misfits Together, so they were never alerted on. `SPOTIFY_MARKET` in
  `config.env` is now ignored.

### Features on other artists' releases ("Appears On")

By default only an artist's own albums and singles are checked. To also get
alerts when an artist features on someone else's record, list them in `config.env`
(names as Spotify shows them, or Spotify artist IDs, comma-separated):
```
APPEARS_ON_ARTISTS=Bastille, Eminem
```
Then you get messages like "**Bastille** appears on: **ODYSSEY** by
ILLENIUM". Compilations ("Pop Hits 2015" and the like) are skipped.

This costs extra API calls, so pick only the artists you care about most.
Spotify doesn't list "Appears On" releases newest first, so the **whole**
list has to be read on every check: one call per 10 releases. Bastille, for
example, appears on about 460 releases, so checking them takes about 47
extra calls, roughly half a day's budget. Each listed artist's cost is
measured and budgeted for, and the check is deferred to a later run if it
won't fit in what's left of the day. An artist whose list would take more
than 50 calls only gets its first page checked (a warning is logged). A
name that doesn't match anyone you follow is reported in the log. Because
the list is split on commas, an artist whose name contains one (e.g.
"Tyler, The Creator") must be listed by Spotify artist ID — the ID is the
last part of the artist's `open.spotify.com/artist/<ID>` link.

### Running on a schedule

#### Auto-install (recommended): a check every ~25 hours

From the repo root:
```bash
./install.sh
```
This does the full setup in one command:

1. Creates a virtualenv in `./venv` and installs `requirements.txt` into it.
2. Runs `python -m spotify_tracker.setup` (setup step 5): creates
   `config.env` (or renames an old `.env`), asks for any missing settings,
   offers to run the Spotify login and, for Discord, offers to install the
   bot service. Run from a non-interactive shell, it only creates the file
   and lists what's still missing.
3. Schedules the check on a **~25-hour** cadence. On a Linux machine with
   systemd it installs the systemd timer `spotify-tracker-check.timer`
   (asking for your sudo password to write it to `/etc/systemd/system`);
   anywhere else (macOS, a container without systemd) it installs a crontab
   entry instead.

If the login can't finish here (a server without a browser), the schedule is
still installed; finish the login as described in setup step 6.

To remove the schedule later (whichever kind it is):
```bash
./install.sh --uninstall
```
This leaves the Discord bot service alone; remove that with
`./install_bot_service.sh --uninstall`.

Other flags: `--cron` (a crontab entry even where systemd is available),
`--systemd` (the timer, failing if there's no systemd), `--no-venv` (skip
venv/pip), `--no-setup` (skip the `config.env` setup and login),
`--no-bot-service` (the setup doesn't ask about the Discord bot service),
`--help`. Installing one kind of schedule removes the other, so only one
ever runs.

**Why 25 hours needs a wrapper.** Cron can't express "every 25 hours" — its
fields are fixed clock times, so any attempt drifts. So the timer (or
crontab entry) actually ticks **hourly** and calls `scripts/run_check.sh`,
which performs a real check only once ~25 hours have elapsed since the last
one (tracked in `state/last_scheduled_check`, with an `flock` so runs never
overlap). To change the interval, pass `CHECK_INTERVAL_HOURS` to the
installer, e.g. `CHECK_INTERVAL_HOURS=12 ./install.sh` (a positive whole
number of hours; anything else makes the wrapper exit with an error in
`logs/schedule.log`). Re-running the installer without it goes back to 25
hours. The wrapper appends its output to `logs/schedule.log` (recreating
`logs/` if it was deleted); each run is also appended to that day's
`logs/<YYYY-MM-DD>.log` as usual.

The wrapper needs `flock` (part of util-linux on Linux; `brew install flock`
on macOS). Without it every scheduled run fails with an error in
`logs/schedule.log`, and the installer warns about it up front. Scheduled
jobs run with a minimal `PATH`, so the installer saves the `PATH` of the
shell you ran `./install.sh` from (and any `CHECK_INTERVAL_HOURS`) to
`schedule.env` in the repo root, and the wrapper uses it; if you install
`flock` (or move Python) later, re-run the installer so `schedule.env` picks
it up.

The scheduled job uses `./venv/bin/python` if present, otherwise the system
`python3`.

#### Managing the systemd timer

The timer `spotify-tracker-check.timer` starts
`spotify-tracker-check.service` every hour, which runs
`scripts/run_check.sh` as you, from the repo folder. A tick missed while the
machine was off runs at the next boot. To install or update only the timer,
run `./install_check_timer.sh` (`--uninstall` removes it, `--print` shows the
unit files). To see when it next fires:
```bash
systemctl list-timers spotify-tracker-check.timer
```
To run a tick now (it still skips the check if it isn't due; use the bot's
`/check`, or `venv/bin/python -m spotify_tracker.main`, to force one):
```bash
sudo systemctl start spotify-tracker-check.service
```

#### Managing it yourself

If you'd rather schedule it yourself, run a one-off check each time, e.g.
every 30 minutes from cron:
```
*/30 * * * * cd /path/to/bot-SpotifyNewTracks && venv/bin/python -m spotify_tracker.main >> tracker.log 2>&1
```

Or just leave it running in the foreground/background with `--interval`.
Neither of these honours the Discord bot's `/pause`, which only stops
`scripts/run_check.sh`.

### Reinstalling or moving to another machine

Keep these from the old copy and put them in the new repo root:

- `config.env`: your settings and the Spotify login.
- `state.json`: the releases already seen, the cached artist list, the
  day's API call count and any rate-limit wait. Without it every artist gets
  a new baseline and today's call budget starts again from 0.
- Optionally `logs/`, `state/last_scheduled_check` (when the last scheduled
  check ran) and `state/paused`.

Copies from an older version, which called these `state/last_cron_run` and
`logs/cron.log`, are renamed when the installer or the next scheduled run
finds them. `venv/` and `schedule.env` (`cron.env` in older versions) are
rebuilt by `./install.sh`, so don't copy them. If the new copy is in a
different folder, run the old copy's uninstall first (`./install.sh
--uninstall`, or `./install_cron.sh --uninstall` in an older version, and
`./install_bot_service.sh --uninstall`); then run `./install.sh` in the new
one so the schedule and the bot point at it.

## Discord

Alerts can go to a channel in your Discord server instead of Telegram. The
scheduled checks still run from the systemd timer (or cron); a separate
bot, running as a systemd service, answers commands from one Discord user (you).

### 1. Create the bot

1. At https://discord.com/developers/applications click **New Application**.
2. On the **Bot** page click **Reset Token** and copy the token. No
   privileged intents are needed.
3. In Discord, turn on **Developer Mode** (User Settings → Advanced), then
   right-click your server → **Copy Server ID**, and your own name →
   **Copy User ID**.

### 2. Run the setup

```bash
python -m spotify_tracker.setup
```
Answer `discord` to "Send alerts with Telegram or Discord?", then give the
bot token, the server ID and your user ID. The setup connects with the
token, lists the server's text channels and asks which one the alerts go
to, then offers to post a test message there. If the bot isn't in the
server yet, it prints an invite link (with the bot's permissions and slash
commands); open it, add the bot, and run the setup again.

The setup saves these to `config.env` (to edit it by hand instead):

| Setting | Value |
|---|---|
| `NOTIFIER` | `discord` |
| `DISCORD_BOT_TOKEN` | The bot token. |
| `DISCORD_GUILD_ID` | The server ID. |
| `DISCORD_USER_ID` | Your user ID: the only user whose commands the bot answers. |
| `DISCORD_CHANNEL_ID` | The alert channel's ID (right-click it → **Copy Channel ID**). |

Re-running the setup offers to keep the current channel, unless you changed
the token or the server.

### 3. Start the bot service

At the end, the setup asks "Install the Discord bot as the service
spotify-tracker-bot.service, running now and at boot? [Y/n]". Press Enter,
then give your sudo password. If the service is already installed, it asks
whether to restart it instead, so the bot picks up the settings you just
changed. If settings are still missing (the Spotify login, say), it doesn't
ask yet; run the setup again once they're in. If you switch back to
Telegram, it offers to remove the service.

To install or update just the service yourself:
```bash
./install_bot_service.sh
```
It runs `python -m spotify_tracker.discord_bot` as you, from this folder,
and starts at boot. Manage it like any service:
```bash
sudo systemctl restart spotify-tracker-bot.service
```
```bash
sudo systemctl status spotify-tracker-bot.service
```
```bash
journalctl -u spotify-tracker-bot.service -f
```
The bot reads the token, server and user ID when it starts, so restart it
after changing them. If `config.env` is incomplete the bot exits and systemd
doesn't keep restarting it; fix the settings, then restart it. Remove it with
`./install_bot_service.sh --uninstall`, and print the unit file without
installing it with `./install_bot_service.sh --print`. On a machine without
systemd, run `python -m spotify_tracker.discord_bot` yourself instead.

### Commands

The commands appear in your server only and answer only the user in
`DISCORD_USER_ID`; anyone else gets a refusal. Replies are visible only to
you.

| Command | What it does |
|---|---|
| `/check` | Runs a check now and replies with its last log lines. Refused while another check (the scheduled one) is running. |
| `/status` | Next scheduled check, last artist checked, followed artists, Spotify API calls used today, any rate-limit pause, and the alert channel. |
| `/channel` | Changes the channel the alerts go to (saved to `config.env`). The bot needs View Channel and Send Messages there. |
| `/pause` | Stops the scheduled checks (creates `state/paused`). `/check` still works. |
| `/resume` | Restarts them. |

## Development

The unit tests use the standard library's `unittest` plus the packages in
`requirements.txt`, and never call Spotify, Telegram or Discord (the Discord
bot's tests run only when discord.py is installed). Run them from the repo
root:
```bash
python -m unittest
```

## License

MIT — see [LICENSE](LICENSE).
