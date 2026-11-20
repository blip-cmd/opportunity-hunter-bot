"""Second look at the best matches, before anything is emailed.

The first AI pass reads the listing text a source gave us, which is often cut
short and rarely says who may apply. This pass opens the real posting page and
asks one narrow question: can the candidate apply from where they live? The
answer must quote the posting. Roles that clearly exclude the candidate are
dropped; the rest show "yes" or "unclear" with the quote in the email.

The candidate's location comes from the first line of the private profile.md
and the country label from config (run.home_country), so no personal detail
lives in this code. It runs only on the few roles good enough to email
(run.verify_max, about 10 free AI calls per run).
"""
from __future__ import annotations

import logging
import re

from . import http
from .models import Job, clean_text

log = logging.getLogger("jobhunt")

PAGE_LIMIT = 12000
MIN_PAGE_TEXT = 400  # shorter than this is a login wall or an empty shell
_NOISE = re.compile(r"<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>", re.S | re.I)

SYSTEM = """You check job postings for location eligibility. Answer only from the posting text, never guess.
Reply with ONE JSON object only, no markdown."""

TMPL = """CANDIDATE: {candidate}
HOME COUNTRY: {home}

JOB: {title} at {company}
Listed location: {location}
POSTING TEXT ({origin}):
{text}

Return JSON with exactly these keys:
{{
 "remote": "remote" | "hybrid" | "onsite" | "unclear",
 "allowed_locations": "where applicants may live or work, as stated, short; 'not stated' if absent",
 "visa_sponsorship": "yes" | "no" | "not stated",
 "eligible": "yes" | "no" | "unclear",
 "evidence": "the exact sentence from the posting your answer rests on, max 200 characters; '' if none",
 "deadline": "application deadline if stated, else 'not stated'"
}}
Rules: "yes" only if the text says remote work is open to the home country, its region or worldwide, or the role offers visa sponsorship or relocation.
"no" if it requires existing right to work, residence or citizenship somewhere else with no sponsorship, or limits remote work to countries or time zones that exclude the home country.
Otherwise "unclear". A listing that only names a city is "unclear", not "no"."""


def page_text(job: Job) -> tuple[str, str]:
    """The posting page as plain text, or the listing text if the page cannot be read."""
    raw = http.get(job.url, timeout=20, retries=1, as_json=False) if job.url else None
    text = clean_text(_NOISE.sub(" ", raw or ""), PAGE_LIMIT)
    if len(text) >= MIN_PAGE_TEXT and len(text) >= len(job.description or "") // 2:
        return text, "full posting page"
    return (job.description or "")[:PAGE_LIMIT], "listing text only, page not readable"


def check(jobs: list[Job], scorer, candidate: str, home: str) -> None:
    """Adds job.llm["eligibility"] to each job; skips jobs already checked in an earlier run."""
    for j in jobs:
        if (j.llm or {}).get("eligibility"):
            continue
        text, origin = page_text(j)
        out = scorer.ask(SYSTEM, TMPL.format(candidate=candidate, home=home, title=j.title, company=j.company,
                                             location=j.location, origin=origin, text=text))
        if not out:
            log.info("  check %s | %s: no AI answer, left unchecked", j.company[:24], j.title[:50])
            continue
        verdict = str(out.get("eligible", "unclear")).lower()
        out["eligible"] = verdict if verdict in {"yes", "no", "unclear"} else "unclear"
        out["source"] = origin
        j.llm["eligibility"] = out
        log.info("  check %s | %s: eligible %s (%s)", j.company[:24], j.title[:50], out["eligible"], origin)


def excluded(job: Job) -> bool:
    return ((job.llm or {}).get("eligibility") or {}).get("eligible") == "no"


def summary(job: Job) -> str:
    """One line for the email and digest, e.g. 'yes ("Open to candidates across EMEA") [full posting page]'."""
    e = (job.llm or {}).get("eligibility")
    if not e:
        return "not checked"
    quote = str(e.get("evidence") or "").strip()[:200]
    return f'{e["eligible"]}' + (f' ("{quote}")' if quote else "") + f' [{e.get("source", "")}]'
