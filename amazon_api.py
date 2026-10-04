"""
Minimal client for Amazon Music's web-player API (standard library only).

This is the unofficial API behind music.amazon.com.au. No login: each run starts a
guest session from config.json. Request headers travel as a JSON string inside the
body's "headers" field, not as HTTP headers.

- Shared rate limiter across worker threads (2 req/s by default; ~13 req/s bursts got 429s).
- A 429, 403 or non-JSON reply (e.g. a CAPTCHA page) blocks the rest of the run:
  there's no Retry-After to honor, and pushing on risks a longer block.
- Retries transient network / 5xx errors with backoff.
"""

from __future__ import annotations
from dataclasses import dataclass
import gzip
import json
import logging
import re
import threading
import time
from typing import Optional
import urllib.error
import urllib.request
import uuid

logger = logging.getLogger("tracker.api")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
ARTIST_LIST_PATH = "showCatalogMusicItems/uri:_SLASH__SLASH_artist_SLASH_{}_SLASH_chronological-albums"
_ALBUM_LINK = re.compile(r"/albums/([A-Z0-9]{10})")


class AmazonError(Exception):
    """Request failed after retries, or returned something unexpected."""


class AmazonBlocked(AmazonError):
    """429 / 403 / non-JSON: stop making requests for the rest of the run."""


class ArtistUnavailable(AmazonError):
    """Amazon answered with its 'Service error' dialog instead of the artist's releases."""


class RateLimiter:
    """Thread-safe minimum interval between requests."""

    def __init__(self, requests_per_second: float):
        self.min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self.lock = threading.Lock()
        self.next_slot = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass
class ListedRelease:
    """One entry of an artist's newest-first release list."""
    album_id: str
    title: str
    artist_name: str
    artwork_url: str


@dataclass
class AlbumDetails:
    title: str = ""
    artist_name: str = ""
    release_date: str = ""          # YYYY-MM-DD (date part of Amazon's datePublished)
    published_at: str = ""          # Amazon's raw datePublished, e.g. 2019-09-11T10:00Z
    track_count: int = 0
    duration_seconds: int = 0
    is_explicit: Optional[bool] = None
    copyright: str = ""
    artwork_url: str = ""


