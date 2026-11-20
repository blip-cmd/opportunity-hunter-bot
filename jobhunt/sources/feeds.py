"""Opportunity RSS feeds (fellowships, graduate schemes, African programmes) and HN hiring threads."""
from __future__ import annotations

import logging
import re
from typing import Callable, Iterable

import feedparser

from ..http import get
from ..models import Job, clean_text, parse_date

log = logging.getLogger("jobhunt")
Keep = Callable[[str, str], bool]

_DEADLINE_RE = re.compile(r"deadline[^.:]*[:\-]?\s*([A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2} [A-Z][a-z]+,? \d{4})")


def rss(feeds: list[dict], keep_feed: Callable[[str, str], bool]) -> Iterable[Job]:
    """keep_feed(title, summary) decides relevance, since RSS has no location field."""
    for f in feeds:
        text = get(f["url"], as_json=False)
        if not text:
            log.info("  rss %s: unreachable", f["name"])
            continue
        for e in feedparser.parse(text).entries:
            title = clean_text(e.get("title"))
            body = clean_text(
                (e.get("content") or [{}])[0].get("value") if e.get("content") else e.get("summary"),
                5000,
            )
            if not keep_feed(title, body):
                continue
            m = _DEADLINE_RE.search(body)
            yield Job(
                source=f"rss:{f['name']}",
                company=f["name"],
                title=title,
                location="See listing",
                url=e.get("link", ""),
                description=body,
                posted=parse_date(e.get("published")),
                deadline=m.group(1) if m else "",
            )


def hackernews(keep: Keep, max_comments: int = 600) -> Iterable[Job]:
    """Latest 'Ask HN: Who is hiring?' thread. Each top-level comment is one company post."""
    data = get(
        "https://hn.algolia.com/api/v1/search_by_date",
        params={"tags": "story,author_whoishiring", "hitsPerPage": 5},
    )
    story = next((h for h in (data or {}).get("hits", []) if "who is hiring" in h.get("title", "").lower()), None)
    if not story:
        return
    thread = get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", timeout=60) or {}
    for c in (thread.get("children") or [])[:max_comments]:
        text = clean_text(c.get("text"))
        if not text:
            continue
        header = text.split(" ", 60)
        first = " ".join(header[:60])
        parts = [p.strip() for p in first.split("|")]
        company = parts[0][:60] if parts else "HN post"
        title = " | ".join(parts[1:3])[:140] if len(parts) > 1 else first[:140]
        loc = " | ".join(parts[1:5])
        if not keep(title + " " + first, loc):
            continue
        yield Job(
            source="hackernews",
            company=company,
            title=title or first[:120],
            location=loc[:160],
            url=f"https://news.ycombinator.com/item?id={c.get('id')}",
            description=text[:5000],
            posted=parse_date(c.get("created_at_i") or c.get("created_at")),
        )
