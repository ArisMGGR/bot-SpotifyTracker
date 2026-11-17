"""Spotify Web API client.

Uses two separate token types:
- an app-only client-credentials token for public catalog data (an artist's
  releases), which never requires the user to log in.
- a user access token (refreshed from a stored refresh token obtained via
  spotify_tracker.login) for data that belongs to the logged-in account,
  such as the list of artists they follow.
"""

import math
import socket
import ssl
import time
from urllib.parse import urlparse

import requests

from spotify_tracker.state import utc_today

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Global pacing for every API request. Spotify's limit is a rolling 30-second
# window, so even spacing of WINDOW / MAX_PER_WINDOW guarantees the window
# count stays under the cap regardless of how requests cluster (catalog
# pagination, back-to-back baselines, the followed-list refresh). A sustained
# ~10 requests per 30s got this app blocked; 5 per 30s leaves headroom.
RATE_LIMIT_WINDOW = 30.0  # seconds
MAX_REQUESTS_PER_WINDOW = 5
MIN_REQUEST_INTERVAL = RATE_LIMIT_WINDOW / MAX_REQUESTS_PER_WINDOW

# Release groups checked for each artist, one request each (see
# get_latest_releases), and how many of the newest releases to fetch per
# group. Far more releases than this never come out between two checks.
RELEASE_GROUPS = ("album", "single")
RELEASES_PER_GROUP = 10

# "Appears On" (other artists' releases an artist features on) is opt-in per
# artist. Unlike albums and singles, Spotify doesn't list it newest first, so
# it has to be read in full: one call per 10 releases. An artist whose list
# would take more calls than this is only given its first page, so one artist
# can't eat most of the day's budget.
MAX_APPEARS_ON_CALLS = 50


class SpotifyRateLimitError(Exception):
    """Raised on any 429 from Spotify.

    A 429 means a penalty is already active, and any request during the
    penalty window (including an in-process retry) extends it — so the
    caller must abort the whole run and try again much later. retry_after
    is Spotify's Retry-After header, or 0 if it wasn't sent.
    """

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Spotify rate limit active; retry after {retry_after}s")


class SpotifyDailyLimitError(Exception):
    """Raised before a request that would exceed the day's self-imposed call
    budget.

    Unlike a 429, this penalty is our own: Spotify allows roughly 105 calls
    per day, so the client refuses to make a call once the day's budget
    (default 100) is spent, stopping short of the point where Spotify itself
    would start blocking. The remaining artists are picked up on the next
    day's runs, once the budget resets.
    """

    def __init__(self, calls_today: int, limit: int):
        self.calls_today = calls_today
        self.limit = limit
        super().__init__(f"Daily Spotify call budget reached ({calls_today}/{limit})")


class SpotifyUnavailableError(Exception):
    """Raised when Spotify can't be used for the rest of this run: a request
    never got an answer (network down, timeout), or an access token couldn't
    be fetched (e.g. a revoked refresh token).

    Every further request would fail the same way while still counting
    toward the day's budget, so the caller must stop the run rather than
    skip to the next artist.
    """


def _iter_causes(err: BaseException):
    """Yield err and every exception wrapped inside it. requests buries the
    real cause several layers deep (ConnectionError -> MaxRetryError ->
    NameResolutionError -> socket.gaierror), linked through __cause__,
    __context__, urllib3's .reason and the first positional arg."""
    seen: set[int] = set()
    stack = [err]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        stack.append(current.__cause__)
        stack.append(current.__context__)
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException):
            stack.append(reason)
        if current.args and isinstance(current.args[0], BaseException):
            stack.append(current.args[0])


def describe_network_error(err: requests.RequestException) -> str:
    """Explain in plain words why a request never got an answer, instead of
    the multi-layer urllib3 message."""
    request = getattr(err, "request", None)
    host = urlparse(request.url).hostname if request is not None and request.url else None
    host = host or "Spotify"
    causes = list(_iter_causes(err))
    names = {type(c).__name__ for c in causes}

    if any(isinstance(c, socket.gaierror) for c in causes) or "NameResolutionError" in names:
        return (
            f"could not look up {host} (DNS lookup failed). This machine most likely had no "
            "internet connection, or its DNS server wasn't answering, when the run started"
        )
    if isinstance(err, requests.ConnectTimeout):
        return f"timed out connecting to {host}. The network is slow or down, or Spotify isn't responding"
    if isinstance(err, requests.Timeout):
        return f"{host} accepted the connection but didn't answer in time. Spotify may be having problems"
    if isinstance(err, requests.exceptions.SSLError) or any(isinstance(c, ssl.SSLError) for c in causes):
        return (
            f"the secure (TLS) connection to {host} failed. Check the system clock, and whether "
            "a proxy or captive portal (e.g. a hotel/public Wi-Fi login page) is intercepting traffic"
        )
    if any(isinstance(c, ConnectionRefusedError) for c in causes):
        return f"{host} refused the connection. A firewall or proxy may be blocking it"
    if any(isinstance(c, ConnectionResetError) for c in causes):
        return f"the connection to {host} was cut off mid-request. The network dropped or is unstable"
    return f"network error reaching {host} ({type(err).__name__}). Check the internet connection"


