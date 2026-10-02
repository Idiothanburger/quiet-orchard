#!/usr/bin/env python3
"""
Match the Apple Music tool's artists to Amazon Music artist ASINs, for review.

For each Apple artist ID in the Apple tool's artists.txt:
  1. Get the artist's name from Apple's public lookup API (falls back to the Apple tool's state).
  2. Search Amazon Music (music.amazon.com.au, guest session) and take the "Artists" results.
  3. For the closest-named candidates, compare their newest Amazon releases with the
     artist's Apple release titles (Apple's lookup API). Matching titles = same artist.
  4. If that fails (often Japanese-script names, which Amazon AU lists romanised), search
     Amazon for the artist's release titles instead and verify the album's main artist.

Writes:
  artists.txt          confident matches active; unsure ones commented out as "# CHECK ..."
  artist_matches.csv   every artist with candidates and scores (opens in Excel)
  match_cache.json     raw per-artist results, so a rerun skips artists already done

Amazon's web API is unofficial. Requests are paced (default 1 per second) and the run
stops at the first 429 or unexpected response; rerun later to continue from the cache.

Usage:
  python tools/match_artists.py --limit 15      # trial run
  python tools/match_artists.py                 # everything
  python tools/match_artists.py --retry-unsure  # search again for cached artists that aren't OK
"""

from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, field
from difflib import SequenceMatcher
import gzip
import json
from pathlib import Path
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_APPLE_DIR = r"C:\Users\user\.gemini\antigravity\scratch\apple_music_notifier"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
WIKIDATA = "https://query.wikidata.org/sparql"
DOMAIN = "music.amazon.com.au"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
CANDIDATES_TO_VERIFY = 2

_SUFFIX = re.compile(r"\s*-\s*(single|ep)\s*$", re.IGNORECASE)
_FEAT = re.compile(r"[(\[]\s*(feat\.?|ft\.?|featuring|with)\b[^)\]]*[)\]]", re.IGNORECASE)
_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_name(name: str) -> str:
    return _NON_WORD.sub("", unicodedata.normalize("NFKC", name or "").casefold())


def normalize_title(title: str) -> str:
    title = unicodedata.normalize("NFKC", title or "")
    title = _SUFFIX.sub("", title)
    title = _FEAT.sub("", title)
    return _NON_WORD.sub("", title.casefold())


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return round(SequenceMatcher(None, na, nb).ratio(), 2)


class AmazonError(Exception):
    """Blocked or unexpected response: stop the run."""


# ------------------------------------------------------------- Amazon side

