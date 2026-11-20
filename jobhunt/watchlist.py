"""Watches named programmes (graduate schemes, fellowships) that have no job feed.

Each run fetches the programme page, looks for 'applications open' wording,
and flags programmes whose expected opening month is close.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date

from .http import get
from .models import clean_text

log = logging.getLogger("jobhunt")

OPEN_RE = re.compile(
    r"applications? (?:are |is )?(?:now )?open|apply now|now accepting|accepting applications|"
    r"application (?:portal|window) (?:is )?open|submit your application",
    re.I,
)
CLOSED_RE = re.compile(
    r"applications? (?:are |have |has )?(?:now )?closed|not (?:currently )?accepting|"
    r"(?:will|to) open (?:in|on)|opens? (?:in|on) |register your interest|join our talent",
    re.I,
)


def months_until(ym: str) -> int | None:
    m = re.match(r"(\d{4})-(\d{1,2})", ym or "")
    if not m:
        return None
    t = date.today()
    return (int(m.group(1)) - t.year) * 12 + int(m.group(2)) - t.month


def check(items: list[dict], state: dict) -> list[dict]:
    """Returns alerts: [{name, url, kind: 'open'|'soon'|'changed', detail, ...}]."""
    alerts = []
    for it in items:
        name, url = it["name"], it["url"]
        prev = state.get(url, {})
        mu = months_until(it.get("expected_open", ""))
        html = get(url, as_json=False, timeout=40)
        if not html:
            # Many corporate sites block bots; still send the calendar reminder.
            log.info("  watch %s: unreachable (date reminder only)", name)
            if mu is not None and 0 <= mu <= 1 and prev.get("soon_alerted") != it.get("expected_open"):
                alerts.append({**it, "kind": "soon",
                               "detail": f"Usually opens {it['expected_open']}. Check the page and prepare now."})
                prev["soon_alerted"] = it.get("expected_open")
            prev["checked"] = date.today().isoformat()
            state[url] = prev
            continue
        text = clean_text(html)
        digest = hashlib.sha1(text.encode()).hexdigest()
        opened = bool(OPEN_RE.search(text)) and not CLOSED_RE.search(text)
        if opened and not prev.get("open_alerted"):
            alerts.append({**it, "kind": "open", "detail": "Page now shows applications open."})
            prev["open_alerted"] = date.today().isoformat()
        elif not opened:
            prev.pop("open_alerted", None)
            if mu is not None and 0 <= mu <= 1 and prev.get("soon_alerted") != it.get("expected_open"):
                alerts.append({**it, "kind": "soon", "detail": f"Usually opens {it['expected_open']}. Prepare now."})
                prev["soon_alerted"] = it.get("expected_open")
            elif prev.get("hash") and prev["hash"] != digest and mu is not None and mu <= 2:
                alerts.append({**it, "kind": "changed", "detail": "Programme page changed. Check for news."})
        prev.update(hash=digest, checked=date.today().isoformat(), open=opened)
        state[url] = prev
    return alerts
