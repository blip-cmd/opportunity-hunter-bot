"""Cheap, deterministic filtering and pre-scoring.

Thousands of raw postings come in; this narrows them to a few dozen worth
spending free LLM quota on. All patterns live in config.yaml.
"""
from __future__ import annotations

import re
from typing import Callable

from .models import Job


def _rx(patterns: list[str]) -> re.Pattern:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I) if patterns else re.compile(r"(?!x)x")


class Filters:
    def __init__(self, cfg: dict):
        r, l, s = cfg["roles"], cfg["locations"], cfg["scoring"]
        self.title_include = _rx(r["title_include"])
        self.title_exclude = _rx(r["title_exclude"])
        self.level_good = _rx(r["level_signals"])
        self.loc_strong = _rx(l["strong_allow"])
        self.loc_sponsor = _rx(l["sponsor_countries"])
        self.loc_block = _rx(l["block"])
        self.loc_priority = _rx(l.get("priority_countries", []))
        self.loc_cond = _rx(l.get("sponsor_conditional", []))
        self.loc_remote = re.compile(r"remote|home.?based|distributed|anywhere", re.I)
        self.focus = [(re.compile(p, re.I), w) for p, w in s["title_focus"].items()]
        self.skills = _rx(s["skills"])
        self.sponsor_pos = _rx(s["sponsorship_positive"])
        self.sponsor_neg = _rx(s["sponsorship_negative"])
        self.yoe_re = re.compile(r"(\d{1,2})\s*\+?\s*(?:-\s*\d+\s*)?years?(?:\s+of)?(?:\s+\w+){0,4}\s+experience", re.I)
        self.max_age = cfg["run"]["max_age_days"]
        o = cfg["opportunity_feeds"]
        self.opp_include = _rx(o["title_include"])
        self.opp_exclude = _rx(o["title_exclude"])

    # ---- stage 1: title/location gate (runs before descriptions are fetched)
    def location_ok(self, loc: str) -> bool:
        loc = (loc or "").strip()
        if not loc or loc.lower() in {"see listing", "(remote)"}:
            return True
        if self.loc_strong.search(loc):
            return True
        if self.loc_sponsor.search(loc):
            return True
        if self.loc_cond.search(loc):
            return True  # kept for now; hard_reject needs a sponsorship signal in the text
        if self.loc_block.search(loc):
            return False
        return bool(self.loc_remote.search(loc))

    def title_ok(self, title: str) -> bool:
        return bool(self.title_include.search(title)) and not self.title_exclude.search(title)

    def keep(self, title: str, loc: str) -> bool:
        return self.title_ok(title) and self.location_ok(loc)

    def keep_feed(self, title: str, body: str) -> bool:
        return bool(self.opp_include.search(title)) and not self.opp_exclude.search(title)

    # ---- stage 2: full-record checks and a heuristic score
    def min_years(self, text: str) -> int | None:
        years = [int(m.group(1)) for m in self.yoe_re.finditer(text or "")]
        years = [y for y in years if y < 20]
        return min(years) if years else None

    def pre_score(self, job: Job) -> float:
        title, desc, loc = job.title, job.description or "", job.location or ""
        score = 0.0
        if self.level_good.search(title):
            score += 3
        elif self.level_good.search(desc[:1500]):
            score += 1
        for rx, w in self.focus:
            if rx.search(title):
                score += w
                break
        score += min(3.0, 0.3 * len({m.group(0).lower() for m in self.skills.finditer(desc)}))
        if self.loc_strong.search(loc):
            score += 2
        if self.loc_priority.search(loc):
            score += 1.5  # best-odds sponsoring markets
        if self.sponsor_pos.search(desc):
            score += 2
        if self.sponsor_neg.search(desc):
            score -= 4
        yrs = self.min_years(desc)
        if yrs is not None:
            score += 1 if yrs <= 1 else (0 if yrs == 2 else -2 * (yrs - 2))
        age = job.age_days()
        if age is not None:
            score += 1.5 if age <= 3 else (0.5 if age <= 10 else 0)
        if job.source.startswith("rss:"):
            score += 1  # curated programmes are usually aimed at early-career Africans
        return round(score, 2)

    def too_old(self, job: Job) -> bool:
        age = job.age_days()
        return age is not None and age > self.max_age

    def hard_reject(self, job: Job) -> str | None:
        """Reasons to drop a job outright, before spending LLM quota."""
        if self.too_old(job):
            return "stale"
        yrs = self.min_years(job.description)
        if yrs is not None and yrs >= 4:
            return f"needs {yrs}+ yrs"
        loc = job.location or ""
        if self.loc_cond.search(loc) and not self.loc_strong.search(loc) \
                and not self.sponsor_pos.search(job.description or ""):
            return "no sponsorship signal"
        if not job.url:
            return "no url"
        return None


def make_keep(f: Filters) -> Callable[[str, str], bool]:
    return f.keep
