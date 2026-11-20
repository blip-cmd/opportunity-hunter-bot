"""Company career boards: Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Amazon.

Each fetcher takes a board slug and a `keep(title, location)` callback. The
callback drops irrelevant titles early so we only download full descriptions
for roles that might fit.
"""
from __future__ import annotations

import logging
from typing import Callable, Iterable

from ..http import get
from ..models import Job, clean_text, parse_date

log = logging.getLogger("jobhunt")
Keep = Callable[[str, str], bool]
DESC_LIMIT = 12000


def greenhouse(slug: str, company: str, keep: Keep) -> Iterable[Job]:
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    for j in (data or {}).get("jobs", []):
        title = j.get("title", "")
        loc = (j.get("location") or {}).get("name", "")
        if not keep(title, loc):
            continue
        detail = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{j['id']}") or {}
        yield Job(
            source="greenhouse",
            company=company,
            title=title,
            location=loc,
            url=j.get("absolute_url", ""),
            description=clean_text(detail.get("content"), DESC_LIMIT),
            posted=parse_date(j.get("first_published") or j.get("updated_at")),
            deadline=str(detail.get("application_deadline") or ""),
        )


def lever(slug: str, company: str, keep: Keep) -> Iterable[Job]:
    data = get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    for j in data or []:
        cats = j.get("categories") or {}
        title = j.get("text", "")
        loc = ", ".join(filter(None, [cats.get("location"), *(cats.get("allLocations") or [])][:3]))
        if not keep(title, loc):
            continue
        desc = " ".join(
            [j.get("descriptionPlain", "")]
            + [clean_text(f"{l.get('text')}: {l.get('content')}") for l in j.get("lists", [])]
            + [j.get("additionalPlain", "")]
        )
        yield Job(
            source="lever",
            company=company,
            title=title,
            location=f"{loc} ({j.get('workplaceType', '')})".strip(),
            url=j.get("hostedUrl", ""),
            description=clean_text(desc, DESC_LIMIT),
            posted=parse_date(j.get("createdAt")),
            remote=(j.get("workplaceType") == "remote") or None,
        )


def ashby(slug: str, company: str, keep: Keep) -> Iterable[Job]:
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    for j in (data or {}).get("jobs", []):
        if j.get("isListed") is False:
            continue
        title = j.get("title", "")
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        loc = ", ".join(filter(None, locs[:4]))
        if j.get("isRemote"):
            loc += " (Remote)"
        if not keep(title, loc):
            continue
        yield Job(
            source="ashby",
            company=company,
            title=title,
            location=loc,
            url=j.get("jobUrl", ""),
            description=clean_text(j.get("descriptionPlain") or j.get("descriptionHtml"), DESC_LIMIT),
            posted=parse_date(j.get("publishedAt")),
            remote=bool(j.get("isRemote")),
        )


def workable(slug: str, company: str, keep: Keep) -> Iterable[Job]:
    data = get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    for j in (data or {}).get("jobs", []):
        title = j.get("title", "")
        loc = ", ".join(filter(None, [j.get("city"), j.get("country")]))
        if j.get("telecommuting"):
            loc += " (Remote)"
        if not keep(title, loc):
            continue
        yield Job(
            source="workable",
            company=company,
            title=title,
            location=loc,
            url=j.get("url") or j.get("shortlink", ""),
            description=clean_text(j.get("description"), DESC_LIMIT),
            posted=parse_date(j.get("published_on") or j.get("created_at")),
            remote=bool(j.get("telecommuting")),
        )


def smartrecruiters(slug: str, company: str, keep: Keep) -> Iterable[Job]:
    offset = 0
    while offset < 1000:
        data = get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 100, "offset": offset},
        )
        content = (data or {}).get("content", [])
        if not content:
            return
        for j in content:
            title = j.get("name", "")
            l = j.get("location") or {}
            loc = ", ".join(filter(None, [l.get("city"), l.get("country", "").upper()]))
            if l.get("remote"):
                loc += " (Remote)"
            if not keep(title, loc):
                continue
            detail = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{j['id']}") or {}
            sections = ((detail.get("jobAd") or {}).get("sections")) or {}
            desc = " ".join(clean_text((s or {}).get("text")) for s in sections.values())
            yield Job(
                source="smartrecruiters",
                company=company,
                title=title,
                location=loc,
                url=detail.get("postingUrl") or f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                description=desc[:DESC_LIMIT],
                posted=parse_date(j.get("releasedDate")),
                remote=bool(l.get("remote")),
            )
        offset += 100


def amazon(queries: list[str], keep: Keep) -> Iterable[Job]:
    """Amazon's careers site exposes a JSON search; good for graduate SDE schemes."""
    for q in queries:
        data = get(
            "https://www.amazon.jobs/en/search.json",
            params={"base_query": q, "result_limit": 50, "sort": "recent"},
        )
        for j in (data or {}).get("jobs", []):
            title = j.get("title", "")
            loc = j.get("normalized_location") or j.get("location", "")
            if not keep(title, loc):
                continue
            desc = " ".join(
                clean_text(j.get(k))
                for k in ("description", "basic_qualifications", "preferred_qualifications")
            )
            yield Job(
                source="amazon",
                company="Amazon",
                title=title,
                location=loc,
                url="https://www.amazon.jobs" + j.get("job_path", ""),
                description=desc[:DESC_LIMIT],
                posted=parse_date(j.get("posted_date")),
            )


FETCHERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "workable": workable,
    "smartrecruiters": smartrecruiters,
}
