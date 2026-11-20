"""Writes picks into the root APPLICATIONS_TRACKER.csv using the existing schema."""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

from . import verify
from .models import Job

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from add_application import FIELDNAMES, TRACKER_PATH, add_row  # noqa: E402

CV_PROFILE = {"data": "data", "software": "software", "ml": "ai / ml", "robotics": "robotics"}


def existing_urls() -> set[str]:
    if not TRACKER_PATH.exists():
        return set()
    with open(TRACKER_PATH, newline="", encoding="utf-8") as f:
        return {(r.get("Job_URL") or "").strip().rstrip("/").lower() for r in csv.DictReader(f)}


def gates_text(g: dict) -> str:
    return ", ".join(f"{k.title()} [{g.get(k, '?')}]" for k in ("location", "timeline", "level", "direction"))


def add_job(job: Job, next_action: str) -> bool:
    """Append one job; returns False if its URL is already tracked."""
    if job.url.strip().rstrip("/").lower() in existing_urls():
        return False
    l = job.llm or {}
    add_row(
        company=job.company,
        role=job.title,
        location=job.location,
        status=l.get("status", "STRETCH"),
        score=f"{l.get('score', '?')}/10",
        gates=gates_text(l.get("gates") or {}),
        prereqs=l.get("prerequisites_blockers", "Unknown") + (
            f' | Eligible from home: {verify.summary(job)}' if l.get("eligibility") else ""),
        gaps=l.get("gaps", ""),
        cv_profile=CV_PROFILE.get(l.get("cv_profile", ""), l.get("cv_profile", "software")),
        deadline=l.get("deadline") or job.deadline or "Rolling",
        job_url=job.url,
        next_action=next_action,
        cover_note=l.get("cover_note", ""),
        date_str=date.today().isoformat(),
    )
    return True


def add_watch(name: str, url: str, opens: str, cv_profile: str, note: str) -> bool:
    if url.strip().rstrip("/").lower() in existing_urls():
        return False
    add_row(
        company=name.split(":")[0],
        role=name,
        location="See programme page",
        status="WATCH",
        score="?/10",
        cv_profile=cv_profile,
        deadline=f"Expected to open {opens}",
        job_url=url,
        next_action=f"Watchlist: emailed when the page shows applications open (expected {opens})",
        cover_note=note,
        date_str=date.today().isoformat(),
    )
    return True
