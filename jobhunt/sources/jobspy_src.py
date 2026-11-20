"""LinkedIn / Indeed / Google Jobs / Glassdoor via the open-source JobSpy library.

These sites rate-limit datacenter IPs (GitHub Actions). Each query is wrapped
so one blocked site never breaks the run; small result counts keep us polite.
Queries are spaced out, and after a few blocked queries in a row the rest are
skipped for this run instead of hammering a site that has already said no.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Callable, Iterable

from ..models import Job, clean_text, parse_date

log = logging.getLogger("jobhunt")
Keep = Callable[[str, str], bool]
# JobSpy 1.4 logs through "jobspy"; older releases used non-propagating "JobSpy:<Site>" loggers.
LOGGERS = ("", "jobspy") + tuple(f"JobSpy:{n}" for n in ("LinkedIn", "Indeed", "Google", "Glassdoor"))


def _val(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    return str(v)


class _BlockCounter(logging.Handler):
    """Counts the "status code 429" messages JobSpy logs when a site blocks us."""

    def __init__(self):
        super().__init__()
        self.hits = 0

    def emit(self, record):
        if "429" in record.getMessage():
            self.hits += 1


def jobspy(queries: list[dict], keep: Keep, hours_old: int = 168,
           pause: float = 30, max_blocked: int = 3) -> Iterable[Job]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning("  jobspy not installed; skipping")
        return
    blocks = _BlockCounter()
    site_logs = [logging.getLogger(n) for n in LOGGERS]
    for lg in site_logs:
        lg.addHandler(blocks)
    try:
        yield from _run(scrape_jobs, queries, keep, hours_old, pause, max_blocked, blocks)
    finally:
        for lg in site_logs:
            lg.removeHandler(blocks)


def _run(scrape_jobs, queries, keep, hours_old, pause, max_blocked, blocks) -> Iterable[Job]:
    blocked_in_a_row = 0
    for i, q in enumerate(queries):
        if blocked_in_a_row >= max_blocked:
            log.info("  jobspy: %d blocked queries in a row; skipping the other %d this run",
                     blocked_in_a_row, len(queries) - i)
            return
        if i:
            time.sleep(pause)
        hits_before = blocks.hits
        extra = {"country_indeed": q["country_indeed"]} if q.get("country_indeed") else {}
        try:
            df = scrape_jobs(
                site_name=q.get("sites", ["indeed", "linkedin", "google"]),
                search_term=q["term"],
                google_search_term=q.get("google_term", f"{q['term']} jobs {q.get('location', '')}"),
                location=q.get("location", ""),
                is_remote=q.get("remote", False),
                results_wanted=q.get("results", 20),
                hours_old=hours_old,
                linkedin_fetch_description=True,
                verbose=0,
                **extra,
            )
        except Exception as e:
            log.info("  jobspy '%s' @ %s failed: %s", q["term"], q.get("location", ""), str(e)[:120])
            continue
        # Blocked means a site refused us and nothing came back at all.
        blocked_in_a_row = blocked_in_a_row + 1 if blocks.hits > hits_before and len(df) == 0 else 0
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
