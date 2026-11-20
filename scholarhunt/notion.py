"""Read the team's Notion databases and check each row's official page.

Phase 1 is read only: nothing is ever written to Notion. For every row in
scope the official link is opened, a deadline is looked for (and kept only with
a quote that really appears on the page), and the page text is fingerprinted so
a change can be reported next week. Lab pages also raise an alert when they
change and mention openings.

Everything specific to a workspace lives in the private config under
`notion:` (database IDs, property names, people and their consent). This module
holds no names, IDs or addresses. The token comes from the NOTION_TOKEN secret.

Rows are in scope when they are tagged with a person whose consent is "yes",
with the shared label, or with nobody. Rows tagged only with people who have
not consented are skipped entirely.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date

import requests

from jobhunt import http
from jobhunt.models import clean_text

log = logging.getLogger("scholarhunt")

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
PAGE_LIMIT = 20000
MIN_PAGE_TEXT = 200
_NOISE = re.compile(r"<(script|style|noscript|svg|head|nav|footer)\b.*?</\1>", re.S | re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\s{2,}")
_DEADLINE_WORDS = re.compile(r"deadline|closing date|\bcloses?\b|apply by|applications? (?:open|close|are due|due)|due (?:date|by)|submi(?:t|ssion)", re.I)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATEISH = re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH}|\b{_MONTH}\s+\d{{1,2}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}}/\d{{1,2}}/\d{{2,4}}\b", re.I)
_OPENINGS = re.compile(r"ph\.?d (?:position|student|opening)s?|prospective students|we are (?:hiring|recruiting)|join (?:us|the lab|our lab)|open positions?|openings?", re.I)

SYSTEM = """You read official university and scholarship pages. Answer only from the sentences given, never guess.
Reply with ONE JSON object only, no markdown."""

TMPL = """PROGRAMME: {title}
TODAY: {today}
SENTENCES FROM THE OFFICIAL PAGE {url}:
{sentences}

