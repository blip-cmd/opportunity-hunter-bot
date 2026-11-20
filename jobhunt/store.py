"""Persistent state: every job ever evaluated, so nothing is re-scored or re-emailed."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import Job


class Store:
    def __init__(self, path: Path, keep_days: int = 90):
        self.path = path
        self.keep_days = keep_days
        self.data: dict = {"jobs": {}, "fingerprints": {}, "watch": {}}
        if path.exists():
            self.data.update(json.loads(path.read_text(encoding="utf-8")))

    @property
    def jobs(self) -> dict:
        return self.data["jobs"]

    def seen(self, job: Job) -> bool:
        return job.key in self.jobs or job.fingerprint in self.data["fingerprints"]

    def record(self, job: Job, rejected: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        entry = self.jobs.get(job.key) or {"first_seen": now, "emailed": None, "tracked": False}
        entry.update(job.to_dict())
        entry["rejected"] = rejected
        self.jobs[job.key] = entry
        self.data["fingerprints"][job.fingerprint] = job.key

    def mark(self, key: str, **fields) -> None:
        self.jobs[key].update(fields)

    def backlog(self, max_age_days: int) -> list[Job]:
        """Scored, never-emailed jobs that are still fresh enough to send."""
        out = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        for e in self.jobs.values():
            if e.get("emailed") or e.get("rejected") or not e.get("llm"):
                continue
            if datetime.fromisoformat(e["first_seen"]) < cutoff:
                continue
            j = Job.from_dict(e)
            if j.age_days() is not None and j.age_days() > max_age_days + 7:
                continue
            out.append(j)
        return out

    def save(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.keep_days)
        jobs = {k: v for k, v in self.jobs.items() if datetime.fromisoformat(v["first_seen"]) >= cutoff}
        fps = {f: k for f, k in self.data["fingerprints"].items() if k in jobs}
        self.data.update(jobs=jobs, fingerprints=fps)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
