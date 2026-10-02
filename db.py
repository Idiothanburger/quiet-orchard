"""
State storage: a single, git-friendly JSON file.

It's text (small diffs, reliable `git status`), loaded into memory once and written
once per run.

Layout:
{
  "version": 1,
  "artists":  {artist_id: {"name": ..., "baselined_at": ...}},
  "releases": {release_id: {"artist_id", "artist_name", "release_name",
                            "release_type", "release_date", "release_url", "detected_at"}},
  "meta":     {"last_warning_at": ...}
}
release_type and release_date are empty for releases recorded silently without details.
"""

from __future__ import annotations
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Optional

from models import Release

logger = logging.getLogger("tracker.db")

STATE_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class StateStore:
    def __init__(self, path: str | Path = "seen_releases.json"):
        self.path = Path(path)
        self.artists: dict[str, dict] = {}
        self.releases: dict[str, dict] = {}
        self.meta: dict[str, str] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # Refuse to continue: starting from empty would re-baseline silently at best,
            # and overwrite the file at worst.
            raise RuntimeError(f"State file {self.path} is unreadable: {e}") from e
        self.artists = data.get("artists") or {}
        self.releases = data.get("releases") or {}
        self.meta = data.get("meta") or {}

    # ---------------------------------------------------------------- reads

    def is_seen(self, release_id: str) -> bool:
        return release_id in self.releases

    def is_baselined(self, artist_id: str) -> bool:
        return bool(self.artists.get(artist_id, {}).get("baselined_at"))

    def count(self) -> int:
        return len(self.releases)

    # --------------------------------------------------------------- writes

    def mark_seen(self, release: Release, release_id: Optional[str] = None) -> None:
        rid = release_id or release.release_id
        if rid in self.releases:
            return
        self.releases[rid] = {
            "artist_id": release.artist_id,
            "artist_name": release.artist_name,
            "release_name": release.release_name,
            "release_type": release.release_type if release.release_date else "",
            "release_date": release.release_date,
            "release_url": f"https://music.amazon.com.au/albums/{rid}",
            "detected_at": _now_iso(),
        }
        self._dirty = True

    def mark_artist_baselined(self, artist_id: str, name: str = "") -> None:
        entry = self.artists.setdefault(artist_id, {})
        if not entry.get("baselined_at"):
            entry["baselined_at"] = _now_iso()
            self._dirty = True
        self.set_artist_name(artist_id, name)

    def set_artist_name(self, artist_id: str, name: str) -> None:
        if not name:
            return
        entry = self.artists.setdefault(artist_id, {})
        if entry.get("name") != name:
            entry["name"] = name
            self._dirty = True

    def get_meta(self, key: str) -> Optional[str]:
        return self.meta.get(key)

    def set_meta(self, key: str, value: str) -> None:
        if self.meta.get(key) != value:
            self.meta[key] = value
            self._dirty = True

    def save(self) -> bool:
        """Atomic write. Keys are sorted so diffs stay small. Returns True if written."""
        if not self._dirty:
            return False
        data = {
            "version": STATE_VERSION,
            "artists": dict(sorted(self.artists.items())),
            "releases": dict(sorted(self.releases.items())),
            "meta": dict(sorted(self.meta.items())),
        }
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp_path, self.path)
        self._dirty = False
        logger.info(f"Saved state: {len(self.releases)} releases, {len(self.artists)} artists")
        return True
