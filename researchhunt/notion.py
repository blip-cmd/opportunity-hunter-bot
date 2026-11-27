"""Notion sync for the Research Programmes database (personal page).

Reads existing rows, checks official pages for changes and deadlines, inserts
new qualifying finds (with query-before-insert dedupe), and logs runs to the
shared Bot Activity Log. All IDs and property names live in private config.
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

log = logging.getLogger("researchhunt")

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"
_DEADLINE_RE = re.compile(
    r"(?:deadline|apply by|applications? (?:open|close|due)|due date|cohort)\D{0,40}"
    r"(\d{4}-\d{2}-\d{2}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:,?\s+\d{4})?|\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?(?:\s+\d{4})?)",
    re.I,
)


def _no_em_dash(s: str) -> str:
    return re.sub(r"\s*\u2014\s*", ", ", s or "").strip()


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": VERSION,
        "Content-Type": "application/json",
    }


def _plain(prop: dict) -> str | None:
    kind = prop.get("type")
    val = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in val or []).strip() or None
    if kind in ("select", "status"):
        return (val or {}).get("name")
    if kind == "date":
        return (val or {}).get("start")
    if kind in ("url", "checkbox"):
        return val
    return None


def _canon_url(u: str | None) -> str:
    if not u:
        return ""
    return u.strip().rstrip("/").lower()


def _query_all(db_id: str, headers: dict) -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        r = requests.post(f"{API}/databases/{db_id}/query", headers=headers, json=body, timeout=25)
        if r.status_code != 200:
            log.warning("Notion query %s failed (%d): %s", db_id, r.status_code, r.text[:200])
            break
        data = r.json()
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def _rt(text: str, limit: int = 1900) -> dict:
    return {"rich_text": [{"text": {"content": _no_em_dash(text)[:limit]}}]}


def _cost_bucket(raw: str) -> str:
    low = (raw or "").lower()
    if "paid (low" in low or ("paid" in low and "waiver" not in low and "scholarship" not in low and "unpaid" not in low):
        return "Paid (Low Priority)"
    if "stipend" in low:
        return "Stipend"
    if "apc" in low:
        return "APC Support"
    if "waiver" in low or "scholarship" in low:
        return "Scholarship / Waiver"
    if "free" in low or "unpaid" in low:
        return "Free"
    return "Unclear"


def _type_bucket(raw: str) -> str:
    low = (raw or "").lower()
    for opt in ("remote_research_collab", "structured_programme", "mentorship_fellowship", "research_internship"):
        if opt in low:
            return opt
    if "fellow" in low or "mentor" in low:
        return "mentorship_fellowship"
    if "intern" in low:
        return "research_internship"
    if "collab" in low:
        return "remote_research_collab"
    return "structured_programme"


def _log_activity(log_page: str, headers: dict, summary: str, dry_run: bool) -> None:
    if not log_page:
        return
    if dry_run:
        log.info("Notion dry-run log: %s", summary)
        return
    block = {
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": _no_em_dash(summary)}}]},
            }
        ]
    }
    r = requests.patch(f"{API}/blocks/{log_page}/children", headers=headers, json=block, timeout=20)
    if r.status_code != 200:
        log.warning("Notion activity log append failed (%d)", r.status_code)


def sync(cfg: dict, new_finds: list[dict], state: dict, dry_run: bool = False) -> dict:
    """Sync new finds to Notion and check existing watched rows. Returns summary counts."""
    ncfg = cfg.get("notion") or {}
    db_id = ncfg.get("database_id")
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"), "").strip()
    if not db_id or not token:
        log.info("Notion skipped (missing database_id or %s)", ncfg.get("token_env", "NOTION_TOKEN"))
        return {"existing": 0, "inserted": 0, "changed": 0}

    headers = _headers(token)
    write_enabled = bool(ncfg.get("write", True)) and not dry_run
    rows = _query_all(db_id, headers)

    known_urls = set()
    known_titles = set()
    changed_rows = 0
    hashes = state.setdefault("notion_hashes", {})
    today_iso = date.today().isoformat()

    for page in rows:
        props = page.get("properties", {})
        title = _plain(props.get("Programme", {})) or ""
        url = _plain(props.get("Official Link", {}))
        status = _plain(props.get("Status", {})) or ""
        if url:
            known_urls.add(_canon_url(url))
        if title:
            known_titles.add(title.strip().lower())

        if not url or status in ("Completed", "Archived"):
            continue
        html_text = http.get(url, as_json=False)
        if not html_text:
            continue
        text = clean_text(html_text, 15000)
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        prev = hashes.get(url)
        page_changed = prev is not None and prev != h
        hashes[url] = h
        if page_changed:
            changed_rows += 1

        m = _DEADLINE_RE.search(text)
        evidence = m.group(0)[:200] if m else ""
        patch_props = {
            "Bot: Last Checked": {"date": {"start": today_iso}},
            "Bot: Page Changed": {"checkbox": page_changed},
        }
        if evidence and not _plain(props.get("Bot: Evidence", {})):
            patch_props["Bot: Evidence"] = _rt(evidence, 200)

        if write_enabled:
            r = requests.patch(
                f"{API}/pages/{page['id']}",
                headers=headers,
                json={"properties": patch_props},
                timeout=20,
            )
            if r.status_code != 200:
                log.warning("Notion patch failed for %s (%d)", title, r.status_code)

    inserted = 0
    max_insert = int(ncfg.get("max_per_run", 10))
    for item in new_finds:
        if inserted >= max_insert:
            break
        u_canon = _canon_url(item.get("url"))
        t_low = (item.get("programme") or "").strip().lower()
        if (u_canon and u_canon in known_urls) or (t_low and t_low in known_titles):
            continue

        props = {
            "Programme": {"title": [{"text": {"content": _no_em_dash(item["programme"])[:200]}}]},
            "Organization": _rt(item.get("organization", ""), 200),
            "Lead / PI": _rt(item.get("lead_or_pi", "TBD"), 200),
            "Type": {"select": {"name": _type_bucket(item.get("type", ""))}},
            "Research Areas": _rt(item.get("research_areas", ""), 500),
            "Cost / Funding": {"select": {"name": _cost_bucket(item.get("cost_or_funding", ""))}},
            "Target Output": _rt(item.get("target_output", ""), 200),
            "Official Link": {"url": item["url"]},
            "Deadline Notes": _rt(item.get("deadline", "See listing"), 200),
            "Status": {"select": {"name": "Inbox"}},
            "Found Via": {"select": {"name": item.get("found_via", "Bot")}},
            "Date Found": {"date": {"start": today_iso}},
            "Notes": _rt(item.get("note", ""), 1000),
            "Bot: Fit": _rt(f"{item.get('score', '?')}/10: {item.get('why_fit', '')}", 300),
        }
        if write_enabled:
            r = requests.post(
                f"{API}/pages",
                headers=headers,
                json={"parent": {"database_id": db_id}, "properties": props},
                timeout=20,
            )
            if r.status_code == 200:
                inserted += 1
                if u_canon:
                    known_urls.add(u_canon)
                if t_low:
                    known_titles.add(t_low)
            else:
                log.warning("Notion insert failed for %s (%d): %s", item["programme"], r.status_code, r.text[:200])
        else:
            log.info("Notion dry-run insert: %s (%s)", item["programme"], item["url"])
            inserted += 1

    summary = f"Research Hunt ({today_iso}): checked {len(rows)} Notion rows ({changed_rows} changed), inserted {inserted} new programmes."
    _log_activity(ncfg.get("log_page", ""), headers, summary, dry_run or not write_enabled)
    return {"existing": len(rows), "inserted": inserted, "changed": changed_rows}
