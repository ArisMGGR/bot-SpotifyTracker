import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent


def _bash_works() -> bool:
    if not shutil.which("bash"):
        return False
    try:
        return subprocess.run(["bash", "-c", "true"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@unittest.skipUnless(_bash_works(), "needs bash")
class ShellScriptTest(unittest.TestCase):
    """Each script runs from a copy in a temporary directory, so nothing here
    touches the real crontab, state/ or the tracker itself."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "scripts").mkdir()
        shutil.copy(REPO_DIR / "scripts" / "run_check.sh", self.root / "scripts")
        shutil.copy(REPO_DIR / "install.sh", self.root)
        shutil.copy(REPO_DIR / "install_bot_service.sh", self.root)
        shutil.copy(REPO_DIR / "install_check_timer.sh", self.root)

    def _run(self, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", *args],
            cwd=self.root,
            env=dict(os.environ, **(env or {})),
            # Not a terminal, so the installer's setup step never prompts.
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def _schedule_log(self) -> str:
        return (self.root / "logs" / "schedule.log").read_text(encoding="utf-8")

    def _install(
        self, env: dict[str, str] | None = None, *flags: str, crontab: str | None = None
    ) -> subprocess.CompletedProcess:
        """Run install.sh --no-venv with stub flock and crontab in a
        directory of their own (like Homebrew's: on the installer's PATH but
        not in cron's /usr/bin:/bin), and a stub venv python. The stubs record
        that they ran; crontab -l lists the given table (none if None) and
        crontab writes the new table to stubs/crontab.out. EXTRA_PATH, if
        given in env, goes on the installer's PATH too."""
        stubs = self.root / "stubs"
        stubs.mkdir()
        (stubs / "flock").write_text('#!/bin/sh\ntouch "$(dirname "$0")/flock-ran"\n', newline="\n")
        if crontab is not None:
            (stubs / "crontab.in").write_text(crontab, newline="\n")
        (stubs / "crontab").write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "-l" ]; then cat "$(dirname "$0")/crontab.in" 2>/dev/null; exit $?; fi\n'
            'cat > "$(dirname "$0")/crontab.out"\n',
            newline="\n",
        )
        (self.root / "venv" / "bin").mkdir(parents=True)
        (self.root / "venv" / "bin" / "python").write_text('#!/bin/sh\necho PYTHON_RAN "$@"\n', newline="\n")
        for stub in (stubs / "flock", stubs / "crontab", self.root / "venv" / "bin" / "python"):
            stub.chmod(0o755)

        return self._run(
            "-c",
            'PATH="$PWD/stubs:${EXTRA_PATH:+$EXTRA_PATH:}$PATH" bash install.sh --no-venv "$@"',
            "install",
            *flags,
            # No systemd unless a test fakes it, so the default schedule is cron.
            env=dict(
                {
                    "CHECK_INTERVAL_HOURS": "",
                    "CRON_INTERVAL_HOURS": "",
                    "EXTRA_PATH": "",
                    "SYSTEMD_RUN_DIR": str(self.root / "no-systemd"),
                },
                **(env or {}),
            ),
        )

    def _run_entry(self) -> subprocess.CompletedProcess:
        """Run the installed crontab command the way cron would: % unescaped,
        through sh, with only /usr/bin:/bin."""
        script = (
            "cmd=$(cut -d' ' -f6- stubs/crontab.out | sed 's/\\\\%/%/g')\n"
            'env -i PATH=/usr/bin:/bin sh -c "$cmd"\n'
        )
        return self._run("-c", script)

    def test_run_check_rejects_a_bad_interval(self):
        for value in ("010", "0", "-1", "abc", "1.5"):
            with self.subTest(CHECK_INTERVAL_HOURS=value):
                result = self._run("scripts/run_check.sh", env={"CHECK_INTERVAL_HOURS": value})
                self.assertEqual(result.returncode, 1)
                schedule_log = self._schedule_log()
                self.assertIn("CHECK_INTERVAL_HOURS must be a positive whole number", schedule_log)
                self.assertIn(f"(got '{value}')", schedule_log)
                self.assertFalse((self.root / "state" / "last_scheduled_check").exists())

    def test_installed_entry_finds_flock_under_crons_minimal_path(self):
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self._run_entry()

        self.assertEqual(result.returncode, 0, result.stderr)
        schedule_log = self._schedule_log()
        self.assertNotIn("flock not found", schedule_log)
        self.assertIn("PYTHON_RAN", schedule_log)
        self.assertTrue((self.root / "stubs" / "flock-ran").exists())

    def test_install_runs_the_setup_step_unless_skipped(self):
        # The stub venv python stands in for spotify_tracker.setup. _install
        # passes --no-venv, which the setup must get too, or it would build
        # ./venv itself.
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PYTHON_RAN -m spotify_tracker.setup --no-venv", result.stdout)

    def test_install_with_no_setup_skips_it_and_warns(self):
        result = self._install(None, "--no-setup")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PYTHON_RAN", result.stdout)
        self.assertIn("config.env not found", result.stdout)

    def test_installed_entry_stays_short_with_a_long_path(self):
        # Cron silently cuts commands longer than ~1000 characters, so the
        # installer's PATH must not end up in the entry.
        long_path = ":".join(f"/opt/tool{i}/bin" for i in range(150))
        result = self._install(env={"EXTRA_PATH": long_path})
        self.assertEqual(result.returncode, 0, result.stderr)

        entry = (self.root / "stubs" / "crontab.out").read_text(encoding="utf-8").strip()
        self.assertLess(len(entry), 998, entry)
        self.assertNotIn(long_path, entry)
        self.assertIn(long_path, (self.root / "schedule.env").read_text(encoding="utf-8"))

    def test_scheduled_run_recreates_a_deleted_logs_dir(self):
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        shutil.rmtree(self.root / "logs")

        result = self._run_entry()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PYTHON_RAN", self._schedule_log())

    def test_interval_given_to_the_installer_reaches_the_scheduled_run(self):
        result = self._install(env={"CHECK_INTERVAL_HOURS": "12"})
        self.assertEqual(result.returncode, 0, result.stderr)
        # The last real run was 13 hours ago: due at 12 hours, not at the
        # default 25.
        (self.root / "state").mkdir()
        (self.root / "state" / "last_scheduled_check").write_text(str(int(time.time()) - 13 * 3600))

        result = self._run_entry()

        self.assertEqual(result.returncode, 0, result.stderr)
        schedule_log = self._schedule_log()
        self.assertIn(">= 12h since last run", schedule_log)
        self.assertIn("PYTHON_RAN", schedule_log)

    def test_run_check_skips_while_paused(self):
        result = self._install()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / "state").mkdir(exist_ok=True)
        (self.root / "state" / "paused").touch()

        result = self._run_entry()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("PYTHON_RAN", self._schedule_log())
        self.assertFalse((self.root / "state" / "last_scheduled_check").exists())

    def _service_stubs(self) -> dict[str, str]:
        """Stub sudo (runs its command) and systemctl (logs its arguments),
        and a unit directory inside the temporary directory."""
        stubs = self.root / "service-stubs"
        stubs.mkdir()
        (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n', newline="\n")
        (stubs / "systemctl").write_text(
            '#!/bin/sh\necho "$@" >> "$(dirname "$0")/systemctl.log"\n', newline="\n"
        )
        for stub in (stubs / "sudo", stubs / "systemctl"):
            stub.chmod(0o755)
        (self.root / "units").mkdir()
        return {"SYSTEMD_UNIT_DIR": str(self.root / "units"), "EXTRA_PATH": str(stubs)}

    def test_bot_service_unit_runs_the_bot_from_the_repo(self):
        result = self._run("install_bot_service.sh", "--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        # By name only: Git Bash on Windows prints the path as /tmp/...
        self.assertRegex(result.stdout, rf"WorkingDirectory=\S*/{re.escape(self.root.name)}\n")
        self.assertIn("-m spotify_tracker.discord_bot", result.stdout)
        self.assertIn("Restart=on-failure", result.stdout)
        self.assertIn("RestartPreventExitStatus=2", result.stdout)

    def test_bot_service_install_writes_the_unit_and_restarts_it(self):
        env = self._service_stubs()
        result = self._run("-c", 'PATH="$EXTRA_PATH:$PATH" bash install_bot_service.sh', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "units" / "spotify-tracker-bot.service").exists())
        log = (self.root / "service-stubs" / "systemctl.log").read_text(encoding="utf-8")
        self.assertIn("daemon-reload", log)
        self.assertIn("enable spotify-tracker-bot.service", log)
        self.assertIn("restart spotify-tracker-bot.service", log)

    def test_install_leaves_the_bot_service_to_the_setup(self):
        # The setup asks about the service itself; the installer only passes
        # --no-bot-service on, and never installs it on its own.
        (self.root / "config.env").write_text("NOTIFIER=discord\n", encoding="utf-8")
        env = self._service_stubs()
        # _install puts EXTRA_PATH (the service stubs) on the installer's PATH.
        result = self._install(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "units" / "spotify-tracker-bot.service").exists())

    def test_install_passes_no_bot_service_to_the_setup(self):
        result = self._install(None, "--no-bot-service")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PYTHON_RAN -m spotify_tracker.setup --no-venv --no-bot-service", result.stdout)

    CRON_ENTRY = "0 * * * * /usr/bin/env bash '/x/scripts/run_check.sh' # spotify-tracker-check (managed by install.sh)\n"
    OLD_CRON_ENTRY = "0 * * * * /usr/bin/env bash '/old/scripts/run_check.sh' # spotify-tracker-cron (managed by install_cron.sh)\n"
    OTHER_JOB = "0 5 * * * /usr/local/bin/backup\n"

    def test_install_picks_the_timer_where_systemd_runs(self):
        env = self._service_stubs()
        (self.root / "run-systemd").mkdir()
        env["SYSTEMD_RUN_DIR"] = str(self.root / "run-systemd")
        result = self._install(env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "units" / "spotify-tracker-check.timer").exists())
        self.assertFalse((self.root / "stubs" / "crontab.out").exists())

    def test_install_with_cron_overrides_systemd(self):
        env = self._service_stubs()
        (self.root / "run-systemd").mkdir()
        env["SYSTEMD_RUN_DIR"] = str(self.root / "run-systemd")
        result = self._install(env, "--cron")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "units" / "spotify-tracker-check.timer").exists())
        self.assertIn("spotify-tracker-check", (self.root / "stubs" / "crontab.out").read_text(encoding="utf-8"))

    def test_install_replaces_an_entry_from_install_cron(self):
        result = self._install(None, crontab=self.OTHER_JOB + self.OLD_CRON_ENTRY)
        self.assertEqual(result.returncode, 0, result.stderr)
        table = (self.root / "stubs" / "crontab.out").read_text(encoding="utf-8")
        self.assertNotIn("spotify-tracker-cron", table)
        self.assertIn(self.OTHER_JOB, table)
        self.assertEqual(table.count("spotify-tracker-check"), 1)

    def test_install_renames_files_from_before_the_rename(self):
        (self.root / "logs").mkdir()
        (self.root / "logs" / "cron.log").write_text("old log\n", encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / "last_cron_run").write_text("123", encoding="utf-8")
        (self.root / "state" / "cron.lock").touch()
        (self.root / "cron.env").write_text("PATH=/old\n", encoding="utf-8")

        result = self._install()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._schedule_log(), "old log\n")
        self.assertEqual((self.root / "state" / "last_scheduled_check").read_text(encoding="utf-8"), "123")
        for old in ("logs/cron.log", "state/last_cron_run", "state/cron.lock", "cron.env"):
            self.assertFalse((self.root / old).exists(), old)
        # Rewritten with the installer's PATH.
        self.assertNotIn("PATH=/old", (self.root / "schedule.env").read_text(encoding="utf-8"))

    def test_scheduled_run_reads_files_from_before_the_rename(self):
        # Copied over after installing: the old interval name and stamp still count.
        (self.root / "cron.env").write_text("PATH=/usr/bin:/bin\nCRON_INTERVAL_HOURS=12\n", encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / "last_cron_run").write_text(str(int(time.time()) - 3600), encoding="utf-8")
        (self.root / "venv" / "bin").mkdir(parents=True)
        (self.root / "venv" / "bin" / "python").write_text('#!/bin/sh\necho PYTHON_RAN "$@"\n', newline="\n")
        (self.root / "venv" / "bin" / "python").chmod(0o755)

        result = self._run("scripts/run_check.sh", env={"CHECK_INTERVAL_HOURS": "", "CRON_INTERVAL_HOURS": ""})

        self.assertEqual(result.returncode, 0, result.stderr)
        # Not due: 1 hour since the last run, of 12.
        self.assertNotIn("running check", self._schedule_log())
        self.assertTrue((self.root / "schedule.env").exists())
        self.assertTrue((self.root / "state" / "last_scheduled_check").exists())
        self.assertFalse((self.root / "state" / "last_cron_run").exists())

    def test_check_timer_units_run_the_wrapper_hourly(self):
        result = self._run("install_check_timer.sh", "--print")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Type=oneshot", result.stdout)
        self.assertRegex(result.stdout, r'ExecStart=/usr/bin/env bash "\S*/scripts/run_check.sh"\n')
        self.assertIn("OnCalendar=hourly", result.stdout)
        self.assertIn("Persistent=true", result.stdout)
        self.assertIn("WantedBy=timers.target", result.stdout)

    def test_install_systemd_installs_the_timer_and_removes_the_cron_entry(self):
        env = self._service_stubs()
        result = self._install(env, "--systemd", crontab=self.OTHER_JOB + self.CRON_ENTRY)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertIn("PYTHON_RAN -m spotify_tracker.setup", result.stdout)
        self.assertTrue((self.root / "units" / "spotify-tracker-check.service").exists())
        self.assertTrue((self.root / "units" / "spotify-tracker-check.timer").exists())
        log = (self.root / "service-stubs" / "systemctl.log").read_text(encoding="utf-8")
        self.assertIn("enable spotify-tracker-check.timer", log)
        self.assertIn("restart spotify-tracker-check.timer", log)
        # The other job stays; ours is gone.
        self.assertEqual((self.root / "stubs" / "crontab.out").read_text(encoding="utf-8"), self.OTHER_JOB)

    def test_install_systemd_leaves_a_crontab_without_our_entry_alone(self):
        result = self._install(self._service_stubs(), "--systemd", crontab=self.OTHER_JOB)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "stubs" / "crontab.out").exists())

    def test_installed_timer_service_runs_the_check(self):
        result = self._install(self._service_stubs(), "--systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = (self.root / "units" / "spotify-tracker-check.service").read_text(encoding="utf-8")
        command = re.search(r"^ExecStart=(.*)$", unit, re.MULTILINE).group(1)

        # Like cron's, systemd's PATH lacks the stubs; schedule.env supplies them.
        result = self._run("-c", 'env -i PATH=/usr/bin:/bin sh -c "$1"', "run", command)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PYTHON_RAN -m spotify_tracker.main", self._schedule_log())
        self.assertTrue((self.root / "stubs" / "flock-ran").exists())

    def test_install_with_cron_removes_the_timer(self):
        env = self._service_stubs()
        (self.root / "units" / "spotify-tracker-check.timer").write_text("[Timer]\n", encoding="utf-8")
        result = self._install(env)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse((self.root / "units" / "spotify-tracker-check.timer").exists())
        self.assertIn("spotify-tracker-check", (self.root / "stubs" / "crontab.out").read_text(encoding="utf-8"))
        log = (self.root / "service-stubs" / "systemctl.log").read_text(encoding="utf-8")
        self.assertIn("disable --now spotify-tracker-check.timer", log)

    def test_uninstall_removes_the_cron_entry_and_the_timer(self):
        env = self._service_stubs()
        (self.root / "units" / "spotify-tracker-check.timer").write_text("[Timer]\n", encoding="utf-8")
        result = self._install(env, "--uninstall", crontab=self.OTHER_JOB + self.CRON_ENTRY)
        self.assertEqual(result.returncode, 0, result.stderr)

        self.assertFalse((self.root / "units" / "spotify-tracker-check.timer").exists())
        self.assertEqual((self.root / "stubs" / "crontab.out").read_text(encoding="utf-8"), self.OTHER_JOB)

    def test_uninstall_with_only_our_cron_entry_empties_the_table(self):
        result = self._install(None, "--uninstall", crontab=self.CRON_ENTRY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "stubs" / "crontab.out").read_text(encoding="utf-8").strip(), "")

    def test_install_help_prints_only_the_header(self):
        result = self._run("install.sh", "--help")

        self.assertEqual(result.returncode, 0)
        self.assertIn("Installer for the Spotify New Tracks notifier", result.stdout)
        self.assertIn("./install.sh --uninstall", result.stdout)
        self.assertNotIn("!/usr/bin/env bash", result.stdout)
        self.assertNotIn("Quote a path", result.stdout)


if __name__ == "__main__":
    unittest.main()