Return JSON with exactly these keys:
{{
 "deadline": "the next application deadline on or after TODAY as YYYY-MM-DD, or '' if none is stated",
 "evidence": "the sentence above it rests on, copied exactly, max 200 characters; '' if none",
 "applies_to": "who or which intake it is for, short (e.g. 'international applicants, fall 2027'); '' if not stated"
}}
Rules: use only a date whose day, month and year are all stated or plainly implied by the sentence.
Ignore deadlines that have passed. If several apply, prefer the one for international applicants and funding."""


def _plain(prop: dict):
    """A Notion property value as plain Python: str, list of str, or None."""
    kind = prop.get("type")
    val = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in val or []).strip() or None
    if kind in ("select", "status"):
        return (val or {}).get("name")
    if kind == "multi_select":
        return [o["name"] for o in val or []]
    if kind == "date":
        return (val or {}).get("start")
    if kind in ("url", "email", "number", "checkbox"):
        return val
    return None


def _query(db_id: str, token: str) -> list[dict] | None:
    headers = {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}
    pages, body = [], {"page_size": 100}
    while True:
        try:
            r = requests.post(f"{API}/databases/{db_id}/query", headers=headers, json=body, timeout=30)
        except requests.RequestException as e:
            log.info("  Notion: request failed (%s)", type(e).__name__)
            return None
        if r.status_code != 200:
            # 404 means the database is not shared with the integration.
            log.info("  Notion: database query returned %s", r.status_code)
            return None
        data = r.json()
        pages += [p for p in data.get("results", []) if not p.get("archived") and not p.get("in_trash")]
        if not data.get("has_more"):
            return pages
        body["start_cursor"] = data["next_cursor"]


def fetch_rows(ncfg: dict) -> list[dict]:
    """All rows of the configured databases, reduced to the fields the bot uses."""
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    if not token:
        log.info("Notion: no token set, skipped")
        return []
    rows = []
    for db in ncfg.get("databases") or []:
        pages = _query(db["id"], token)
        if pages is None:
            log.info("  Notion %s: unreadable", db["name"])
            continue
        for p in pages:
            props = {k: _plain(v) for k, v in p.get("properties", {}).items()}
            people = props.get(db.get("people", "")) or []
            rows.append({
                "db": db["name"],
                "title": props.get(db["title"]) or "(untitled)",
                "notion_url": p.get("url", ""),
                "link": (props.get(db.get("link", "")) or "").strip(),
                "people": people if isinstance(people, list) else [people],
                "status": props.get(db.get("status", "")),
                "human_deadline": props.get(db.get("deadline", "")),
                "watch": bool(db.get("watch")),
            })
        log.info("  Notion %s: %d rows", db["name"], len(pages))
    return rows


def consenting(ncfg: dict) -> set[str]:
    return {p["name"] for p in ncfg.get("people") or [] if str(p.get("consent", "")).lower() in ("yes", "true")}


def in_scope(rows: list[dict], ncfg: dict) -> list[dict]:
    ok = consenting(ncfg) | {ncfg.get("shared_label", "Shared")}
    skip = set(ncfg.get("skip_status") or [])
    return [r for r in rows if r["status"] not in skip and (not r["people"] or ok & set(r["people"]))]


def _page_text(url: str) -> str:
    raw = http.get(url, timeout=25, retries=1, as_json=False)
    return clean_text(_NOISE.sub(" ", raw or ""), PAGE_LIMIT)


def _deadline_sentences(text: str) -> list[str]:
    out = []
    for s in _SENTENCE.split(text):
        s = s.strip()
        if 20 <= len(s) <= 400 and _DEADLINE_WORDS.search(s) and _DATEISH.search(s):
            out.append(s)
    return out[:12]


def _norm(s: str) -> str:
    return re.sub(r"\W+", " ", s).strip().lower()


def _ask_deadline(row: dict, sentences: list[str], scorer, today: date) -> dict | None:
    out = scorer.ask(SYSTEM, TMPL.format(title=row["title"], today=today.isoformat(), url=row["link"],
                                         sentences="\n".join(f"- {s}" for s in sentences)))
    if not out:
        return None
    try:
        dl = date.fromisoformat(str(out.get("deadline", "")).strip())
    except ValueError:
        return None
    evidence = str(out.get("evidence", "")).strip()[:200]
    # Keep the date only if it is in the future and its quote really is on the page.
    if dl < today or not evidence or _norm(evidence) not in _norm(" ".join(sentences)):
        log.info("  %s: deadline answer not backed by the page, dropped", row["title"][:50])
        return None
    return {"deadline": dl.isoformat(), "evidence": evidence, "applies_to": str(out.get("applies_to", ""))[:80]}


def check(rows: list[dict], scorer, state: dict, today: date, max_calls: int) -> dict:
    """Open each row's official link; adds row['found'], row['changed'], row['openings']."""
    pages = state.setdefault("notion", {})
    stats = {"checked": 0, "unreadable": 0, "no_link": 0, "deadlines": 0, "changed": 0, "ai_calls": 0}
    for r in rows:
        r.update(found=None, changed=False, openings=False)
        if not r["link"].startswith("http"):
            stats["no_link"] += 1
            continue
        text = _page_text(r["link"])
        if len(text) < MIN_PAGE_TEXT:
            stats["unreadable"] += 1
            log.info("  %s: page unreadable", r["title"][:60])
            continue
        stats["checked"] += 1
        h = hashlib.sha1(text.encode()).hexdigest()
        prev = pages.get(r["link"], {})
        r["changed"] = bool(prev.get("hash")) and prev["hash"] != h
        r["openings"] = r["watch"] and r["changed"] and bool(_OPENINGS.search(text))
        stats["changed"] += r["changed"]
        # An unchanged page keeps last week's answer (even "none found"), unless that deadline has passed.
        same = prev.get("hash") == h and prev.get("asked")
        found = prev.get("found") if same else None
        expired = bool(found) and found["deadline"] < today.isoformat()
        asked = bool(same) and not expired
        if expired:
            found = None
        if not asked and scorer and stats["ai_calls"] < max_calls:
            sentences = _deadline_sentences(text)
            if sentences:
                stats["ai_calls"] += 1
                found = _ask_deadline(r, sentences, scorer, today)
            asked = True
        r["found"] = found
        stats["deadlines"] += bool(found)
        pages[r["link"]] = {"hash": h, "checked": today.isoformat(), "asked": asked, "found": found}
        log.info("  %s: %s%s", r["title"][:60], f"deadline {found['deadline']}" if found else "no deadline found",
                 ", page changed" if r["changed"] else "")
    return stats


def deadline_alerts(rows: list[dict], state: dict, today: date, days_before: list[int]) -> list[dict]:
    """Same thresholds as the named programmes. The human date column wins over a found date."""
    alerts = []
    for r in rows:
        dl = r["human_deadline"] or (r["found"] or {}).get("deadline")
        if not dl:
            continue
        try:
            days = (date.fromisoformat(dl[:10]) - today).days
        except ValueError:
            continue
        hit = [t for t in days_before if 0 <= days <= t]
        if not hit:
            continue
        tag = f"notion|{r['title']}|{dl[:10]}|{min(hit)}"
        if tag in state["alerted"]:
            continue
        source = "Notion date" if r["human_deadline"] else "official page"
        alerts.append({"name": r["title"], "deadline": dl[:10], "url": r["link"] or r["notion_url"], "days": days,
                       "tag": tag, "note": f"{source}" + (f': "{r["found"]["evidence"]}"' if not r["human_deadline"] else ""),
                       "notion_url": r["notion_url"]})
    return alerts


def news(rows: list[dict], state: dict) -> list[dict]:
    """Things worth a line in the email: newly found deadlines and lab pages with openings."""
    seen = state.setdefault("notion_reported", {})
    out = []
    for r in rows:
        f = r["found"]
        if f and not r["human_deadline"] and seen.get(r["notion_url"]) != f["deadline"]:
            out.append({**r, "kind": "deadline", "key": f["deadline"]})
        if r["openings"]:
            out.append({**r, "kind": "openings", "key": None})
    return out


def mark_reported(items: list[dict], state: dict) -> None:
    for i in items:
        if i["kind"] == "deadline":
            state.setdefault("notion_reported", {})[i["notion_url"]] = i["key"]
