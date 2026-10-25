"""One-time setup: authorize this app to read your followed artists.

Run with: python -m spotify_tracker.login
"""

from spotify_tracker.auth import run_login_flow
from spotify_tracker.config import env_path, load_client_credentials
from spotify_tracker.env_file import set_env_value


def main() -> None:
    client_id, client_secret, redirect_uri = load_client_credentials()
    refresh_token = run_login_flow(client_id, client_secret, redirect_uri)

    # The same file config.py loads, not one in the current directory.
    path = env_path()
    set_env_value(path, "SPOTIFY_REFRESH_TOKEN", refresh_token)
    print(f"Saved SPOTIFY_REFRESH_TOKEN to {path}.")
    print("You can now run: python -m spotify_tracker.main")


if __name__ == "__main__":
    main()