class Amazon:
    def __init__(self, requests_per_second: float):
        self.gap = 1.0 / requests_per_second
        self.last = 0.0
        self.count = 0
        self.cfg = json.loads(self._post(f"https://{DOMAIN}/config.json", {}))

    def _post(self, url: str, body: dict) -> str:
        wait = self.last + self.gap - time.time()
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.count += 1
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "text/plain;charset=UTF-8", "User-Agent": UA,
            "Accept-Encoding": "gzip", "Origin": f"https://{DOMAIN}", "Referer": f"https://{DOMAIN}/",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, enc = r.read(), r.headers.get("Content-Encoding")
        except urllib.error.HTTPError as e:
            raise AmazonError(f"HTTP {e.code} from {url.rsplit('/', 1)[-1][:60]}") from e
        except OSError as e:
            raise AmazonError(f"network error: {e}") from e
        if enc == "gzip":
            raw = gzip.decompress(raw)
        return raw.decode("utf-8", "replace")

    def _api(self, path: str, extra: dict) -> dict:
        c = self.cfg
        headers = {
            "x-amzn-authentication": json.dumps({
                "interface": "ClientAuthenticationInterface.v1_0.ClientTokenElement", "accessToken": ""}),
            "x-amzn-device-model": "WEBPLAYER", "x-amzn-device-width": "1920",
            "x-amzn-device-family": "WebPlayer", "x-amzn-device-id": c["deviceId"],
            "x-amzn-user-agent": UA, "x-amzn-session-id": c["sessionId"],
            "x-amzn-device-height": "1080", "x-amzn-request-id": str(uuid.uuid4()),
            "x-amzn-device-language": c["displayLanguage"], "x-amzn-currency-of-preference": "AUD",
            "x-amzn-os-version": "1.0", "x-amzn-application-version": c["version"],
            "x-amzn-device-time-zone": "Pacific/Auckland", "x-amzn-timestamp": str(int(time.time() * 1000)),
            "x-amzn-csrf": json.dumps({
                "interface": "CSRFInterface.v1_0.CSRFHeaderElement", "token": c["csrf"]["token"],
                "timestamp": c["csrf"]["ts"], "rndNonce": c["csrf"]["rnd"]}),
            "x-amzn-music-domain": DOMAIN, "x-amzn-referer": DOMAIN, "x-amzn-affiliate-tags": "",
            "x-amzn-ref-marker": "", "x-amzn-page-url": f"https://{DOMAIN}/",
            "x-amzn-weblab-id-overrides": "", "x-amzn-video-player-token": "",
            "x-amzn-feature-flags": "hd-supported,uhd-supported", "x-amzn-has-profile-id": "",
            "x-amzn-age-band": "",
        }
        body = {**extra, "userHash": json.dumps({"level": "LIBRARY_MEMBER"}), "headers": json.dumps(headers)}
        txt = self._post(f"https://{c['siteRegion'].lower()}.mesk.skill.music.a2z.com/api/{path}", body)
        try:
            return json.loads(txt)
        except ValueError:
            raise AmazonError(f"non-JSON response from {path[:60]}: {txt[:200]!r}")

    @staticmethod
    def _widgets(data: dict) -> list[dict] | None:
        """None for the 'Service error' dialog (artist page or search not available)."""
        template = data["methods"][0].get("template", {})
        if "DialogTemplate" in template.get("interface", ""):
            return None
        return template.get("widgets", [])

    def _search(self, keyword: str) -> list[dict]:
        data = self._api("showSearch", {
            "filter": json.dumps({"IsLibrary": ["false"]}),
            "keyword": json.dumps({"interface": "Web.SearchInterface.v1_0.SearchKeywordClientInformation",
                                   "keyword": ""}),
            "suggestedKeyword": keyword,
        })
        return self._widgets(data) or []

    def search_artists(self, keyword: str) -> list[tuple[str, str]]:
        """[(asin, name)] from the 'Artists' section of a search."""
        out: list[tuple[str, str]] = []
        for w in self._search(keyword):
            if w.get("header") != "Artists":
                continue
            for it in w.get("items", []):
                m = re.match(r"/artists/([A-Z0-9]{10})", (it.get("primaryLink") or {}).get("deeplink", ""))
                if m:
                    out.append((m.group(1), _text(it.get("primaryText"))))
        return out

    def search_releases(self, keyword: str) -> list[tuple[str, str]]:
        """[(album_asin, title)] from the 'Albums' and 'Songs' sections of a search."""
        out: list[tuple[str, str]] = []
        for w in self._search(keyword):
            if w.get("header") not in ("Albums", "Songs"):
                continue
            for it in w.get("items", []):
                m = re.match(r"/albums/([A-Z0-9]{10})", (it.get("primaryLink") or {}).get("deeplink", ""))
                if m:
                    out.append((m.group(1), _text(it.get("primaryText"))))
        return out

    def album_artist(self, album_asin: str) -> tuple[str, str] | None:
        """(artist_asin, slug) most linked from an album page: its main artist."""
        data = self._api("showCatalogAlbum", {"id": album_asin})
        links = Counter(re.findall(r"/artists/([A-Z0-9]{10})/([^\"\\?]+)", json.dumps(data)))
        if not links:
            return None
        counts = Counter()
        for (asin, _slug), n in links.items():
            counts[asin] += n
        asin = counts.most_common(1)[0][0]
        slug = min((s for a, s in links if a == asin), key=len)
        return asin, slug

    def release_titles(self, asin: str) -> list[str] | None:
        """Newest ~20 release titles, or None if the artist page gives 'Service error'."""
        data = self._api("showCatalogMusicItems/uri:_SLASH__SLASH_artist_SLASH_"
                         f"{asin}_SLASH_chronological-albums", {})
        widgets = self._widgets(data)
        if widgets is None:
            return None
        return [_text(it.get("primaryText")) for w in widgets for it in w.get("items", [])]


