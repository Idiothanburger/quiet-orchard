"""
Release data model and helpers: type guess, NZ-date age filter.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import re
from typing import Optional

from amazon_api import AlbumDetails, ListedRelease

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

NZ_FALLBACK_OFFSET = timezone(timedelta(hours=12))

_EP_TITLE = re.compile(r"(?:^|[\s\-(\[])e\.?p\.?[)\]]?$", re.IGNORECASE)
_ARTWORK_SIZE = re.compile(r"\._[^/]*_(?=\.\w+$)")


@dataclass
class Release:
    release_id: str          # Amazon album ASIN
    artist_id: str           # the tracked artist this release was found under
    artist_name: str         # Amazon's credit line, e.g. "Supercell [feat. Ann & Gaku]"
    release_name: str
    artwork_url: str
    release_date: str = ""   # YYYY-MM-DD; only known after fetching album details
    published_at: str = ""   # Amazon's raw datePublished
    track_count: int = 0
    duration_seconds: int = 0
    release_type: str = "Single"
    is_explicit: Optional[bool] = None
    copyright: str = ""
    duplicate_ids: list[str] = field(default_factory=list)

    @property
    def release_url(self) -> str:
        return f"https://music.amazon.com.au/albums/{self.release_id}"

    @property
    def artist_url(self) -> str:
        return f"https://music.amazon.com.au/artists/{self.artist_id}"

    @classmethod
    def from_listing(cls, item: ListedRelease, tracked_artist_id: str) -> Release:
        return cls(
            release_id=item.album_id,
            artist_id=tracked_artist_id,
            artist_name=item.artist_name or "Unknown Artist",
            release_name=item.title or "Unknown Title",
            artwork_url=full_size_artwork(item.artwork_url),
        )

    def apply_album_details(self, d: AlbumDetails) -> None:
        self.release_date = d.release_date
        self.published_at = d.published_at
        self.track_count = d.track_count
        self.duration_seconds = d.duration_seconds
        self.is_explicit = d.is_explicit
        self.copyright = d.copyright
        if d.artwork_url:
            self.artwork_url = d.artwork_url
        self.release_type = classify_release_type(self.release_name, self.track_count, self.duration_seconds)

    def dedupe_key(self) -> tuple:
        """Same title, date and track count = the same release (e.g. explicit + clean versions)."""
        return (self.artist_id, normalize_title(self.release_name), self.release_date, self.track_count)


def classify_release_type(name: str, track_count: int, duration_seconds: int = 0) -> str:
    """
    Amazon labels everything "Album", so guess with Spotify's rule: 1-3 tracks under
    30 minutes = single; up to 6 tracks under 30 minutes = EP; anything else = album.
    """
    if _EP_TITLE.search(name.strip()):
        return "EP"
    short = duration_seconds < 30 * 60 if duration_seconds else True
    if track_count <= 3 and short:
        return "Single"
    if track_count <= 6 and short:
        return "EP"
    return "Album"


def full_size_artwork(url: str) -> str:
    """List thumbnails look like .../51Ux7-82apL._SX354_SY354_BL0_QL100_.jpg; drop the size suffix."""
    return _ARTWORK_SIZE.sub("", url or "")


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").casefold()).strip()


def nz_today(now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    tz = NZ_FALLBACK_OFFSET
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo("Pacific/Auckland")
        except ZoneInfoNotFoundError:
            pass  # e.g. Windows without the tzdata package; UTC+12 is close enough locally
    return now.astimezone(tz).date()


def parse_release_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


def is_recent_release(release_date: str, max_age_days: int = 7, today: Optional[date] = None) -> bool:
    """True if released within the last max_age_days (NZ calendar) or dated in the future."""
    released = parse_release_date(release_date)
    if released is None:
        return False
    today = today or nz_today()
    return (today - released).days <= max_age_days


def is_upcoming(release_date: str, today: Optional[date] = None) -> bool:
    released = parse_release_date(release_date)
    if released is None:
        return False
    return released > (today or nz_today())