class AmazonMusicClient:
    def __init__(self, domain: str = "music.amazon.com.au", requests_per_second: float = 2.0,
                 max_retries: int = 3, timeout: float = 30.0, read_deadline: float = 60.0):
        self.domain = domain
        self.rate_limiter = RateLimiter(requests_per_second)
        self.max_retries = max_retries
        self.timeout = timeout              # per socket operation (connect, each read)
        self.read_deadline = read_deadline  # whole response body
        self.cfg: dict = {}
        self._blocked: Optional[AmazonBlocked] = None
        self._count_lock = threading.Lock()
        self.request_count = 0

    @property
    def blocked(self) -> bool:
        return self._blocked is not None

    # --------------------------------------------------------------- session

    def start_session(self) -> None:
        """Guest session: device ID, session ID and CSRF token. Fails once, up front."""
        text = self._post(f"https://{self.domain}/config.json", {})
        try:
            cfg = json.loads(text)
            for key in ("deviceId", "sessionId", "csrf", "version", "siteRegion"):
                cfg[key]  # noqa: B018 - presence check
        except (ValueError, KeyError) as e:
            raise AmazonError(f"Unexpected config.json response: {text[:200]!r}") from e
        self.cfg = cfg
        logger.info(f"Amazon session started (territory {cfg.get('musicTerritory')}, region {cfg['siteRegion']})")

    # -------------------------------------------------------------- requests

    def _post(self, url: str, body: dict) -> str:
        if self._blocked:
            raise self._blocked
        data = json.dumps(body).encode("utf-8")
        last_error: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            self.rate_limiter.wait()
            if self._blocked:
                raise self._blocked
            with self._count_lock:
                self.request_count += 1
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
                "Origin": f"https://{self.domain}",
                "Referer": f"https://{self.domain}/",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = read_with_deadline(resp, time.monotonic() + self.read_deadline)
                    encoding = resp.headers.get("Content-Encoding")
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    self._block(f"HTTP {e.code}")
                if 500 <= e.code < 600:
                    last_error = e
                    time.sleep(attempt * 2.0)
                    continue
                raise AmazonError(f"HTTP {e.code} for {_short(url)}") from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_error = e
                time.sleep(attempt * 2.0)
                continue
            if encoding == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
        raise AmazonError(f"Gave up on {_short(url)} after {self.max_retries} attempts: {last_error}")

    def _block(self, reason: str) -> None:
        self._blocked = AmazonBlocked(f"Amazon blocked requests ({reason}) after {self.request_count} "
                                      f"requests this run")
        logger.error(str(self._blocked))
        raise self._blocked

    def _api(self, path: str, extra: Optional[dict] = None) -> dict:
        """POST to the skill API. `path` may be a full URL (pagination links are)."""
        if not self.cfg:
            self.start_session()
        c = self.cfg
        headers = {
            "x-amzn-authentication": json.dumps({
                "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement", "accessToken": ""}),
            "x-amzn-device-model": "WEBPLAYER",
            "x-amzn-device-width": "1920",
            "x-amzn-device-family": "WebPlayer",
            "x-amzn-device-id": c["deviceId"],
            "x-amzn-user-agent": USER_AGENT,
            "x-amzn-session-id": c["sessionId"],
            "x-amzn-device-height": "1080",
            "x-amzn-request-id": str(uuid.uuid4()),
            "x-amzn-device-language": c.get("displayLanguage", "en_AU"),
            "x-amzn-currency-of-preference": "AUD",
            "x-amzn-os-version": "1.0",
            "x-amzn-application-version": c["version"],
            "x-amzn-device-time-zone": "Pacific/Auckland",
            "x-amzn-timestamp": str(int(time.time() * 1000)),
            "x-amzn-csrf": json.dumps({
                "interface": "CSRFInterface.v1_0.CSRFHeaderElement", "token": c["csrf"]["token"],
                "timestamp": c["csrf"]["ts"], "rndNonce": c["csrf"]["rnd"]}),
            "x-amzn-music-domain": self.domain,
            "x-amzn-referer": self.domain,
            "x-amzn-affiliate-tags": "",
            "x-amzn-ref-marker": "",
            "x-amzn-page-url": f"https://{self.domain}/",
            "x-amzn-weblab-id-overrides": "",
            "x-amzn-video-player-token": "",
            "x-amzn-feature-flags": "hd-supported,uhd-supported",
            "x-amzn-has-profile-id": "",
            "x-amzn-age-band": "",
        }
        body = {**(extra or {}), "userHash": json.dumps({"level": "LIBRARY_MEMBER"}), "headers": json.dumps(headers)}
        url = path if path.startswith("https://") else \
            f"https://{c['siteRegion'].lower()}.mesk.skill.music.a2z.com/api/{path}"
        text = self._post(url, body)
        try:
            data = json.loads(text)
            data["methods"][0]
        except (ValueError, KeyError, IndexError, TypeError):
            self._block(f"unexpected response from {_short(url)}: {text[:150]!r}")
        return data

    # ------------------------------------------------------------ endpoints

    def artist_releases(self, artist_id: str, page_url: str = "") -> tuple[list[ListedRelease], str]:
        """One page (20) of an artist's releases, newest first, and the next page's URL ("" if none)."""
        data = self._api(page_url or ARTIST_LIST_PATH.format(artist_id))
        method = data["methods"][0]
        if page_url:  # later pages: {"items": [...], "onEndOfWidget": [...]}
            items, end = method.get("items") or [], method.get("onEndOfWidget") or []
        else:
            template = method.get("template") or {}
            if "DialogTemplate" in template.get("interface", ""):
                raise ArtistUnavailable(f"Amazon returned 'Service error' for artist {artist_id}")
            widgets = template.get("widgets") or []
            if not widgets:
                return [], ""
            items, end = widgets[0].get("items") or [], widgets[0].get("onEndOfWidget") or []
        releases = []
        for it in items:
            m = _ALBUM_LINK.match((it.get("primaryLink") or {}).get("deeplink", ""))
            if m:
                releases.append(ListedRelease(m.group(1), _text(it.get("primaryText")),
                                              _text(it.get("secondaryText")), it.get("image") or ""))
        next_url = next((e.get("url", "") for e in end if e.get("url")), "")
        return releases, next_url

    def album(self, album_id: str) -> AlbumDetails:
        template = self._api("showCatalogAlbum", {"id": album_id})["methods"][0].get("template") or {}
        return parse_album(template)


def read_with_deadline(resp, deadline: float, clock=time.monotonic) -> bytes:
    """Read a response body, giving up at `deadline`. The socket timeout alone restarts
    with every byte received, so a slowly trickling response could otherwise hang a run."""
    chunks = []
    while True:
        if clock() > deadline:
            raise TimeoutError("response body took too long")
        chunk = resp.read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def parse_album(template: dict) -> AlbumDetails:
    """Album page: the header fields plus the schema.org MusicAlbum block in templateData."""
    d = AlbumDetails(
        title=_text(template.get("headerText")),
        artist_name=_text(template.get("headerPrimaryText")),
        copyright=_text(template.get("footer")),
        artwork_url=template.get("headerImage") or "",
    )
    scripts = (((template.get("templateData") or {}).get("seoHead") or {}).get("script")) or []
    for s in scripts:
        try:
            ld = json.loads(s.get("innerHTML") or "")
        except ValueError:
            continue
        if ld.get("@type") != "MusicAlbum":
            continue
        d.title = ld.get("name") or d.title
        d.artist_name = (ld.get("byArtist") or {}).get("name") or d.artist_name
        d.published_at = ld.get("datePublished") or ""
        d.release_date = d.published_at[:10]
        d.track_count = int(ld.get("numTracks") or 0)
        d.duration_seconds = parse_duration(ld.get("timeRequired") or "")
        tracks = ld.get("track") or []
        flags = [t.get("isFamilyFriendly") for t in tracks if isinstance(t.get("isFamilyFriendly"), bool)]
        if flags:
            d.is_explicit = not all(flags)
        break
    return d


def parse_duration(value: str) -> int:
    """ISO 8601 duration like PT14M46S or PT1H2M3S -> seconds."""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value or "")
    if not m:
        return 0
    h, mnt, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mnt * 60 + s


def _text(v) -> str:
    return ((v.get("text") if isinstance(v, dict) else v) or "").strip()


def _short(url: str) -> str:
    return url.split("?", 1)[0].rsplit("/", 1)[-1][:80]