def _text(v) -> str:
    return (v.get("text") if isinstance(v, dict) else v) or ""


# --------------------------------------------------------------- Apple side

def read_apple_ids(apple_dir: Path) -> list[str]:
    ids, seen = [], set()
    for line in (apple_dir / "artists.txt").read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        m = re.search(r"/artist/[^/]+/(\d+)", value)
        value = m.group(1) if m else value
        if value.isdigit() and value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def read_apple_state(apple_dir: Path) -> tuple[dict[str, Counter], dict[str, set[str]]]:
    """Per Apple artist ID: release artist-name counts and normalized release titles."""
    names: dict[str, Counter] = defaultdict(Counter)
    titles: dict[str, set[str]] = defaultdict(set)
    path = apple_dir / "seen_releases.json"
    if not path.exists():
        return names, titles
    data = json.loads(path.read_text(encoding="utf-8"))
    # Newer format: {"releases": {...}, "artists": {...}, "meta": {...}}; older: {collection_id: record}.
    records = data["releases"].values() if isinstance(data.get("releases"), dict) else data.values()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        artist_id = str(rec.get("artist_id", ""))
        if rec.get("artist_name"):
            names[artist_id][rec["artist_name"]] += 1
        t = normalize_title(rec.get("collection_name", ""))
        if t:
            titles[artist_id].add(t)
    return names, titles


def lookup_apple_names(ids: list[str], country: str = "nz") -> dict[str, str]:
    """Apple's lookup API accepts many IDs at once and needs no key."""
    found: dict[str, str] = {}
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        url = f"{ITUNES_LOOKUP}?{urllib.parse.urlencode({'id': ','.join(batch), 'country': country})}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "amazon-music-notifier/0.1"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"Apple lookup failed for a batch of {len(batch)}: {e}")
            continue
        for r in data.get("results", []):
            if r.get("wrapperType") == "artist" and r.get("artistName"):
                found[str(r["artistId"])] = r["artistName"]
    return found


