from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(raw: Optional[str], limit: Optional[int] = None) -> str:
    """Strip HTML (including double-escaped HTML) and collapse whitespace."""
    if not raw:
        return ""
    text = html.unescape(html.unescape(str(raw)))
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text[:limit] if limit else text


@dataclass
class Job:
    source: str
    company: str
    title: str
    location: str
    url: str
    description: str = ""
    posted: Optional[datetime] = None
    remote: Optional[bool] = None
    deadline: str = ""
    # Filled in later by the pipeline
    pre_score: float = 0.0
    llm: dict = field(default_factory=dict)
    final_score: float = 0.0

    @property
    def key(self) -> str:
        norm = re.sub(r"[?#].*$", "", self.url.strip().lower()).rstrip("/")
        return hashlib.sha1(norm.encode()).hexdigest()[:16]

    @property
    def fingerprint(self) -> str:
        """Catches the same role posted on several boards."""
        t = re.sub(r"[^a-z0-9]", "", f"{self.company}|{self.title}".lower())
        return hashlib.sha1(t.encode()).hexdigest()[:16]

    def age_days(self) -> Optional[float]:
        if not self.posted:
            return None
        p = self.posted if self.posted.tzinfo else self.posted.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - p).total_seconds() / 86400

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted"] = self.posted.isoformat() if self.posted else None
        d["description"] = self.description[:600]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        d = dict(d)
        d["posted"] = parse_date(d.get("posted"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


def parse_date(value) -> Optional[datetime]:
    """Accepts ISO strings, epoch seconds or epoch milliseconds."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
            v = float(value)
            if v > 1e11:
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = re.sub(r"\s+", " ", str(value).strip()).replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        for fmt in ("%B %d, %Y", "%Y-%m-%d", "%a, %d %b %Y %H:%M:%S %z", "%d %b %Y"):
            try:
                d = datetime.strptime(s, fmt)
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    except Exception:
        return None
    return None
