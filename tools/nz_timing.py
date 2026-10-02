#!/usr/bin/env python3
"""
Watch when new releases appear on music.amazon.com.au (guest session, no login).

Takes a baseline of every artist's newest releases, then a snapshot at each given UTC
time, and reports the releases that appeared since the previous snapshot, with Amazon's
release date and track count. Run it across NZ midnight and AU midnight to see which
one guest sessions follow.

On 2026-10-06/07 (NZDT +13, AEDT +11): NZ midnight = 11:00 UTC, Sydney midnight = 13:00 UTC.

Usage (start it 15-20 minutes before the first time; leave the PC on):
  python tools/nz_timing.py --at 11:02 11:32 12:02 12:32 13:02 13:32

Results are printed and appended to nz_timing_log.jsonl.
"""

from __future__ import annotations
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from match_artists import ROOT, Amazon, AmazonError, _text  # noqa: E402

LIST = "showCatalogMusicItems/uri:_SLASH__SLASH_artist_SLASH_{}_SLASH_chronological-albums"


def read_artists(path: Path) -> list[tuple[str, str]]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\s*(?:\S*/artists/)?([A-Z0-9]{10})\b(?:.*#\s*(.*))?", line)
        if m and not line.lstrip().startswith("#"):
            out.append((m.group(1), (m.group(2) or "").strip()))
    return out


def snapshot(amazon: Amazon, artists: list[tuple[str, str]]) -> tuple[dict[str, dict], int]:
    """{album_asin: {title, artist}} from page 1 of each artist's list; also the failure count."""
    found: dict[str, dict] = {}
    failed = 0
    for asin, name in artists:
        data = amazon._api(LIST.format(asin), {})
        widgets = amazon._widgets(data)
        if widgets is None:
            failed += 1
            continue
        for w in widgets:
            for it in w.get("items", []):
                m = re.match(r"/albums/([A-Z0-9]{10})", (it.get("primaryLink") or {}).get("deeplink", ""))
                if m and m.group(1) not in found:
                    found[m.group(1)] = {"title": _text(it.get("primaryText")), "artist": name or asin}
    return found, failed


def details(amazon: Amazon, album: str) -> dict:
    raw = json.dumps(amazon._api("showCatalogAlbum", {"id": album}))
    date = re.search(r'datePublished\\*"\s*:\s*\\*"([^"\\]+)', raw)
    tracks = re.search(r'numTracks\\*"\s*:\s*\\*"?(\d+)', raw)
    return {"date": date.group(1) if date else None, "tracks": int(tracks.group(1)) if tracks else None}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Log when releases appear on Amazon Music AU")
    p.add_argument("--at", nargs="+", required=True, help="UTC snapshot times, HH:MM")
    p.add_argument("--date", help="UTC date of the times, YYYY-MM-DD (default: today, UTC)")
    p.add_argument("--artists", default=str(ROOT / "artists.txt"))
    p.add_argument("--log", default=str(ROOT / "nz_timing_log.jsonl"))
    p.add_argument("--requests-per-second", type=float, default=1.5)
    args = p.parse_args(argv)

    day = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else datetime.now(timezone.utc).date()
    times = sorted(datetime.combine(day, datetime.strptime(t, "%H:%M").time(), timezone.utc) for t in args.at)
    artists = read_artists(Path(args.artists))
    log = open(args.log, "a", encoding="utf-8")
    print(f"{len(artists)} artists; snapshots at {', '.join(t.strftime('%H:%M') for t in times)} UTC on {day}")

    def stamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        start = time.time()
        known, failed = snapshot(Amazon(args.requests_per_second), artists)
        print(f"[{stamp()}] baseline: {len(known)} releases, {failed} artists failed, {time.time() - start:.0f}s")
        log.write(json.dumps({"at": stamp(), "kind": "baseline", "releases": len(known), "failed": failed}) + "\n")
        log.flush()
        for when in times:
            wait = (when - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                print(f"  waiting until {when.strftime('%H:%M')} UTC ({wait / 60:.0f} min)...", flush=True)
                time.sleep(wait)
            started = stamp()
            amazon = Amazon(args.requests_per_second)  # fresh session each time
            seen, failed = snapshot(amazon, artists)
            new = {a: v for a, v in seen.items() if a not in known}
            print(f"[{started}] snapshot: {len(new)} new, {failed} artists failed", flush=True)
            for album, v in new.items():
                d = details(amazon, album)
                print(f"    {v['artist']} - {v['title']}  date={d['date']} tracks={d['tracks']}  "
                      f"https://music.amazon.com.au/albums/{album}", flush=True)
                log.write(json.dumps({"at": started, "kind": "new", "album": album, **v, **d},
                                     ensure_ascii=False) + "\n")
            log.write(json.dumps({"at": started, "kind": "snapshot", "new": len(new), "failed": failed}) + "\n")
            log.flush()
            known.update(seen)
    except AmazonError as e:
        print(f"Stopped: {e}")
        return 1
    except KeyboardInterrupt:
        print("Stopped by user.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