def lookup_apple_titles(ids: list[str], country: str = "nz") -> dict[str, list[tuple[str, str]]]:
    """(release date, title) per artist, newest first. The limit applies per artist, and each
    artist's albums follow its own 'artist' row, so collabs are credited to the right ID."""
    titles: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for i in range(0, len(ids), 10):
        batch = ids[i:i + 10]
        query = urllib.parse.urlencode({"id": ",".join(batch), "entity": "album", "limit": 200, "country": country})
        try:
            req = urllib.request.Request(f"{ITUNES_LOOKUP}?{query}", headers={"User-Agent": "amazon-music-notifier/0.1"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"Apple album lookup failed for a batch of {len(batch)}: {e}")
            continue
        current = None
        for r in data.get("results", []):
            if r.get("wrapperType") == "artist":
                current = str(r.get("artistId"))
            elif r.get("wrapperType") == "collection" and current and r.get("collectionName"):
                titles[current].append((r.get("releaseDate", ""), r["collectionName"]))
        time.sleep(3)  # Apple's lookup API allows roughly 20 requests a minute
    for v in titles.values():
        v.sort(reverse=True)
    return titles


def best_state_name(counter: Counter) -> str:
    """Prefer a solo credit over collab strings like 'A & B'."""
    solo = [n for n, _ in counter.most_common() if not re.search(r" & |, | x | feat", n, re.IGNORECASE)]
    if solo:
        return solo[0]
    return counter.most_common(1)[0][0] if counter else ""


def lookup_wikidata(ids: list[str]) -> dict[str, str]:
    """Apple ID -> Amazon ASIN via Wikidata (P2850 -> P6276), as a cross-check only."""
    values = " ".join(f'"{i}"' for i in ids)
    query = f"SELECT ?apple ?amz WHERE {{ VALUES ?apple {{ {values} }} ?item wdt:P2850 ?apple . ?item wdt:P6276 ?amz . }}"
    req = urllib.request.Request(
        WIKIDATA, data=urllib.parse.urlencode({"query": query, "format": "json"}).encode(),
        headers={"User-Agent": "amazon-music-notifier/0.1 (personal project)",
                 "Accept": "application/sparql-results+json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            rows = json.loads(resp.read().decode("utf-8"))["results"]["bindings"]
    except Exception as e:
        print(f"Wikidata lookup failed: {e}")
        return {}
    return {r["apple"]["value"]: r["amz"]["value"] for r in rows}


# ------------------------------------------------------------------ matching

@dataclass
class AppleArtist:
    apple_id: str
    name: str = ""
    titles: set[str] = field(default_factory=set)
    raw_titles: list[str] = field(default_factory=list)  # Apple release titles, newest first


TITLES_TO_SEARCH = 3
_LATIN = re.compile(r"[A-Za-z]")


def titles_to_search(raw: list[str]) -> list[str]:
    """Distinctive recent titles, Latin-script first (Amazon AU romanises Japanese titles)."""
    picked, seen = [], set()
    for t in raw:
        clean = _SUFFIX.sub("", t).strip()
        n = normalize_title(clean)
        if len(n) >= 3 and n not in seen:
            seen.add(n)
            picked.append(clean)
    picked.sort(key=lambda t: not _LATIN.search(t))  # stable: keeps newest first within each group
    return picked[:TITLES_TO_SEARCH]


def add_title_candidates(amazon: Amazon, apple: AppleArtist, cands: list[dict]) -> None:
    """Fallback when the name search fails: search Amazon for the artist's release titles,
    take the artist of a release with the same title, and verify it like the others."""
    tried = {c["asin"] for c in cands}
    for title in titles_to_search(apple.raw_titles):
        want = normalize_title(title)
        album = next((a for a, t in amazon.search_releases(title) if normalize_title(t) == want), None)
        if not album:
            continue
        found = amazon.album_artist(album)
        if not found or found[0] in tried:
            continue
        asin, slug = found
        tried.add(asin)
        slug = urllib.parse.unquote(slug)
        c = {"asin": asin, "name": slug, "name_score": name_similarity(apple.name, slug),
             "title_matches": None, "via": f"title search: {title}"}
        cands.append(c)
        titles = amazon.release_titles(asin)
        c["title_matches"] = "service error" if titles is None else \
            len({normalize_title(t) for t in titles} & apple.titles)
        if isinstance(c["title_matches"], int) and c["title_matches"] >= 2:
            return


def match_artist(amazon: Amazon, apple: AppleArtist) -> dict:
    """Search and verify; returns a JSON-able record for the cache."""
    rec = {"apple_id": apple.apple_id, "apple_name": apple.name, "candidates": []}
    if not apple.name:
        return rec
    found = amazon.search_artists(apple.name)
    if not found:
        simpler = re.sub(r"\s*[(\[].*?[)\]]\s*", " ", apple.name).strip()
        if simpler and simpler != apple.name:
            found = amazon.search_artists(simpler)
    cands = [{"asin": a, "name": n, "name_score": name_similarity(apple.name, n), "title_matches": None}
             for a, n in dict(found).items()]
    cands.sort(key=lambda c: c["name_score"], reverse=True)  # stable: keeps Amazon's order on ties
    for c in cands[:CANDIDATES_TO_VERIFY]:
        if not apple.titles:
            break
        titles = amazon.release_titles(c["asin"])
        if titles is None:
            c["title_matches"] = "service error"
            continue
        c["title_matches"] = len({normalize_title(t) for t in titles} & apple.titles)
        if c["title_matches"] >= 2 and c["name_score"] >= 0.8:
            break  # clearly the one; save requests
    rec["candidates"] = cands
    if judge(rec, bool(apple.titles))[0] != "OK" and apple.raw_titles:
        add_title_candidates(amazon, apple, cands)
    return rec


def judge(rec: dict, has_titles: bool) -> tuple[str, dict | None, str]:
    cands = rec["candidates"]
    if not rec["apple_name"]:
        return "NOT FOUND", None, "no name available for this Apple ID"
    if not cands:
        return "NOT FOUND", None, "no Amazon artist results"

    def tm(c):
        return c["title_matches"] if isinstance(c["title_matches"], int) else 0

    best = max(cands, key=lambda c: (tm(c), c["title_matches"] != "service error", c["name_score"]))
    m = tm(best)
    if best.get("via") and best["name_score"] < 0.8 and m < 5:
        # Title search takes an album's main artist, which for a collab may be the partner.
        return "CHECK", best, f"only {m} matching release title(s), found by {best['via']}"
    if m >= 2 or (m >= 1 and best["name_score"] >= 0.8):
        via = f", found by {best['via']}" if best.get("via") else ""
        return "OK", best, f"{m} matching release title(s){via}"
    if best["name_score"] == 1.0 and not has_titles:
        return "CHECK", best, "exact name, but no Apple releases to compare"
    if best["name_score"] == 1.0:
        return "CHECK", best, "exact name, but no matching release titles"
    return "CHECK", best, "closest name only"


# ------------------------------------------------------------------ output

def amazon_url(asin: str) -> str:
    return f"https://{DOMAIN}/artists/{asin}"


def write_outputs(rows: list[tuple], artists_path: Path, csv_path: Path) -> None:
    lines = [
        "# One Amazon Music artist ASIN per line (or a music.amazon.*/artists/ASIN link).",
        "# Anything after # is a comment.",
        "#",
        "# Generated by tools/match_artists.py from the Apple Music tool's list.",
        "# Lines starting with '# CHECK' are unsure matches: open the link, and if it's right,",
        "# delete the leading '# CHECK ' so the line starts with the ASIN.",
        "",
    ]
    written: set[str] = set()
    for status, rec, best, note, wd in rows:
        if status == "OK" and best["asin"] not in written:
            written.add(best["asin"])
            lines.append(f"{best['asin']}  # {best['name']}")
    lines += ["", "# ---- Needs review ----"]
    for status, rec, best, note, wd in rows:
        if status == "CHECK" and best["asin"] not in written:
            lines.append(f"# CHECK {best['asin']}  # {best['name']}  (Apple: {rec['apple_name']}; {note}) "
                         f"{amazon_url(best['asin'])}")
    for status, rec, best, note, wd in rows:
        if status in ("NOT FOUND", "NOT RUN"):
            lines.append(f"# {status}: {rec['apple_name'] or '?'}  (Apple ID {rec['apple_id']}; {note})")
    artists_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    # utf-8-sig so Excel shows Japanese names correctly.
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "apple_id", "apple_name", "amazon_asin", "amazon_name", "name_score",
                    "title_matches", "note", "wikidata_asin", "wikidata_agrees", "amazon_url", "apple_url",
                    "other_candidates"])
        for status, rec, best, note, wd in rows:
            others = [c for c in rec.get("candidates", []) if c is not best][:4]
            w.writerow([
                status, rec["apple_id"], rec["apple_name"],
                best["asin"] if best else "", best["name"] if best else "",
                best["name_score"] if best else "",
                best["title_matches"] if best and best["title_matches"] is not None else "",
                note, wd or "", ("yes" if best and wd == best["asin"] else "no") if wd else "",
                amazon_url(best["asin"]) if best else "",
                f"https://music.apple.com/nz/artist/{rec['apple_id']}",
                " | ".join(f"{c['name']} ({amazon_url(c['asin'])})" for c in others),
            ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Match Apple Music artists to Amazon Music ASINs for review")
    parser.add_argument("--apple-dir", default=DEFAULT_APPLE_DIR, help="Apple Music tool folder")
    parser.add_argument("--out", default=str(ROOT / "artists.txt"), help="artists file to write")
    parser.add_argument("--csv", default=str(ROOT / "artist_matches.csv"), help="review CSV to write")
    parser.add_argument("--cache", default=str(ROOT / "match_cache.json"), help="per-artist results cache")
    parser.add_argument("--limit", type=int, default=0, help="only match the first N artists (for a trial run)")
    parser.add_argument("--retry-unsure", action="store_true", help="search again for cached artists that aren't OK")
    parser.add_argument("--overwrite", action="store_true", help="replace an artists file that already has IDs")
    parser.add_argument("--requests-per-second", type=float, default=1.0)
    args = parser.parse_args(argv)

    apple_dir, cache_path, out_path = Path(args.apple_dir), Path(args.cache), Path(args.out)
    if out_path.exists() and not args.overwrite and any(
            re.match(r"[A-Z0-9]{10}\b", line) for line in out_path.read_text(encoding="utf-8").splitlines()):
        print(f"{out_path} already has artist IDs (maybe hand-edited). "
              "Use --overwrite to replace it, or --out to write elsewhere.")
        return 1
    ids = read_apple_ids(apple_dir)
    if args.limit:
        ids = ids[:args.limit]
    print(f"{len(ids)} Apple artists. Looking up names...")
    state_names, state_titles = read_apple_state(apple_dir)
    apple_names = lookup_apple_names(ids)
    print("Looking up Apple release titles...")
    apple_titles = lookup_apple_titles(ids)
    wikidata = lookup_wikidata(ids)
    artists = []
    for i in ids:
        raw = [t for _, t in apple_titles.get(i, [])]
        artists.append(AppleArtist(i, apple_names.get(i) or best_state_name(state_names.get(i, Counter())),
                                   state_titles.get(i, set()) | {normalize_title(t) for t in raw}, raw))

    cache: dict[str, dict] = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    if args.retry_unsure:
        for a in artists:
            if a.apple_id in cache and judge(cache[a.apple_id], bool(a.titles))[0] != "OK":
                del cache[a.apple_id]
    todo = [a for a in artists if a.apple_id not in cache]
    print(f"{len(artists) - len(todo)} already in the cache, {len(todo)} to search.")

    amazon = None
    stopped = ""
    for n, apple in enumerate(todo, 1):
        try:
            amazon = amazon or Amazon(args.requests_per_second)
            rec = match_artist(amazon, apple)
        except AmazonError as e:
            stopped = str(e)
            print(f"Stopping: {e}. Rerun later to continue; finished artists are cached.")
            break
        cache[apple.apple_id] = rec
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        status, best, note = judge(rec, bool(apple.titles))
        shown = f"{best['name']} ({best['asin']})" if best else "-"
        print(f"[{n}/{len(todo)}] {status:9} {apple.name or apple.apple_id} -> {shown}  {note}", flush=True)

    rows = []
    for a in artists:
        rec = cache.get(a.apple_id)
        if rec is None:
            rows.append(("NOT RUN", {"apple_id": a.apple_id, "apple_name": a.name}, None,
                         "not searched yet", wikidata.get(a.apple_id)))
            continue
        status, best, note = judge(rec, bool(a.titles))
        rows.append((status, rec, best, note, wikidata.get(a.apple_id)))
    write_outputs(rows, Path(args.out), Path(args.csv))

    counts = Counter(r[0] for r in rows)
    wd_rows = [r for r in rows if r[4] and r[2]]
    agree = sum(1 for r in wd_rows if r[2]["asin"] == r[4])
    print(f"\nWrote {args.out} and {args.csv}")
    print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
          + f"  ({amazon.count if amazon else 0} Amazon requests this run)")
    print(f"Wikidata cross-check: {agree}/{len(wd_rows)} same ASIN (Amazon has alias ASINs, so a difference "
          f"isn't necessarily wrong)")
    return 1 if stopped else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
