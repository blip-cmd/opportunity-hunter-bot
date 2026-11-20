"""Remote / international job boards with free public feeds.

Attribution: Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow, Working Nomads
and We Work Remotely publish these feeds for public use and ask for a link
back. Every job keeps its original board URL so attribution is preserved.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

import feedparser

from ..http import get
from ..models import Job, clean_text, parse_date

log = logging.getLogger("jobhunt")
Keep = Callable[[str, str], bool]
DESC_LIMIT = 6000


def remotive(searches: list[str], keep: Keep) -> Iterable[Job]:
    seen = set()
    for q in searches:
        data = get("https://remotive.com/api/remote-jobs", params={"search": q, "limit": 100})
        for j in (data or {}).get("jobs", []):
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            title, loc = j.get("title", ""), j.get("candidate_required_location", "") + " (Remote)"
            if keep(title, loc):
                yield Job("remotive", j.get("company_name", ""), title, loc, j.get("url", ""),
                          clean_text(j.get("description"), DESC_LIMIT),
                          parse_date(j.get("publication_date")), True)


def remoteok(_: list[str], keep: Keep) -> Iterable[Job]:
    data = get("https://remoteok.com/api")
    for j in (data or [])[1:]:  # element 0 is the legal notice
        title, loc = j.get("position", ""), (j.get("location") or "Worldwide") + " (Remote)"
        if keep(title, loc):
            yield Job("remoteok", j.get("company", ""), title, loc, j.get("url", ""),
                      clean_text(j.get("description"), DESC_LIMIT), parse_date(j.get("epoch")), True)


def himalayas(searches: list[str], keep: Keep) -> Iterable[Job]:
    seen = set()
    for q in searches:
        data = get("https://himalayas.app/jobs/api/search", params={"q": q, "sort": "recent"})
        for j in (data or {}).get("jobs", []):
            url = j.get("applicationLink") or j.get("guid", "")
            if url in seen:
                continue
            seen.add(url)
            restr = j.get("locationRestrictions") or []
            loc = (", ".join(restr) if restr else "Worldwide") + " (Remote)"
            title = j.get("title", "")
            if keep(title, loc):
                yield Job("himalayas", j.get("companyName", ""), title, loc, url,
                          clean_text(j.get("description") or j.get("excerpt"), DESC_LIMIT),
                          parse_date(j.get("pubDate")), True)


def jobicy(tags: list[str], keep: Keep) -> Iterable[Job]:
    seen = set()
    for tag in tags:
        data = get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag})
        for j in (data or {}).get("jobs", []):
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            title, loc = j.get("jobTitle", ""), f"{j.get('jobGeo', '')} (Remote)"
            if keep(title, loc):
                yield Job("jobicy", j.get("companyName", ""), title, loc, j.get("url", ""),
                          clean_text(j.get("jobDescription"), DESC_LIMIT), parse_date(j.get("pubDate")), True)


def arbeitnow(_: list[str], keep: Keep, pages: int = 4) -> Iterable[Job]:
    """German board; many listings state visa sponsorship / English-speaking teams."""
    for page in range(1, pages + 1):
        data = get("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        for j in (data or {}).get("data", []):
            title = j.get("title", "")
            loc = j.get("location", "") + (" (Remote)" if j.get("remote") else "")
            if keep(title, loc):
                yield Job("arbeitnow", j.get("company_name", ""), title, loc, j.get("url", ""),
                          clean_text(j.get("description"), DESC_LIMIT), parse_date(j.get("created_at")),
                          bool(j.get("remote")))


def workingnomads(_: list[str], keep: Keep) -> Iterable[Job]:
    data = get("https://www.workingnomads.com/api/exposed_jobs/")
    for j in data or []:
        title, loc = j.get("title", ""), (j.get("location") or "Anywhere") + " (Remote)"
        if keep(title, loc):
            yield Job("workingnomads", j.get("company_name", ""), title, loc, j.get("url", ""),
                      clean_text(j.get("description"), DESC_LIMIT), parse_date(j.get("pub_date")), True)


WWR_FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
]


def weworkremotely(_: list[str], keep: Keep) -> Iterable[Job]:
    for feed_url in WWR_FEEDS:
        text = get(feed_url, as_json=False)
        if not text:
            continue
        for e in feedparser.parse(text).entries:
            # WWR titles look like "Company: Role"
            company, _, title = (e.get("title", "")).partition(": ")
            if not title:
                company, title = "", company
            loc = (e.get("region") or "Anywhere") + " (Remote)"
            if keep(title, loc):
                yield Job("weworkremotely", company, title, loc, e.get("link", ""),
                          clean_text(e.get("summary"), DESC_LIMIT), parse_date(e.get("published")), True)


BOARDS = {
    "remotive": remotive,
    "remoteok": remoteok,
    "himalayas": himalayas,
    "jobicy": jobicy,
    "arbeitnow": arbeitnow,
    "workingnomads": workingnomads,
    "weworkremotely": weworkremotely,
}
