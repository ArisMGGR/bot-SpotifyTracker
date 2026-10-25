import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from spotify_tracker import bootstrap


def _done(returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode)


class EnsureVenvTest(unittest.TestCase):
    """No real virtualenv is created and pip never runs: venv.create and
    subprocess.run are mocked, and ./venv points at a temporary directory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.venv_dir = Path(tmp.name) / "venv"
        for patcher in (
            mock.patch.object(bootstrap, "VENV_DIR", self.venv_dir),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch("builtins.print"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        os.environ.pop(bootstrap.BOOTSTRAPPED_ENV, None)

    def _make_venv_python(self) -> Path:
        python = bootstrap.venv_python()
        python.parent.mkdir(parents=True)
        python.touch()
        return python

    def _ensure(self, *, inside: bool, run_results=(), missing=()):
        with (
            mock.patch.object(bootstrap, "in_venv", return_value=inside),
            mock.patch.object(bootstrap, "_missing_modules", return_value=list(missing)),
            mock.patch.object(bootstrap.venv, "create") as create,
            mock.patch.object(bootstrap.subprocess, "run", side_effect=list(run_results)) as run,
        ):
            try:
                bootstrap.ensure_venv(["--non-interactive"])
                exit_code = None
            except SystemExit as err:
                exit_code = err.code
        return create, run, exit_code

    def test_inside_the_venv_with_everything_installed_does_nothing(self):
        create, run, exit_code = self._ensure(inside=True)
        self.assertIsNone(exit_code)
        create.assert_not_called()
        run.assert_not_called()

    def test_inside_the_venv_installs_missing_dependencies(self):
        create, run, exit_code = self._ensure(inside=True, run_results=[_done()], missing=["dotenv"])
        self.assertIsNone(exit_code)
        create.assert_not_called()
        self.assertEqual(run.call_args.args[0][:4], [sys.executable, "-m", "pip", "install"])

    def test_outside_creates_the_venv_installs_and_reruns_inside_it(self):
        create, run, exit_code = self._ensure(inside=False, run_results=[_done(), _done(3)])
        create.assert_called_once_with(self.venv_dir, with_pip=True)
        python = str(bootstrap.venv_python())
        pip_call, rerun_call = run.call_args_list
        self.assertEqual(pip_call.args[0][:4], [python, "-m", "pip", "install"])
        self.assertEqual(rerun_call.args[0], [python, "-m", "spotify_tracker.setup", "--non-interactive"])
        self.assertEqual(rerun_call.kwargs["env"][bootstrap.BOOTSTRAPPED_ENV], "1")
        # The re-run's exit code becomes this run's.
        self.assertEqual(exit_code, 3)

    def test_outside_reuses_an_existing_venv(self):
        self._make_venv_python()
        create, run, exit_code = self._ensure(inside=False, run_results=[_done(), _done()])
        create.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(exit_code, 0)

    def test_a_failed_install_stops_before_the_rerun(self):
        self._make_venv_python()
        create, run, exit_code = self._ensure(inside=False, run_results=[_done(1)])
        self.assertEqual(run.call_count, 1)
        self.assertIn("failed", exit_code)

    def test_the_rerun_never_bootstraps_again(self):
        os.environ[bootstrap.BOOTSTRAPPED_ENV] = "1"
        create, run, exit_code = self._ensure(inside=False)
        self.assertIsNone(exit_code)
        create.assert_not_called()
        run.assert_not_called()

    def test_a_venv_that_cant_be_created_explains_why(self):
        with (
            mock.patch.object(bootstrap, "in_venv", return_value=False),
            mock.patch.object(bootstrap.venv, "create", side_effect=subprocess.CalledProcessError(1, "ensurepip")),
            mock.patch.object(bootstrap.subprocess, "run") as run,
        ):
            with self.assertRaises(SystemExit) as ctx:
                bootstrap.ensure_venv([])
        self.assertIn("python3-venv", str(ctx.exception))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
