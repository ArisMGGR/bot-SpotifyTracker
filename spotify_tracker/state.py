"""Persistent state: seen releases per artist, check times, and the cached
followed-artists list.

File layout (state.json):
{
  "artists": {
    "<artist_id>": {
      "name": "...",             # for human readability of the file
      "releases": ["<album_id>", ...],
      "checked_at": 1751400000,  # epoch seconds of the last successful check
      "last_cost": 3             # Spotify calls that check took (optional)
    }
  },
  "followed": {
    "fetched_at": 1751400000,
    "artists": [{"id": "...", "name": "..."}, ...]
  },
  "rate_limited_until": 0,    # epoch seconds; runs are skipped before this
  "api_usage": {"date": "2026-10-23", "count": 42}   # calls on that UTC day
}

Older versions of this file were just {"<artist_id>": ["<album_id>", ...]};
those are migrated on load (check times start at 0 so they sort as most
overdue).
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def utc_today() -> str:
    """The current calendar day as YYYY-MM-DD in UTC. Spotify's daily quota
    resets on a fixed clock, so use UTC rather than local time so the reset is
    deterministic regardless of where the machine runs."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, path: Path):
        self._path = path
        self._data: dict = {
            "artists": {},
            "followed": None,
            "rate_limited_until": 0,
            "api_usage": None,
        }
        if self._path.exists():
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._data = self._migrate(raw)

    @staticmethod
    def _migrate(raw: dict) -> dict:
        if "artists" in raw and isinstance(raw.get("artists"), dict):
            raw.setdefault("followed", None)
            raw.setdefault("rate_limited_until", 0)
            raw.setdefault("api_usage", None)
            return raw
        # Old flat format: {artist_id: [release ids]}
        return {
            "artists": {
                artist_id: {"name": "", "releases": ids, "checked_at": 0}
                for artist_id, ids in raw.items()
            },
            "followed": None,
            "rate_limited_until": 0,
            "api_usage": None,
        }

    # --- daily API-call budget tracking ---

    def api_calls_today(self) -> int:
        """Spotify calls already counted for the current UTC day (0 if the
        stored tally is from an earlier day — i.e. the budget has reset)."""
        usage = self._data.get("api_usage")
        if not usage or usage.get("date") != utc_today():
            return 0
        return usage.get("count", 0)

    def set_api_calls_today(self, count: int) -> None:
        """Persist today's running call total, stamped with today's date so a
        new day is recognised as a fresh budget."""
        self._data["api_usage"] = {"date": utc_today(), "count": int(count)}

    # --- rate-limit penalty persistence ---

    def rate_limited_until(self) -> float:
        """Epoch seconds until which Spotify's penalty is assumed active."""
        return self._data.get("rate_limited_until") or 0

    def set_rate_limited_until(self, timestamp: float) -> None:
        self._data["rate_limited_until"] = int(timestamp)

    # --- per-artist release tracking ---

    def has_artist(self, artist_id: str) -> bool:
        return artist_id in self._data["artists"]

    def known_release_ids(self, artist_id: str) -> set[str]:
        entry = self._data["artists"].get(artist_id)
        return set(entry["releases"]) if entry else set()

    def checked_at(self, artist_id: str) -> float:
        entry = self._data["artists"].get(artist_id)
        return entry.get("checked_at", 0) if entry else 0

    def last_checked_at(self) -> float:
        """When any artist was last checked successfully (0 if never)."""
        return max((entry.get("checked_at", 0) for entry in self._data["artists"].values()), default=0)

    def last_cost(self, artist_id: str) -> int | None:
        """How many Spotify calls the previous check of this artist took, or
        None if never recorded."""
        entry = self._data["artists"].get(artist_id)
        return entry.get("last_cost") if entry else None

    def set_last_cost(self, artist_id: str, cost: int) -> None:
        """Update an already-tracked artist's expected cost without marking
        them checked, so later runs budget for it. Untracked artists are left
        alone: adding an entry would make them skip their silent baseline."""
        entry = self._data["artists"].get(artist_id)
        if entry is not None:
            entry["last_cost"] = int(cost)

    def record_check(
        self, artist_id: str, name: str, release_ids: set[str], cost: int | None = None
    ) -> None:
        entry = {
            "name": name,
            "releases": sorted(release_ids),
            "checked_at": int(time.time()),
        }
        if cost is not None:
            # What this check cost. Used to budget for artists whose "Appears
            # On" list is checked, whose cost varies with the list's size.
            entry["last_cost"] = int(cost)
        self._data["artists"][artist_id] = entry

    # --- followed-artists cache ---

    def cached_followed_artists(self, max_age_seconds: float) -> list[dict] | None:
        cache = self._data.get("followed")
        if not cache:
            return None
        if time.time() - cache.get("fetched_at", 0) > max_age_seconds:
            return None
        return cache["artists"]

    def set_followed_artists(self, artists: list[dict]) -> None:
        self._data["followed"] = {
            "fetched_at": int(time.time()),
            "artists": [{"id": a["id"], "name": a["name"]} for a in artists],
        }

    def save(self) -> None:
        # Write a temporary file and swap it in, so an interrupted save (kill,
        # power loss, full disk) leaves the previous state.json intact rather
        # than a truncated one that every later run would fail to parse.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
