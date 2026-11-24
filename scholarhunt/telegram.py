"""Read public Telegram channels through their web preview (t.me/s/<handle>).

Read only, and no account, token or login: only channels that Telegram itself
shows to anyone on the web can be read. Which channels to read is set in the
private config under `telegram:`. Nothing is ever posted from here.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from jobhunt.http import get
from jobhunt.models import Job, clean_text, parse_date

log = logging.getLogger("scholarhunt")
_HANDLE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_URL = re.compile(r"https?://\S+")
_TELEGRAM_HOSTS = {"t.me", "telegram.me", "telegram.org"}


def _outbound(text_el) -> str:
    """First link that leaves Telegram. It matches the RSS link when a site posts to both."""
    for a in text_el.select("a[href]"):
        u = urlparse(a["href"])
        if u.scheme in ("http", "https") and u.netloc.lower().removeprefix("www.") not in _TELEGRAM_HOSTS:
            return a["href"]
    return ""


def parse(page: str, name: str, since: datetime) -> list[Job]:
    """Text posts newer than `since`. A post with no text (photo only) is skipped."""
    out = []
    for m in BeautifulSoup(page, "html.parser").select(".tgme_widget_message[data-post]"):
        text_el = m.select_one(".tgme_widget_message_text.js-message_text") or m.select_one(".tgme_widget_message_text")
        time_el = m.select_one("time[datetime]")
        posted = parse_date(time_el["datetime"]) if time_el else None
        if not text_el or not posted or posted < since:
            continue
        for br in text_el.select("br"):
            br.replace_with("\n")
        lines = [clean_text(_URL.sub("", s)) for s in text_el.get_text().splitlines()]
        title = next((s for s in lines if s), "")
        link = _outbound(text_el)
        if not title and not link:
            continue
        post = f"https://t.me/{m['data-post']}"
        out.append(Job(source=f"telegram:{name}", company=name, title=(title or link)[:200], location="See listing",
                       url=link or post, description=clean_text(text_el.get_text(), 5000), posted=posted))
    return out


def fetch(tcfg: dict) -> list[Job]:
    since = datetime.now(timezone.utc) - timedelta(days=tcfg.get("max_age_days", 10))
    out = []
    for c in tcfg.get("channels") or []:
        handle = str(c["handle"]).lstrip("@")
        if not _HANDLE.fullmatch(handle):
            log.info("  telegram %s: not a public channel handle", c["name"])
            continue
        page = get(f"https://t.me/s/{handle}", as_json=False)
        if not page:
            log.info("  telegram %s: unreachable", c["name"])
            continue
        if "tgme_widget_message" not in page:     # private, renamed, or preview switched off
            log.info("  telegram %s: no public preview", c["name"])
            continue
        jobs = parse(page, c["name"], since)
        log.info("  telegram %s: %d recent posts", c["name"], len(jobs))
        out += jobs
    return out
