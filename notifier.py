#!/usr/bin/env python3
"""
Amazon Music new-release Discord notifier
=========================================
Checks every artist in artists.txt on music.amazon.com.au (guest session, no login)
and posts new releases to a Discord webhook.

Per run:
  1. Scan all artists (newest-first release list, 20 per page), paced by a shared limiter.
  2. Newly added artists are recorded silently (baseline), with no detail requests.
  3. For known artists, each unseen release gets one detail request (date, tracks, explicit).
     Releases from the last 7 days (NZ date) or future-dated are alerted; older ones are
     recorded silently.
  4. A release is marked seen only after Discord confirms delivery.
  5. State is written once, to seen_releases.json, and committed by the workflow.
"""

from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Optional

from amazon_api import AmazonBlocked, AmazonError, AmazonMusicClient
from db import StateStore
from discord_client import DiscordClient
from models import Release, is_recent_release

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logger = logging.getLogger("tracker")

ARTIST_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
ARTIST_REF_RE = re.compile(r"music\.amazon\.[a-z.]+/artists/([A-Z0-9]{10})")


def load_dotenv(path: str = ".env") -> None:
    """Tiny .env loader (stdlib only). Existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


class Config:
    def __init__(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.artists_file = os.getenv("ARTISTS_FILE", "artists.txt").strip()
        self.artist_ids_raw = os.getenv("ARTIST_IDS", "").strip()
        self.domain = os.getenv("AMAZON_DOMAIN", "music.amazon.com.au").strip()
        self.state_path = os.getenv("STATE_PATH", "seen_releases.json").strip()
        self.concurrency = int(os.getenv("CONCURRENCY", "2"))
        self.requests_per_second = float(os.getenv("REQUESTS_PER_SECOND", "2"))
        self.max_release_age_days = int(os.getenv("MAX_RELEASE_AGE_DAYS", "7"))
        self.max_pages = int(os.getenv("MAX_PAGES", "2"))
        self.failure_warn_ratio = float(os.getenv("FAILURE_WARN_RATIO", "0.10"))
        self.warning_cooldown_hours = float(os.getenv("WARNING_COOLDOWN_HOURS", "6"))
        # Stop scanning after this long (a normal run takes ~3 min); unfinished artists are retried
        # next run. Keeps a stuck request from running into the workflow's 15-minute timeout.
        self.run_deadline_seconds = float(os.getenv("RUN_DEADLINE_SECONDS", "600"))
        self.deadline_grace_seconds = 30.0
        self.dry_run = _env_bool("DRY_RUN")
        self.silent = _env_bool("SILENT")
        self.log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    def get_artist_targets(self) -> list[str]:
        """Artist ASINs from artists.txt plus ARTIST_IDS, de-duplicated, order kept."""
        targets: list[str] = []
        seen: set[str] = set()

        def add(raw: str, source: str) -> None:
            value = raw.split("#", 1)[0].strip()
            if not value:
                return
            match = ARTIST_REF_RE.search(value)
            artist_id = match.group(1) if match else value
            if not ARTIST_ID_RE.match(artist_id):
                logger.warning(f"Ignoring unrecognised artist entry in {source}: {value!r}")
                return
            if artist_id not in seen:
                seen.add(artist_id)
                targets.append(artist_id)

        if self.artists_file and Path(self.artists_file).exists():
            for line in Path(self.artists_file).read_text(encoding="utf-8").splitlines():
                add(line, self.artists_file)
        for item in self.artist_ids_raw.split(","):
            add(item, "ARTIST_IDS")
        return targets


@dataclass
class ArtistScan:
    artist_id: str
    artist_name: str = ""
    releases: list[Release] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RunSummary:
    artists_checked: int = 0
    artists_failed: int = 0
    artists_baselined: int = 0
    new_releases: list[str] = field(default_factory=list)
    undelivered: int = 0
    requests: int = 0
    duration_seconds: float = 0.0
    blocked: bool = False
    fatal_error: Optional[str] = None


class ReleaseTracker:
    def __init__(self, config: Config, amazon: Optional[AmazonMusicClient] = None,
                 discord: Optional[DiscordClient] = None, store: Optional[StateStore] = None):
        self.cfg = config
        self.amazon = amazon or AmazonMusicClient(config.domain, requests_per_second=config.requests_per_second)
        self.discord = discord or DiscordClient(config.webhook_url)
        self.store = store or StateStore(config.state_path)

    # ----------------------------------------------------------------- scan

    def scan_artist(self, artist_id: str, force_notify: bool = False, deadline: Optional[float] = None) -> ArtistScan:
        """Fetch an artist's newest releases, plus details for unseen ones. Read-only on state."""
        scan = ArtistScan(artist_id)
        known = self.store.is_baselined(artist_id) or force_notify

        def check_deadline() -> None:
            if deadline is not None and time.monotonic() > deadline:
                raise AmazonError("run deadline reached; retried next run")

        try:
            page_url = ""
            for _page in range(max(1, self.cfg.max_pages)):
                check_deadline()
                listed, page_url = self.amazon.artist_releases(artist_id, page_url)
                page = [Release.from_listing(item, artist_id) for item in listed]
                scan.releases.extend(page)
                # Only dig deeper when a known artist has an entire page of unseen releases
                # (a burst of drops would otherwise push some off page 1).
                whole_page_new = page and all(not self.store.is_seen(r.release_id) for r in page)
                if not (known and whole_page_new and page_url):
                    break
            credits = Counter(r.artist_name for r in scan.releases if r.artist_name != "Unknown Artist")
            scan.artist_name = min(credits, key=lambda n: (-credits[n], len(n))) if credits else ""

            # Dates are only on the album page, so fetch details for unseen releases of known
            # artists. New artists are baselined without them: old releases don't need dates.
            if known:
                for r in scan.releases:
                    if not self.store.is_seen(r.release_id):
                        check_deadline()
                        r.apply_album_details(self.amazon.album(r.release_id))
        except AmazonError as e:  # includes ArtistUnavailable and AmazonBlocked
            scan.error = str(e)
        except Exception as e:  # never let one artist kill the run
            scan.error = f"{type(e).__name__}: {e}"
        return scan

    # ------------------------------------------------------------------ run

    def run(self, force_notify: bool = False) -> RunSummary:
        summary = RunSummary()
        start = time.monotonic()
        targets = self.cfg.get_artist_targets()
        if not targets:
            logger.warning("No artists configured. Add Amazon artist ASINs or links to artists.txt.")
            summary.fatal_error = "no artists configured"
            return summary

        try:
            self.amazon.start_session()
        except AmazonError as e:
            logger.error(f"Couldn't start an Amazon Music session: {e}")
            summary.fatal_error = f"session failed: {e}"
            summary.blocked = isinstance(e, AmazonBlocked)
            self._maybe_warn("Amazon Music session failed", str(e))
            self._save()
            return summary

        mode = "dry run" if self.cfg.dry_run else "silent" if self.cfg.silent else \
            "live" if self.discord.is_configured() else "live, but DISCORD_WEBHOOK_URL is NOT set"
        logger.info(f"Scanning {len(targets)} artists ({self.cfg.concurrency} workers, "
                    f"{self.cfg.requests_per_second:g} req/s cap, {mode})...")

        scans = self._scan_all(targets, force_notify, start + self.cfg.run_deadline_seconds)

        summary.artists_checked = len(targets)
        failed = [s for s in scans if s.error]
        summary.artists_failed = len(failed)
        summary.blocked = self.amazon.blocked
        for s in failed:
            logger.warning(f"Failed: {s.artist_id}: {s.error}")

        to_alert = self._process_scans(scans, force_notify, summary)
        try:
            self._deliver(to_alert, summary)
        finally:
            summary.requests = self.amazon.request_count
            summary.duration_seconds = round(time.monotonic() - start, 1)
            self._check_failures(failed, summary)
            self._save()

        rate = summary.requests / summary.duration_seconds if summary.duration_seconds else 0
        logger.info(
            f"Done in {summary.duration_seconds}s: {summary.artists_checked} artists, "
            f"{summary.artists_failed} failed, {summary.artists_baselined} baselined, "
            f"{len(summary.new_releases)} new, {summary.requests} requests ({rate:.1f}/s)"
            + (", BLOCKED by Amazon" if summary.blocked else "")
        )
        return summary

    def _scan_all(self, targets: list[str], force_notify: bool, deadline: float) -> list[ArtistScan]:
        """Scan in parallel, but never wait past the deadline (plus a grace period for requests
        already in flight). A scan still running then is reported as failed, and its thread is
        abandoned rather than joined; main() exits without waiting for it."""
        pool = ThreadPoolExecutor(max_workers=max(1, self.cfg.concurrency))
        futures = {pool.submit(self.scan_artist, a, force_notify, deadline): a for a in targets}
        done, pending = wait(futures, timeout=max(0.0, deadline - time.monotonic()) + self.cfg.deadline_grace_seconds)
        pool.shutdown(wait=False, cancel_futures=True)
        if pending:
            logger.warning(f"Run deadline reached: {len(pending)} artist checks didn't finish")
        return [f.result() if f in done else ArtistScan(a, error="still running at the run deadline; retried next run")
                for f, a in futures.items()]

    def _process_scans(self, scans: list[ArtistScan], force_notify: bool, summary: RunSummary) -> list[Release]:
        """Baseline new artists, silently cache old releases, and pick the ones to alert."""
        to_alert: dict[tuple, Release] = {}
        queued_ids: set[str] = set()

        known, new = [], []
        for scan in scans:
            if scan.artist_name:
                self.store.set_artist_name(scan.artist_id, scan.artist_name)
            if scan.error:
                continue  # partial data: don't baseline or alert from it
            if self.store.is_baselined(scan.artist_id) or force_notify:
                known.append(scan)
            else:
                new.append(scan)

        # Known artists first, so a collab with a newly added artist still alerts
        # instead of being swallowed by the new artist's silent baseline.
        for scan in known:
            for r in scan.releases:
                if self.store.is_seen(r.release_id) or r.release_id in queued_ids:
                    continue
                # No date means the album page changed shape: alert rather than risk missing it.
                if r.release_date and not is_recent_release(r.release_date, self.cfg.max_release_age_days):
                    self.store.mark_seen(r)  # back catalog: never alert
                    continue
                key = r.dedupe_key()
                if key in to_alert:
                    to_alert[key].duplicate_ids.append(r.release_id)
                else:
                    to_alert[key] = r
                queued_ids.add(r.release_id)

            if force_notify:
                self.store.mark_artist_baselined(scan.artist_id, scan.artist_name)

        for scan in new:
            for r in scan.releases:
                if r.release_id not in queued_ids:
                    self.store.mark_seen(r)
            self.store.mark_artist_baselined(scan.artist_id, scan.artist_name)
            summary.artists_baselined += 1
            logger.info(f"Baselined {scan.artist_name or scan.artist_id}: {len(scan.releases)} releases recorded silently")

        return sorted(to_alert.values(), key=lambda r: (r.release_date, r.artist_name.casefold(), r.release_name))

    def _deliver(self, releases: list[Release], summary: RunSummary) -> None:
        for r in releases:
            label = f"{r.artist_name} - {r.release_name} [{r.release_type}] ({r.published_at or r.release_date})"
            logger.info(f"NEW: {label}")
            summary.new_releases.append(label)

            if self.cfg.dry_run:
                logger.info("  [dry run] not sending, not marking seen")
                continue
            if self.cfg.silent:
                logger.info("  [silent] not sending, marking seen")
            elif not self.discord.send_release_notification(r):
                summary.undelivered += 1
                logger.warning("  Discord delivery failed; will retry next run")
                continue
            self.store.mark_seen(r)
            for dup in r.duplicate_ids:
                self.store.mark_seen(r, release_id=dup)

    def _check_failures(self, failed: list[ArtistScan], summary: RunSummary) -> None:
        if not summary.artists_checked or not failed:
            return
        ratio = len(failed) / summary.artists_checked
        if ratio <= self.cfg.failure_warn_ratio:
            return
        examples = "\n".join(f"• `{s.artist_id}`: {s.error[:150]}" for s in failed[:5])
        self._maybe_warn(
            f"{len(failed)} of {summary.artists_checked} artists failed",
            f"{ratio:.0%} of artist checks failed this run.\n\n{examples}\n\n"
            f"Requests: {summary.requests}. See the GitHub Actions log for details.",
        )

    def _maybe_warn(self, title: str, description: str) -> None:
        """Send at most one warning per cooldown window, so a broken setup doesn't spam."""
        last = self.store.get_meta("last_warning_at")
        now = datetime.now(timezone.utc)
        if last:
            try:
                if now - datetime.fromisoformat(last) < timedelta(hours=self.cfg.warning_cooldown_hours):
                    logger.info(f"Warning suppressed (cooldown): {title}")
                    return
            except ValueError:
                pass
        if self.cfg.dry_run or self.cfg.silent:
            logger.info(f"[{'dry run' if self.cfg.dry_run else 'silent'}] would warn: {title}")
            return
        if self.discord.send_warning(title, description):
            self.store.set_meta("last_warning_at", now.replace(microsecond=0).isoformat())

    def _save(self) -> None:
        if self.cfg.dry_run:
            logger.info("[dry run] state not saved")
            return
        self.store.save()


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Amazon Music new-release Discord notifier")
    parser.add_argument("--dry-run", action="store_true", help="Don't post to Discord or write state")
    parser.add_argument("--silent", action="store_true",
                        help="Don't post to Discord, but record releases as seen (for a trial period)")
    parser.add_argument("--test-discord", action="store_true", help="Send a test message to the webhook and exit")
    parser.add_argument("--force-notify", action="store_true",
                        help="Alert recent releases for new artists too, instead of baselining them silently")
    args = parser.parse_args(argv)

    config = Config()
    config.dry_run = config.dry_run or args.dry_run
    config.silent = config.silent or args.silent
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO), format=LOG_FORMAT)

    if args.test_discord:
        ok = DiscordClient(config.webhook_url).send_test_message()
        print("Test message delivered." if ok else "Test message failed. Check DISCORD_WEBHOOK_URL.")
        return 0 if ok else 1

    summary = ReleaseTracker(config).run(force_notify=args.force_notify)
    if summary.fatal_error or summary.blocked:
        return 1
    if summary.artists_checked and summary.artists_failed == summary.artists_checked:
        return 1
    return 0


if __name__ == "__main__":
    exit_code = main()
    # Exit without joining worker threads: a request stuck past the run deadline would
    # otherwise keep the process alive after state is already saved.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)
