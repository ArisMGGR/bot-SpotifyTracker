"""Helper to update a single KEY=value line in a config.env file in place."""

from pathlib import Path


def set_env_value(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    prefix = f"{key}="
    found = False
    # Every line with the key, not just the first: dotenv reads the last one,
    # so a duplicate left behind (e.g. a line pasted in by hand) would win.
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = f"{key}={value}"
            found = True
    if not found:
        lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