class AppearsOnBudgetError(Exception):
    """Raised after the first page of an artist's "Appears On" list, when the
    rest of the list won't fit in what's left of the day's budget.

    Raised before the remaining pages are requested, so only that first call
    is spent. calls_needed is what reading the list takes (all pages, or just
    the first if the list is capped), so the caller can budget for it and
    retry the artist on a later run.
    """

    def __init__(self, calls_needed: int):
        self.calls_needed = calls_needed
        super().__init__(f"Appears On list needs {calls_needed} calls")


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        daily_call_limit: int | None = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._app_token = None
        self._app_token_expiry = 0.0
        self._user_token = None
        self._user_token_expiry = 0.0
        self._last_request_at = 0.0
        # Count of HTTP requests actually sent to Spotify (token refreshes and
        # API calls). Reported per run so you can see how close a run came to
        # the rate limit, even when one stops it early.
        self.request_count = 0
        # Running total of calls attributed to the current UTC day (_day),
        # spanning every run of the day (persisted between runs by the caller
        # via start_day). Spotify's daily quota is what actually blocks the app
        # long-term, so this — not request_count — is what the budget guards.
        self._daily_calls = 0
        self._day = utc_today()
        self._daily_call_limit = daily_call_limit

    @property
    def daily_calls(self) -> int:
        """Calls counted toward the current UTC day. Resets to 0 when the day
        changes, including in the middle of a run, so calls made before
        midnight aren't charged to the new day's budget."""
        today = utc_today()
        if today != self._day:
            self._day = today
            self._daily_calls = 0
        return self._daily_calls

    def start_day(self, calls_already_made: int) -> None:
        """Seed the day's running call total from persisted state at the start
        of a run, so the daily budget is enforced across separate runs (cron)
        as well as within one long-lived process (--interval)."""
        self._day = utc_today()
        self._daily_calls = calls_already_made

    def _check_daily_budget(self) -> None:
        if self._daily_call_limit is not None and self.daily_calls >= self._daily_call_limit:
            raise SpotifyDailyLimitError(self.daily_calls, self._daily_call_limit)

    def _account_request(self) -> None:
        """Refuse a request that would exceed the day's budget, otherwise count
        the one about to be sent (toward both the per-run and per-day totals)."""
        self._check_daily_budget()  # also rolls daily_calls over to a new day
        self.request_count += 1
        self._daily_calls += 1

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Send one request to Spotify (token endpoint or Web API): refuse it
        if the day's budget is spent, space it from the previous one, count
        it, and raise SpotifyRateLimitError on a 429."""
        # Bail before the pacing sleep if the day's budget is already spent —
        # no point waiting 6 seconds for a call we're going to refuse.
        self._check_daily_budget()

        # Evenly space every request so the rolling-window count can never
        # exceed MAX_REQUESTS_PER_WINDOW, no matter how calls cluster.
        elapsed = time.time() - self._last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)

        self._account_request()
        try:
            response = requests.request(method, url, timeout=15, **kwargs)
        except (requests.ConnectionError, requests.Timeout) as err:
            # The traceback chain is kept (from err) for debugging, but the
            # message itself says what went wrong in plain words.
            raise SpotifyUnavailableError(f"Spotify unreachable: {describe_network_error(err)}") from err
        finally:
            # A failed request still reached (or tried to reach) Spotify, so it
            # counts for pacing too; otherwise retries after a failure would go
            # out back to back.
            self._last_request_at = time.time()

        if response.status_code == 429:
            # Never retry in-process: any request during the penalty window
            # extends it, and Retry-After can under-report (or be missing
            # entirely) while the block is still active.
            try:
                retry_after = int(response.headers.get("Retry-After", "0"))
            except ValueError:
                retry_after = 0
            raise SpotifyRateLimitError(retry_after)
        return response

    def _request_token(self, data: dict) -> dict:
        response = self._send("POST", TOKEN_URL, data=data, auth=(self._client_id, self._client_secret))
        if not response.ok:
            # Spotify's body says why (e.g. "invalid_grant": the refresh token
            # was revoked), which a bare "400 Bad Request" doesn't.
            message = f"Spotify token request ({data['grant_type']}) failed: {response.status_code} {response.text}"
            if "invalid_grant" in response.text:
                message += (
                    ". The saved Spotify login was revoked or has expired; this won't fix itself. "
                    "Log in again with: python -m spotify_tracker.login"
                )
            elif "invalid_client" in response.text:
                message += ". The Spotify client ID or secret in config.env is wrong"
            raise SpotifyUnavailableError(message)
        return response.json()

    def _get_app_token(self) -> str:
        if self._app_token and time.time() < self._app_token_expiry:
            return self._app_token

        payload = self._request_token({"grant_type": "client_credentials"})
        self._app_token = payload["access_token"]
        self._app_token_expiry = time.time() + payload["expires_in"] - 60
        return self._app_token

    def _get_user_token(self) -> str:
        if self._user_token and time.time() < self._user_token_expiry:
            return self._user_token

        payload = self._request_token({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
        self._user_token = payload["access_token"]
        self._user_token_expiry = time.time() + payload["expires_in"] - 60
        return self._user_token

    def _get(self, url_or_path: str, params=None, *, auth: str = "app") -> dict:
        if url_or_path.startswith("http"):
            url = url_or_path
        else:
            url = f"{API_BASE}{url_or_path}"

        token = self._get_user_token() if auth == "user" else self._get_app_token()
        headers = {"Authorization": f"Bearer {token}"}
        response = self._send("GET", url, headers=headers, params=params)

        if not response.ok:
            # Surface Spotify's actual error body instead of a bare "400 Bad
            # Request", since that's usually the only clue as to what it
            # didn't like.
            raise requests.exceptions.HTTPError(
                f"{response.status_code} error for url {response.url}: {response.text}",
                response=response,
            )
        return response.json()

    def get_followed_artists(self) -> list[dict]:
        """Return every artist the logged-in user follows."""
        artists: list[dict] = []
        url = f"{API_BASE}/me/following"
        params = {"type": "artist", "limit": 50}

        while url:
            data = self._get(url, params=params, auth="user")
            block = data.get("artists", {})
            artists.extend(block.get("items", []))
            url = block.get("next")
            params = None

        return artists

    def get_latest_releases(self, artist_id: str) -> list[dict]:
        """Return the artist's newest albums and newest singles: one request
        per group in RELEASE_GROUPS, so every check costs the same few calls
        regardless of catalog size.

        Groups are requested separately because a combined
        include_groups=album,single response lists every album before any
        single, so a new single can sit many pages deep. Within one group,
        Spotify lists the newest releases first.

        No market filter: with market=GR, Spotify's API left out Bastille's
        collaborations with Misfits Together ("Oh America (1951)", "Only Name
        I Own") even though they're listed on Bastille's page, so those
        releases were never alerted on.

        May raise SpotifyRateLimitError between groups; the caller must then
        discard the partial result rather than record it.
        """
        releases: list[dict] = []
        for group in RELEASE_GROUPS:
            data = self._get(
                f"/artists/{artist_id}/albums",
                params={"include_groups": group, "limit": RELEASES_PER_GROUP},
            )
            releases.extend(_with_group(data.get("items", []), group))
        return releases

    def get_appears_on(self, artist_id: str, reserve_calls: int = 0) -> tuple[list[dict], int]:
        """Return the artist's "Appears On" releases, plus the number of calls
        the full list takes.

        The list isn't ordered by date, so it's read in full. If that would
        take more than MAX_APPEARS_ON_CALLS, only the first page is returned
        (the caller can tell from the call count). Raises AppearsOnBudgetError
        after the first page if the rest, plus reserve_calls the caller still
        needs afterwards, won't fit in today's budget.
        """
        url = f"{API_BASE}/artists/{artist_id}/albums"
        data = self._get(url, params={"include_groups": "appears_on", "limit": RELEASES_PER_GROUP})
        releases = _with_group(data.get("items", []), "appears_on")
        calls_needed = max(1, math.ceil(data.get("total", 0) / RELEASES_PER_GROUP))
        capped = calls_needed > MAX_APPEARS_ON_CALLS
        pages_left = 0 if capped else calls_needed - 1

        if (
            self._daily_call_limit is not None
            and self.daily_calls + pages_left + reserve_calls > self._daily_call_limit
        ):
            raise AppearsOnBudgetError(1 + pages_left)
        if capped:
            return releases, calls_needed

        url = data.get("next")
        while url:
            data = self._get(url)
            releases.extend(_with_group(data.get("items", []), "appears_on"))
            url = data.get("next")
        return releases, calls_needed


def _with_group(releases: list[dict], group: str) -> list[dict]:
    """Label each release with the group it was requested under. Spotify no
    longer fills in album_group, and each request asks for one group only."""
    for release in releases:
        release["album_group"] = group
    return releases
