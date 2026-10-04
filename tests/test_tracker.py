"""
Unit tests for the tracker. Run: python -m unittest discover -s tests
No network: Amazon and Discord are faked.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import gzip
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from amazon_api import (AlbumDetails, AmazonBlocked, AmazonError, AmazonMusicClient, ArtistUnavailable,
                        ListedRelease, parse_album, parse_duration, read_with_deadline)
from db import StateStore
from discord_client import DiscordClient
from models import (Release, classify_release_type, full_size_artwork, is_recent_release, is_upcoming,
                    nz_today, parse_release_date)
from notifier import Config, ReleaseTracker

A1 = "B000000001"
A2 = "B000000002"
A3 = "B000000003"


def days_ago(n: int) -> str:
    return (nz_today() - timedelta(days=n)).isoformat()


def rel(album_id: str, title: str, released: str, tracks: int = 1, artist: str = "Artist") -> dict:
    """A fake catalog entry: what the list shows plus what the album page says."""
    return {"id": album_id, "title": title, "date": released, "tracks": tracks, "artist": artist}


class FakeAmazon:
    """Serves artist release lists from {artist_id: [rel(...), ...]}, newest first, 20 per page."""

    def __init__(self, catalog: dict, fail: set[str] | None = None, explicit: set[str] | None = None,
                 unavailable: set[str] | None = None, block_after: int | None = None,
                 hang: set[str] | None = None, release: threading.Event | None = None):
        self.catalog = catalog
        self.fail = fail or set()
        self.hang = hang or set()
        self.release = release  # set by the test to let hung requests finish
        self.unavailable = unavailable or set()
        self.explicit = explicit or set()
        self.block_after = block_after
        self.request_count = 0
        self.list_calls: list[tuple] = []
        self.album_calls: list[str] = []
        self.blocked = False

    def _count(self):
        self.request_count += 1
        if self.block_after is not None and self.request_count > self.block_after:
            self.blocked = True
            raise AmazonBlocked("Amazon blocked requests (HTTP 429)")

    def start_session(self):
        pass

    def artist_releases(self, artist_id, page_url=""):
        self._count()
        offset = int(page_url or 0)
        self.list_calls.append((artist_id, offset))
        if artist_id in self.hang:
            self.release.wait(10)
        if artist_id in self.fail:
            raise AmazonError("boom")
        if artist_id in self.unavailable:
            raise ArtistUnavailable("Service error")
        items = self.catalog.get(artist_id, [])
        page = items[offset:offset + 20]
        nxt = str(offset + 20) if offset + 20 < len(items) else ""
        return [ListedRelease(r["id"], r["title"], r["artist"], f"https://img/{r['id']}._SX354_.jpg")
                for r in page], nxt

    def album(self, album_id):
        self._count()
        self.album_calls.append(album_id)
        r = next(r for items in self.catalog.values() for r in items if r["id"] == album_id)
        return AlbumDetails(title=r["title"], artist_name=r["artist"], release_date=r["date"][:10],
                            published_at=r["date"] and r["date"][:10] + "T10:00Z", track_count=r["tracks"],
                            duration_seconds=r["tracks"] * 200, is_explicit=album_id in self.explicit,
                            copyright="℗ 2026 Label")


class FakeDiscord:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.sent: list[Release] = []
        self.warnings: list[str] = []

    def send_release_notification(self, release):
        self.sent.append(release)
        return self.ok

    def send_warning(self, title, description):
        self.warnings.append(title)
        return True


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.state_path = self.dir / "seen_releases.json"

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, catalog, artists, discord_ok=True, amazon_kw=None, **cfg_overrides):
        (self.dir / "artists.txt").write_text("\n".join(artists), encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            cfg = Config()
        cfg.artists_file = str(self.dir / "artists.txt")
        cfg.state_path = str(self.state_path)
        cfg.concurrency = 2
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
        amazon = FakeAmazon(catalog, **(amazon_kw or {}))
        discord = FakeDiscord(ok=discord_ok)
        tracker = ReleaseTracker(cfg, amazon=amazon, discord=discord, store=StateStore(cfg.state_path))
        return tracker, amazon, discord


# --------------------------------------------------------------------- models

class TestModels(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_release_type("Song", 1, 200), "Single")
        self.assertEqual(classify_release_type("Song", 3, 600), "Single")
        self.assertEqual(classify_release_type("Mini", 5, 1200), "EP")
        self.assertEqual(classify_release_type("Thing - EP", 2, 400), "EP")
        self.assertEqual(classify_release_type("Long one", 2, 40 * 60), "Album")
        self.assertEqual(classify_release_type("Big", 12, 3000), "Album")
        self.assertEqual(classify_release_type("Unknown length", 2, 0), "Single")

    def test_dates(self):
        today = date(2026, 10, 2)
        self.assertEqual(parse_release_date("2019-09-11T10:00Z"), date(2019, 9, 11))
        self.assertIsNone(parse_release_date("garbage"))
        self.assertIsNone(parse_release_date(""))
        self.assertTrue(is_recent_release("2026-09-25", 7, today))
        self.assertFalse(is_recent_release("2026-09-24", 7, today))
        self.assertTrue(is_recent_release("2026-10-09", 7, today))
        self.assertTrue(is_upcoming("2026-10-03", today))
        self.assertFalse(is_upcoming("2026-10-02", today))

    def test_nz_today_is_ahead_of_utc(self):
        # 2026-10-01 12:30 UTC = 2026-10-02 01:30 NZDT (00:30 on the UTC+12 fallback)
        self.assertEqual(nz_today(datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)), date(2026, 10, 2))

    def test_artwork_size_suffix_removed(self):
        self.assertEqual(full_size_artwork("https://m.media-amazon.com/images/I/51Ux7-82apL._SX354_SY354_BL0_QL100_.jpg"),
                         "https://m.media-amazon.com/images/I/51Ux7-82apL.jpg")
        self.assertEqual(full_size_artwork(""), "")

    def test_details(self):
        r = Release.from_listing(ListedRelease("ALB", "Hello", "X", ""), A1)
        r.apply_album_details(AlbumDetails(release_date="2026-10-02", track_count=5, duration_seconds=900,
                                           is_explicit=True, copyright="℗ y"))
        self.assertEqual(r.release_type, "EP")
        self.assertTrue(r.is_explicit)
        self.assertEqual(r.release_url, "https://music.amazon.com.au/albums/ALB")
        self.assertEqual(r.artist_url, f"https://music.amazon.com.au/artists/{A1}")


# --------------------------------------------------------------------- config

class TestConfig(unittest.TestCase):
    def test_targets(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "artists.txt"
            p.write_text(f"# comment\n{A1}  # name\nhttps://music.amazon.com.au/artists/{A2}/some-name\n"
                         f"# CHECK {A3}\n{A1}\nnot-an-id\n", encoding="utf-8")
            with patch.dict("os.environ", {"ARTISTS_FILE": str(p), "ARTIST_IDS": f"{A2}, {A3}"}, clear=True):
                self.assertEqual(Config().get_artist_targets(), [A1, A2, A3])


# ---------------------------------------------------------------------- state

class TestStateStore(TrackerTestCase):
    def test_roundtrip_and_dirty(self):
        s = StateStore(self.state_path)
        self.assertFalse(s.save())
        s.mark_seen(Release.from_listing(ListedRelease("ALB", "Hello", "X", ""), A1))
        s.mark_artist_baselined(A1, "X")
        self.assertTrue(s.save())
        s2 = StateStore(self.state_path)
        self.assertTrue(s2.is_seen("ALB"))
        self.assertTrue(s2.is_baselined(A1))
        self.assertEqual(s2.releases["ALB"]["release_type"], "")  # no details yet

    def test_corrupt_file_refuses(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            StateStore(self.state_path)


# -------------------------------------------------------------------- tracker

class TestTracker(TrackerTestCase):
    def test_first_run_baselines_silently_without_detail_requests(self):
        catalog = {A1: [rel("old", "Old", days_ago(1))]}
        tracker, amazon, discord = self.make(catalog, [A1])
        s = tracker.run()
        self.assertEqual(s.artists_baselined, 1)
        self.assertEqual(discord.sent, [])
        self.assertEqual(amazon.album_calls, [])
        self.assertTrue(StateStore(self.state_path).is_seen("old"))

        catalog[A1].insert(0, rel("new", "New", days_ago(0)))
        tracker, amazon, discord = self.make(catalog, [A1])
        s = tracker.run()
        self.assertEqual([r.release_id for r in discord.sent], ["new"])
        self.assertEqual(amazon.album_calls, ["new"])  # only the unseen one
        self.assertEqual(discord.sent[0].copyright, "℗ 2026 Label")
        self.assertTrue(StateStore(self.state_path).is_seen("new"))

    def test_new_artist_is_baselined_while_known_artist_alerts(self):
        catalog = {A1: [], A2: [rel("b_old", "B", days_ago(0))]}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("a_new", "A", days_ago(0))]
        tracker, _, discord = self.make(catalog, [A1, A2])
        s = tracker.run()
        self.assertEqual([r.release_id for r in discord.sent], ["a_new"])
        self.assertEqual(s.artists_baselined, 1)
        self.assertTrue(StateStore(self.state_path).is_seen("b_old"))

    def test_collab_with_new_artist_still_alerts(self):
        self.make({A1: []}, [A1])[0].run()
        collab = rel("collab", "Duet", days_ago(0))
        tracker, _, discord = self.make({A2: [collab], A1: [collab]}, [A2, A1])  # new artist listed first
        tracker.run()
        self.assertEqual([r.release_id for r in discord.sent], ["collab"])

    def test_old_unseen_release_is_cached_not_alerted(self):
        catalog = {A1: []}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("ancient", "Old", "2019-01-01")]
        tracker, _, discord = self.make(catalog, [A1])
        tracker.run()
        self.assertEqual(discord.sent, [])
        self.assertTrue(StateStore(self.state_path).is_seen("ancient"))

    def test_missing_date_alerts_rather_than_hides(self):
        catalog = {A1: []}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("nodate", "Mystery", "")]
        tracker, _, discord = self.make(catalog, [A1])
        tracker.run()
        self.assertEqual([r.release_id for r in discord.sent], ["nodate"])

    def test_failed_delivery_is_not_marked_seen(self):
        catalog = {A1: []}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("new", "New", days_ago(0))]
        tracker, _, discord = self.make(catalog, [A1], discord_ok=False)
        s = tracker.run()
        self.assertEqual(s.undelivered, 1)
        self.assertFalse(StateStore(self.state_path).is_seen("new"))
        tracker, _, discord = self.make(catalog, [A1])  # retried next run
        tracker.run()
        self.assertEqual([r.release_id for r in discord.sent], ["new"])

    def test_dry_run_writes_nothing(self):
        tracker, _, discord = self.make({A1: [rel("x", "X", days_ago(0))]}, [A1], dry_run=True)
        tracker.run()
        self.assertFalse(self.state_path.exists())

    def test_silent_marks_seen_without_posting(self):
        catalog = {A1: []}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("new", "New", days_ago(0))]
        tracker, _, discord = self.make(catalog, [A1], silent=True)
        s = tracker.run()
        self.assertEqual(discord.sent, [])
        self.assertEqual(len(s.new_releases), 1)
        self.assertTrue(StateStore(self.state_path).is_seen("new"))

    def test_explicit_and_clean_versions_alert_once(self):
        catalog = {A1: []}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel("explicit", "Song", days_ago(0)), rel("clean", "Song", days_ago(0))]
        tracker, amazon, discord = self.make(catalog, [A1], amazon_kw={"explicit": {"explicit"}})
        tracker.run()
        self.assertEqual(len(discord.sent), 1)
        self.assertTrue(discord.sent[0].is_explicit)
        store = StateStore(self.state_path)
        self.assertTrue(store.is_seen("explicit") and store.is_seen("clean"))

    def test_pages_further_when_whole_page_is_new(self):
        catalog = {A1: [rel("s0", "Old", "2020-01-01")]}
        self.make(catalog, [A1])[0].run()
        catalog[A1] = [rel(f"n{i}", f"N{i}", days_ago(0)) for i in range(22)] + catalog[A1]
        tracker, amazon, discord = self.make(catalog, [A1])
        tracker.run()
        self.assertIn((A1, 20), amazon.list_calls)
        self.assertEqual(len(discord.sent), 22)

    def test_no_extra_pages_on_baseline(self):
        catalog = {A1: [rel(f"n{i}", f"N{i}", "2020-01-01") for i in range(25)]}
        tracker, amazon, _ = self.make(catalog, [A1])
        tracker.run()
        self.assertNotIn((A1, 20), amazon.list_calls)

    def test_unavailable_artist_counts_as_failure_and_warns_once(self):
        tracker, _, discord = self.make({A1: []}, [A1, A2], amazon_kw={"unavailable": {A2}})
        s = tracker.run()
        self.assertEqual(s.artists_failed, 1)
        self.assertEqual(len(discord.warnings), 1)
        self.assertFalse(StateStore(self.state_path).is_baselined(A2))
        tracker, _, discord = self.make({A1: []}, [A1, A2], amazon_kw={"unavailable": {A2}})
        tracker.run()
        self.assertEqual(discord.warnings, [])  # cooldown

    def test_no_warning_below_threshold(self):
        artists = [A1, A2, A3] + [f"B{i:09d}" for i in range(10, 18)]
        tracker, _, discord = self.make({}, artists, amazon_kw={"fail": {A1}})
        tracker.run()
        self.assertEqual(discord.warnings, [])

    def test_stuck_request_does_not_hold_up_the_run(self):
        release = threading.Event()
        self.addCleanup(release.set)
        catalog = {A1: [rel("a", "A", "2020-01-01")], A2: [], A3: [rel("c", "C", "2020-01-01")]}
        tracker, _, _ = self.make(catalog, [A1, A2, A3], amazon_kw={"hang": {A2}, "release": release},
                                  run_deadline_seconds=0.3, deadline_grace_seconds=0.2)
        started = time.monotonic()
        s = tracker.run()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(s.artists_failed, 1)
        store = StateStore(self.state_path)  # the others were saved
        self.assertTrue(store.is_baselined(A1) and store.is_baselined(A3))
        self.assertFalse(store.is_baselined(A2))

    def test_block_fails_remaining_artists_and_flags_run(self):
        artists = [A1, A2, A3]
        tracker, _, discord = self.make({}, artists, amazon_kw={"block_after": 1}, concurrency=1)
        s = tracker.run()
        self.assertTrue(s.blocked)
        self.assertEqual(s.artists_failed, 2)
        store = StateStore(self.state_path)
        self.assertTrue(store.is_baselined(A1))
        self.assertFalse(store.is_baselined(A2))


# ----------------------------------------------------------------- amazon api

def http_error(code: int, body: bytes = b"{}"):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))


class FakeResponse(io.BytesIO):
    status = 200

    def __init__(self, data: bytes, gz: bool = False):
        super().__init__(gzip.compress(data) if gz else data)
        self.headers = {"Content-Encoding": "gzip"} if gz else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


CONFIG = json.dumps({"deviceId": "d", "sessionId": "s", "csrf": {"token": "t", "ts": "1", "rnd": "2"},
                     "version": "1.0", "siteRegion": "FE", "displayLanguage": "en_AU",
                     "musicTerritory": "AU"}).encode()


def list_page(items: list[tuple[str, str]], next_url: str = "", dialog: bool = False) -> bytes:
    if dialog:
        return json.dumps({"methods": [{"template": {
            "interface": "Web.TemplatesInterface.v1_0.Touch.DialogTemplateInterface.DialogTemplate",
            "header": "Service error"}}]}).encode()
    widget = {"items": [{"primaryText": {"text": t}, "secondaryText": "Artist", "image": "img",
                         "primaryLink": {"deeplink": f"/albums/{a}"}} for a, t in items],
              "onEndOfWidget": [{"url": next_url}] if next_url else []}
    return json.dumps({"methods": [{"template": {"interface": "GalleryTemplate", "widgets": [widget]}}]}).encode()


class TestAmazonClient(unittest.TestCase):
    def client(self):
        return AmazonMusicClient(requests_per_second=0)

    def test_list_and_pagination(self):
        page1 = list_page([("B0AAAAAAAA", "One")], next_url="https://fe.mesk.skill.music.a2z.com/api/next?x=1")
        page2 = json.dumps({"methods": [{"items": [{"primaryText": {"text": "Two"},
                                                    "primaryLink": {"deeplink": "/albums/B0BBBBBBBB"}}],
                                         "onEndOfWidget": []}]}).encode()
        seen = []

        def fake(req, timeout=None):
            seen.append(req.full_url)
            return [FakeResponse(CONFIG), FakeResponse(page1, gz=True), FakeResponse(page2)][len(seen) - 1]

        with patch("urllib.request.urlopen", side_effect=fake):
            c = self.client()
            items, nxt = c.artist_releases(A1)
            self.assertEqual([(i.album_id, i.title) for i in items], [("B0AAAAAAAA", "One")])
            items2, nxt2 = c.artist_releases(A1, nxt)
        self.assertEqual(items2[0].album_id, "B0BBBBBBBB")
        self.assertEqual(nxt2, "")
        self.assertEqual(seen[2], "https://fe.mesk.skill.music.a2z.com/api/next?x=1")

    def test_service_error_dialog_is_artist_unavailable(self):
        with patch("urllib.request.urlopen", side_effect=[FakeResponse(CONFIG), FakeResponse(list_page([], dialog=True))]):
            with self.assertRaises(ArtistUnavailable):
                self.client().artist_releases(A1)

    def test_429_blocks_rest_of_run(self):
        with patch("urllib.request.urlopen", side_effect=[FakeResponse(CONFIG), http_error(429)]) as m:
            c = self.client()
            with self.assertRaises(AmazonBlocked):
                c.artist_releases(A1)
            with self.assertRaises(AmazonBlocked):
                c.artist_releases(A2)  # fails fast
            self.assertEqual(m.call_count, 2)
            self.assertTrue(c.blocked)

    def test_html_reply_blocks(self):
        with patch("urllib.request.urlopen", side_effect=[FakeResponse(CONFIG), FakeResponse(b"<html>captcha</html>")]):
            with self.assertRaises(AmazonBlocked):
                self.client().artist_releases(A1)

    @patch("amazon_api.time.sleep")
    def test_5xx_is_retried(self, _sleep):
        responses = [FakeResponse(CONFIG), http_error(503), FakeResponse(list_page([("B0AAAAAAAA", "One")]))]
        with patch("urllib.request.urlopen", side_effect=responses):
            items, _ = self.client().artist_releases(A1)
        self.assertEqual(len(items), 1)

    def test_headers_travel_inside_body(self):
        captured = []

        def fake(req, timeout=None):
            captured.append(req)
            return FakeResponse(CONFIG) if len(captured) == 1 else FakeResponse(list_page([]))

        with patch("urllib.request.urlopen", side_effect=fake):
            self.client().artist_releases(A1)
        body = json.loads(captured[1].data)
        inner = json.loads(body["headers"])
        self.assertEqual(inner["x-amzn-session-id"], "s")
        self.assertIn(A1, captured[1].full_url)

    def test_slow_body_hits_read_deadline(self):
        ticks = iter(range(100))
        slow = io.BytesIO(b"x" * 300_000)  # five 64 KB chunks, one clock tick each
        with self.assertRaises(TimeoutError):
            read_with_deadline(slow, deadline=3, clock=lambda: next(ticks))
        self.assertEqual(read_with_deadline(io.BytesIO(b"ok"), deadline=3, clock=lambda: 0), b"ok")

    def test_parse_album(self):
        ld = {"@type": "MusicAlbum", "name": "#Love", "byArtist": {"name": "Supercell"},
              "datePublished": "2019-09-11T10:00Z", "numTracks": 4, "timeRequired": "PT14M46S",
              "track": [{"isFamilyFriendly": True}, {"isFamilyFriendly": False}]}
        template = {"headerText": {"text": "#Love"}, "footer": "(P)2019 Sony", "headerImage": "big.jpg",
                    "templateData": {"seoHead": {"script": [{"innerHTML": json.dumps(ld)}]}}}
        d = parse_album(template)
        self.assertEqual((d.release_date, d.track_count, d.duration_seconds), ("2019-09-11", 4, 886))
        self.assertTrue(d.is_explicit)
        self.assertEqual(d.copyright, "(P)2019 Sony")
        self.assertEqual(parse_duration("PT1H2M3S"), 3723)
        self.assertEqual(parse_duration("garbage"), 0)


# -------------------------------------------------------------------- discord

class TestDiscord(unittest.TestCase):
    def test_payload_links_to_amazon(self):
        r = Release.from_listing(ListedRelease("ALB", "Hello", "X", ""), A1)
        r.release_date = (nz_today() + timedelta(days=1)).isoformat()
        r.is_explicit = True
        p = DiscordClient("https://discord.com/api/webhooks/1/abc").build_release_payload(r)
        embed = p["embeds"][0]
        self.assertEqual(embed["url"], "https://music.amazon.com.au/albums/ALB")
        self.assertIn("🅴", embed["title"])
        self.assertIn("Upcoming", p["content"])
        self.assertTrue(any("music.amazon.com.au" in f["value"] for f in embed["fields"]))
        self.assertEqual(p["allowed_mentions"], {"parse": []})

    def test_unconfigured_does_not_send(self):
        r = Release.from_listing(ListedRelease("ALB", "Hello", "X", ""), A1)
        self.assertFalse(DiscordClient("").send_release_notification(r))

    @patch("discord_client.time.sleep")
    def test_429_then_ok(self, _sleep):
        responses = [http_error(429, body=b'{"retry_after": 0.5}'), FakeResponse(b"")]
        with patch("urllib.request.urlopen", side_effect=responses):
            self.assertTrue(DiscordClient("https://discord.com/api/webhooks/1/abc").send_warning("t", "d"))


if __name__ == "__main__":
    unittest.main()
