"""Gets spotify_tracker.setup running inside the project's ./venv.

Standard library only: this runs before the dependencies are installed.

Started from any other Python, it creates ./venv if missing, installs
requirements.txt into it and runs the setup again with the venv's Python, so
the settings and login happen in the same environment the scheduled check
uses. Started from the venv's Python, it only installs requirements.txt if a
dependency is missing (as after a bare 'python -m venv venv').
"""

import importlib.util
import os
import subprocess
import sys
import venv
from pathlib import Path

# Same as config.REPO_DIR; config itself can't be imported before the
# dependencies are in.
REPO_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_DIR / "venv"
REQUIREMENTS = REPO_DIR / "requirements.txt"

# Import names of the packages in requirements.txt.
REQUIRED_MODULES = ("requests", "dotenv", "tqdm", "discord")

# Set on the re-run inside the venv, so a venv that doesn't look like one to
# in_venv() can't make the setup start itself over and over.
BOOTSTRAPPED_ENV = "SPOTIFY_TRACKER_BOOTSTRAPPED"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def in_venv() -> bool:
    return sys.prefix != sys.base_prefix and Path(sys.prefix).resolve() == VENV_DIR.resolve()


def _missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def _install_requirements(python: Path | str) -> None:
    print(f"Installing dependencies from {REQUIREMENTS.name} ...", flush=True)
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)]
    )
    if result.returncode != 0:
        raise SystemExit(f"Installing dependencies with {python} failed (exit code {result.returncode}).")


def _create_venv() -> None:
    print(f"Creating virtualenv in {VENV_DIR} ...", flush=True)
    try:
        venv.create(VENV_DIR, with_pip=True)
    except (OSError, subprocess.CalledProcessError) as err:
        raise SystemExit(
            f"Could not create {VENV_DIR}: {err}. On Debian/Ubuntu install the venv "
            "module first ('sudo apt install python3-venv'), or pass --no-venv to use "
            "this Python as it is."
        ) from err


def ensure_venv(argv: list[str]) -> None:
    """Returns once the dependencies are importable here; otherwise re-runs
    the setup with the venv's Python and exits with its exit code."""
    if in_venv() or os.environ.get(BOOTSTRAPPED_ENV):
        if _missing_modules():
            _install_requirements(sys.executable)
        return

    python = venv_python()
    if not python.exists():
        _create_venv()
    _install_requirements(python)

    print(f"Continuing with {python}\n", flush=True)
    # A child process rather than os.exec*, which on Windows returns to the
    # shell straight away and leaves the child fighting it for the console.
    result = subprocess.run(
        [str(python), "-m", "spotify_tracker.setup", *argv],
        cwd=REPO_DIR,
        env=dict(os.environ, **{BOOTSTRAPPED_ENV: "1"}),
    )
    raise SystemExit(result.returncode)
