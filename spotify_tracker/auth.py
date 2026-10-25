"""One-time OAuth Authorization Code flow to obtain a Spotify user refresh token.

This is only needed for endpoints that read the logged-in user's own data
(like their followed artists) which client-credentials app tokens can't see.
"""

import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
SCOPE = "user-follow-read"

# How long to wait for the browser to come back to the redirect URI. Without
# a limit, a login on a machine the browser can't reach (a headless server)
# would wait forever.
CALLBACK_TIMEOUT = 300  # seconds


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        self.server.auth_code = params.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_error = params.get("error", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_state = params.get("state", [None])[0]  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        if self.server.auth_code:  # type: ignore[attr-defined]
            self.wfile.write(b"<h1>Login successful</h1>You can close this tab and return to the terminal.")
        else:
            self.wfile.write(b"<h1>Login failed</h1>Check the terminal for details.")

    def log_message(self, format: str, *args) -> None:
        pass  # silence the default per-request access log


class _CallbackServer(HTTPServer):
    timeout = CALLBACK_TIMEOUT
    timed_out = False

    def handle_timeout(self) -> None:
        self.timed_out = True


def _wait_for_callback(port: int) -> tuple[str | None, str | None, str | None]:
    with _CallbackServer(("127.0.0.1", port), _CallbackHandler) as server:
        server.auth_code = None  # type: ignore[attr-defined]
        server.auth_error = None  # type: ignore[attr-defined]
        server.auth_state = None  # type: ignore[attr-defined]
        # Blocks until the redirect hits this server (or the timeout passes),
        # then stops.
        server.handle_request()
    if server.timed_out:
        raise SystemExit(
            f"Spotify authorization failed: no browser redirect reached 127.0.0.1:{port} "
            f"within {CALLBACK_TIMEOUT // 60} minutes. On a machine without a browser, run the "
            "login on your own computer and copy SPOTIFY_REFRESH_TOKEN into this machine's config.env "
            "(see README)."
        )
    return server.auth_code, server.auth_error, server.auth_state  # type: ignore[attr-defined]


def run_login_flow(client_id: str, client_secret: str, redirect_uri: str) -> str:
    """Runs the browser login flow and returns a long-lived refresh token."""
    port = urllib.parse.urlparse(redirect_uri).port or 80
    state = secrets.token_urlsafe(16)

    auth_url = AUTHORIZE_URL + "?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
        }
    )

    print(f"Opening your browser to authorize this app:\n{auth_url}\n")
    webbrowser.open(auth_url)

    code, error, returned_state = _wait_for_callback(port)
    if error or not code:
        raise SystemExit(f"Spotify authorization failed: {error or 'no code returned'}")
    if not secrets.compare_digest(returned_state or "", state):
        # The state we generated must come back unchanged; a mismatch means the
        # redirect didn't originate from the request we started (possible CSRF).
        raise SystemExit("Spotify authorization failed: state mismatch (possible CSRF); aborting.")

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
        timeout=15,
    )
    if not response.ok:
        # Spotify's body names the problem (e.g. "invalid_grant" for a
        # redirect URI mismatch), which a bare status code doesn't.
        raise SystemExit(f"Spotify token exchange failed: {response.status_code} {response.text}")
    refresh_token = response.json().get("refresh_token")
    if not refresh_token:
        raise SystemExit("Spotify did not return a refresh token.")
    return refresh_token
