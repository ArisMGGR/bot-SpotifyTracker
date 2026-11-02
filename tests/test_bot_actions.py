import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spotify_tracker import bot_actions, main
from spotify_tracker.state import utc_today


class _FakeProcess:
    def __init__(self, output: str, returncode: int):
        self._output = output.encode()
        self.returncode = returncode

    async def communicate(self):
        return self._output, None


class BotActionsTest(unittest.TestCase):
    """Every path points into a temporary directory, so the real state/,
    state.json and schedule.env are never touched."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        state_dir = self.root / "state"
        for name, path in (
            ("REPO_DIR", self.root),
            ("STATE_FILE", self.root / "state.json"),
            ("STATE_DIR", state_dir),
            ("LOCK_FILE", state_dir / "check.lock"),
            ("PAUSE_FILE", state_dir / "paused"),
            ("STAMP_FILE", state_dir / "last_scheduled_check"),
            ("SCHEDULE_ENV", self.root / "schedule.env"),
        ):
            patcher = mock.patch.object(bot_actions, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_daily_limit_matches_main(self):
        self.assertEqual(bot_actions.DAILY_CALL_LIMIT, main.DAILY_CALL_LIMIT)

    def test_pause_and_resume_create_and_remove_the_pause_file(self):
        self.assertFalse(bot_actions.is_paused())
        bot_actions.set_paused(True)
        self.assertTrue((self.root / "state" / "paused").exists())
        bot_actions.set_paused(False)
        self.assertFalse(bot_actions.is_paused())
        bot_actions.set_paused(False)  # resuming twice is fine

    def test_summary_keeps_log_messages_without_timestamps_or_progress_bar(self):
        output = (
            "2026-10-24 10:00:00,000 [INFO] Logging this run to logs/2026-10-24.log\n"
            "Checking artists:  50%|#####     | 1/2\rChecking artists: 100%|##########| 2/2\n"
            "2026-10-24 10:00:05,000 [WARNING] Daily Spotify call budget reached\n"
            "2026-10-24 10:00:06,000 [INFO] Run complete: checked all 2 artists\n"
        )
        self.assertEqual(
            bot_actions.summarize_output(output),
            "Logging this run to logs/2026-10-24.log\n"
            "WARNING: Daily Spotify call budget reached\n"
            "Run complete: checked all 2 artists",
        )

    def test_summary_is_cut_to_fit_a_discord_message(self):
        output = "".join(f"2026-10-24 10:00:00,000 [INFO] {'x' * 300}\n" for _ in range(20))
        summary = bot_actions.summarize_output(output)
        self.assertLessEqual(len(summary), bot_actions.REPLY_LOG_CHARS + 3)
        self.assertTrue(summary.startswith("..."))

    def test_run_check_runs_main_and_reports_its_log(self):
        process = _FakeProcess("2026-10-24 10:00:06,000 [INFO] Run complete: checked all 2 artists\n", 0)
        with mock.patch.object(bot_actions.asyncio, "create_subprocess_exec", return_value=process) as start:
            reply = asyncio.run(bot_actions.run_check())
        self.assertEqual(start.call_args.args[1:], ("-m", "spotify_tracker.main"))
        self.assertTrue(reply.startswith("Check finished."))
        self.assertIn("Run complete: checked all 2 artists", reply)

    def test_run_check_reads_config_env_as_it_is_now(self):
        # The bot loaded DISCORD_CHANNEL_ID=111 at start; /channel since
        # wrote 222. The check must not inherit the stale value.
        config_env = self.root / "config.env"
        config_env.write_text("DISCORD_CHANNEL_ID=222\n", encoding="utf-8")
        process = _FakeProcess("", 0)
        with (
            mock.patch.object(bot_actions, "env_path", return_value=config_env),
            mock.patch.dict(os.environ, {"DISCORD_CHANNEL_ID": "111", "KEPT": "1"}),
            mock.patch.object(bot_actions.asyncio, "create_subprocess_exec", return_value=process) as start,
        ):
            asyncio.run(bot_actions.run_check())
        env = start.call_args.kwargs["env"]
        self.assertNotIn("DISCORD_CHANNEL_ID", env)
        self.assertEqual(env["KEPT"], "1")

    def test_run_check_reports_a_failed_check(self):
        process = _FakeProcess("", 1)
        with mock.patch.object(bot_actions.asyncio, "create_subprocess_exec", return_value=process):
            reply = asyncio.run(bot_actions.run_check())
        self.assertEqual(reply, "Check failed (exit code 1).")

    @unittest.skipIf(os.name == "nt", "flock is POSIX only")
    def test_run_check_refuses_while_another_check_holds_the_lock(self):
        held = bot_actions.try_lock()
        self.addCleanup(held.close)
        with mock.patch.object(bot_actions.asyncio, "create_subprocess_exec") as start:
            reply = asyncio.run(bot_actions.run_check())
        self.assertIn("already running", reply)
        start.assert_not_called()

    def _write_state(self, **data):
        base = {"artists": {}, "followed": None, "rate_limited_until": 0, "api_usage": None}
        (self.root / "state.json").write_text(json.dumps(dict(base, **data)), encoding="utf-8")

    def test_status_reports_checks_calls_and_the_next_scheduled_run(self):
        now = 1_800_000_000
        self._write_state(
            artists={"a": {"name": "A", "releases": [], "checked_at": now - 60}},
            followed={"fetched_at": now, "artists": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]},
            api_usage={"date": utc_today(), "count": 7},
        )
        (self.root / "state").mkdir()
        (self.root / "state" / "last_scheduled_check").write_text(str(now - 3600), encoding="utf-8")
        (self.root / "schedule.env").write_text("PATH=/usr/bin\nCHECK_INTERVAL_HOURS=12\n", encoding="utf-8")

        text = bot_actions.status_text("<#99>", now=now)

        self.assertIn("Scheduled checks: running", text)
        self.assertIn(f"Next scheduled check: <t:{now - 3600 + 12 * 3600}:R>", text)
        self.assertIn(f"Last artist checked: <t:{now - 60}:R>", text)
        self.assertIn("Followed artists: 2", text)
        self.assertIn("Spotify API calls today: 7/100", text)
        self.assertIn("Alert channel: <#99>", text)

    def test_status_reads_the_interval_under_its_old_name(self):
        now = 1_800_000_000
        self._write_state()
        (self.root / "state").mkdir()
        (self.root / "state" / "last_scheduled_check").write_text(str(now - 3600), encoding="utf-8")
        (self.root / "schedule.env").write_text("PATH=/usr/bin\nCRON_INTERVAL_HOURS=6\n", encoding="utf-8")

        text = bot_actions.status_text("<#99>", now=now)

        self.assertIn(f"Next scheduled check: <t:{now - 3600 + 6 * 3600}:R>", text)

    def test_status_shows_a_pause_and_a_rate_limit(self):
        now = 1_800_000_000
        self._write_state(rate_limited_until=now + 600)
        bot_actions.set_paused(True)
        text = bot_actions.status_text("<#99>", now=now)
        self.assertIn("**paused**", text)
        self.assertIn(f"checks paused until <t:{now + 600}:R>", text)
        self.assertIn("Last artist checked: never", text)
        self.assertIn("Followed artists: not fetched yet", text)


if __name__ == "__main__":
    unittest.main()
