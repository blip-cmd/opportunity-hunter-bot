"""LinkedIn / Indeed / Google Jobs / Glassdoor via the open-source JobSpy library.

These sites rate-limit datacenter IPs (GitHub Actions). Each query is wrapped
so one blocked site never breaks the run; small result counts keep us polite.
"""
from __future__ import annotations

import logging
import math
from typing import Callable, Iterable

from ..models import Job, clean_text, parse_date

log = logging.getLogger("jobhunt")
Keep = Callable[[str, str], bool]


def _val(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


def jobspy(queries: list[dict], keep: Keep, hours_old: int = 168) -> Iterable[Job]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("  jobspy not installed; skipping")
        return
    for q in queries:
        try:
            df = scrape_jobs(
                site_name=q.get("sites", ["indeed", "linkedin", "google"]),
                search_term=q["term"],
                google_search_term=q.get("google_term", f"{q['term']} jobs {q.get('location', '')}"),
                location=q.get("location", ""),
                country_indeed=q.get("country_indeed", "worldwide"),
                is_remote=q.get("remote", False),
                results_wanted=q.get("results", 20),
                hours_old=hours_old,
                linkedin_fetch_description=True,
                verbose=0,
            )
        except Exception as e:
            log.info("  jobspy '%s' @ %s failed: %s", q["term"], q.get("location", ""), str(e)[:120])
            continue
        n = 0
        for row in df.to_dict("records"):
            title = _val(row.get("title"))
            loc = _val(row.get("location"))
            if row.get("is_remote") is True:
                loc += " (Remote)"
            if not keep(title, loc):
                continue
            n += 1
            yield Job(
                source=f"jobspy:{_val(row.get('site'))}",
                company=_val(row.get("company")),
                title=title,
                location=loc,
                url=_val(row.get("job_url_direct")) or _val(row.get("job_url")),
                description=clean_text(_val(row.get("description")), 6000),
                posted=parse_date(_val(row.get("date_posted"))),
                remote=bool(row.get("is_remote")) if row.get("is_remote") is not None else None,
            )
        log.info("  jobspy '%s' @ %s: %d kept of %d", q["term"], q.get("location", ""), n, len(df))
