"""
Discord webhook delivery: release embeds, warnings, and a connectivity test.
"""

from __future__ import annotations
import json
import logging
import time
from typing import Optional
import urllib.error
import urllib.request

from models import Release, is_upcoming, parse_release_date

logger = logging.getLogger("tracker.discord")

AMAZON_TEAL = 0x25D1DA
WARNING_ORANGE = 0xF0A030

RELEASE_EMOJIS = {
    "Single": "🎵",
    "EP": "🎶",
    "Album": "💿",
}


class DiscordClient:
    def __init__(self, webhook_url: str, username: str = "New Release Tracker", avatar_url: Optional[str] = None):
        self.webhook_url = (webhook_url or "").strip()
        self.username = username
        self.avatar_url = avatar_url

    def is_configured(self) -> bool:
        return self.webhook_url.startswith("https://")

    # --------------------------------------------------------------- public

    def send_release_notification(self, release: Release) -> bool:
        if not self.is_configured():
            logger.warning("DISCORD_WEBHOOK_URL is not configured; skipping notification.")
            return False
        return self._post(self.build_release_payload(release))

    def send_warning(self, title: str, description: str) -> bool:
        if not self.is_configured():
            return False
        return self._post(self._wrap({
            "title": f"⚠️ {title}",
            "description": description[:4000],
            "color": WARNING_ORANGE,
            "timestamp": _utc_now(),
        }))

    def send_test_message(self) -> bool:
        if not self.is_configured():
            logger.error("Cannot send test message: DISCORD_WEBHOOK_URL is empty or invalid.")
            return False
        return self._post(self._wrap({
            "title": "Release tracker connected",
            "description": "Watching Amazon Music (AU) for new releases.",
            "color": AMAZON_TEAL,
            "footer": {"text": "Data from Amazon Music"},
            "timestamp": _utc_now(),
        }))

    # ------------------------------------------------------------- payloads

    def build_release_payload(self, release: Release) -> dict:
        emoji = RELEASE_EMOJIS.get(release.release_type, "🎵")
        upcoming = is_upcoming(release.release_date)

        title = f"{emoji} {release.release_name}"
        if release.is_explicit:
            title += " 🅴"
        title = title[:256]

        date_value = _format_date(release.release_date)
        if upcoming:
            date_value += " · **Upcoming**"

        tracks = release.track_count
        fields = [
            {"name": "Type", "value": f"**{release.release_type}**", "inline": True},
            {"name": "Tracks", "value": f"{tracks} {'track' if tracks == 1 else 'tracks'}" if tracks else "?", "inline": True},
            {"name": "Release date", "value": date_value, "inline": True},
        ]
        if release.copyright:
            fields.append({"name": "Copyright", "value": release.copyright[:200], "inline": False})
        fields.append({
            "name": "Listen",
            "value": f"[▶️ Open on Amazon Music]({release.release_url})",
            "inline": False,
        })

        embed = {
            "title": title,
            "url": release.release_url,
            "color": AMAZON_TEAL,
            "author": {"name": release.artist_name[:256], "url": release.artist_url},
            "fields": fields,
            "footer": {"text": f"Data from Amazon Music · AU · {release.release_id}"},
            "timestamp": _utc_now(),
        }
        if release.artwork_url:
            embed["thumbnail"] = {"url": release.artwork_url}
            embed["image"] = {"url": release.artwork_url}

        label = "Upcoming " + release.release_type.lower() if upcoming else "New " + release.release_type.lower()
        content = f"🚨 **{label}** from **{release.artist_name}**: **{release.release_name}**"
        return self._wrap(embed, content=content[:2000])

    def _wrap(self, embed: dict, content: Optional[str] = None) -> dict:
        payload: dict = {"username": self.username, "embeds": [embed], "allowed_mentions": {"parse": []}}
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        if content:
            payload["content"] = content
        return payload

    # ------------------------------------------------------------- delivery

    def _post(self, payload: dict, max_retries: int = 4) -> bool:
        """True only when Discord confirms delivery (2xx)."""
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "release-tracker/1.0"}

        for attempt in range(1, max_retries + 1):
            req = urllib.request.Request(self.webhook_url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    if 200 <= resp.status < 300:
                        _respect_bucket(resp.headers)
                        return True
                    logger.warning(f"Discord responded with unexpected status {resp.status}")
                    return False
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    retry_after = 2.0
                    try:
                        retry_after = float(json.loads(e.read().decode("utf-8")).get("retry_after", 2.0))
                    except Exception:
                        pass
                    logger.warning(f"Discord rate limit; retrying in {retry_after:.1f}s ({attempt}/{max_retries})")
                    time.sleep(min(retry_after, 30.0) + 0.25)
                    continue
                if 500 <= e.code < 600 and attempt < max_retries:
                    time.sleep(attempt * 1.5)
                    continue
                logger.error(f"Discord webhook error {e.code}: {e.reason}")
                return False
            except Exception as e:
                logger.error(f"Network error posting to Discord: {e}")
                if attempt < max_retries:
                    time.sleep(attempt * 1.5)
                    continue
                return False
        return False


def _respect_bucket(headers) -> None:
    """If this was the last request in Discord's bucket, wait for the reset before returning.
    Must never raise: it runs after a confirmed delivery, and an error here would cause a re-post."""
    try:
        if headers.get("X-RateLimit-Remaining") == "0":
            time.sleep(min(float(headers.get("X-RateLimit-Reset-After", "1")), 10.0))
    except Exception:
        pass


def _format_date(release_date: str) -> str:
    parsed = parse_release_date(release_date)
    if not parsed:
        return release_date or "Unknown"
    return parsed.strftime("%a %d %b %Y")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
