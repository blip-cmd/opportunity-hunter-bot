"""Read the team's Notion databases, check each row's official page, write back.

For every row in scope the official link is opened, a deadline is looked for
(and kept only with a quote that really appears on the page), and the page text
is fingerprinted so a change can be reported next week. Lab pages also raise an
alert when they change and mention openings.

Write-back (only when the config says `write: true`, never on a dry run):
- the bot's own columns (deadline found, evidence, last checked, page changed,
  fit) are set when their value differs from what is already there;
- an empty human deadline is filled with a found date (found dates always rest
  on a quote that is on the official page); a human date that disagrees is left
  alone, and the notes column gets the configured "confirm" note if it is empty;
- a yearly row in one of the configured statuses moves to the "awaiting" status
  once every deadline known for it has passed;
- nothing else is touched: no other status, no notes with text in them, no page
  bodies, nothing is deleted;
- each run appends one entry to the configured log page.

Everything specific to a workspace lives in the private config under
`notion:` (database IDs, property names, people and their consent). This module
holds no names, IDs or addresses. The token comes from the NOTION_TOKEN secret.

Rows are in scope when they are tagged with a person whose consent is "yes",
with the shared label, or with nobody. Rows tagged only with people who have
not consented are skipped entirely. Fit lines are written only for people whose
consent is "yes".
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import date
from pathlib import Path

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

FIT_SYSTEM = """You judge how well a graduate programme or scholarship fits each person described. Be strict and honest,
never invent facts, and say so when eligibility is unclear. Reply with ONE JSON object only, no markdown."""

FIT_TMPL = """OPPORTUNITY: {title} ({db})
DETAILS FROM THE TEAM'S DATABASE:
{details}
TEXT FROM THE OFFICIAL PAGE (may be empty):
{page}

PEOPLE:
{people}

Return JSON: {{"fits": [{{"person": "name exactly as given", "score": integer 0-10, "reason": "one short sentence, max 150 characters"}}]}}
One entry per person. 10 = funded, open to the person's nationality, matches their focus and timeline; 3 or less = poor fit or likely ineligible.
Write plain sentences with commas, no dashes between clauses."""

# Roles of the bot columns and the Notion type each one has.
BOT_TYPES = {"deadline_found": "date", "evidence": "rich_text", "last_checked": "date",
             "page_changed": "checkbox", "fit": "rich_text"}
FIT_PAGE_CHARS = 3000


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Notion-Version": VERSION, "Content-Type": "application/json"}


def _no_em_dash(s: str) -> str:
    """Workspace rule: no em dashes in anything the bot writes (quotes included)."""
    return re.sub("\\s*\N{EM DASH}\\s*", ", ", s or "").strip()


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
    headers = _headers(token)
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
                "id": p["id"],
                "props": props,
                "dbcfg": db,
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
        r.update(found=None, changed=False, openings=False, read=False, page_hash="", excerpt="", quote="")
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
        r.update(read=True, page_hash=h, excerpt=text[:FIT_PAGE_CHARS])
        if r["openings"]:
            r["quote"] = next((s.strip()[:200] for s in _SENTENCE.split(text)
                               if _OPENINGS.search(s) and len(s.strip()) <= 400), "")
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


# ---------------------------------------------------------------- write-back

def _bot_names(db: dict) -> set[str]:
    return {v for v in (db.get("bot") or {}).values() if v}


def people_profiles(ncfg: dict, base: Path) -> dict[str, str]:
    """Consenting people only: their focus line plus their profile file, if one is set."""
    out = {}
    for p in ncfg.get("people") or []:
        if p["name"] not in consenting(ncfg):
            continue
        text = f"Focus: {p.get('focus', '')}"
        if p.get("profile"):
            try:
                text += "\n" + (base / p["profile"]).read_text(encoding="utf-8")[:4000]
            except OSError:
                log.info("  Notion fit: a profile file is missing, using the focus line only")
        out[p["name"]] = text
    return out


def _details(r: dict) -> str:
    """The row's human columns, minus the ones the bot itself may change."""
    db = r["dbcfg"]
    skip = _bot_names(db) | {db.get(k) for k in ("title", "people", "status", "deadline", "notes")}
    lines = []
    for k, v in sorted(r["props"].items()):
        if k in skip or v in (None, "", [], False):
            continue
        lines.append(f"{k}: {', '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines) or "(none)"


def score_fit(rows: list[dict], scorer, state: dict, profiles: dict[str, str], max_calls: int) -> tuple[dict, dict]:
    """Fit line per row for every consenting person. Re-asked only when the row, page or profiles change."""
    memo = state.setdefault("notion_fit", {})
    fits, stats = {}, {"fit_asked": 0, "fit_reused": 0}
    if not profiles:
        return fits, stats
    psig = hashlib.sha1(json.dumps(profiles, sort_keys=True).encode()).hexdigest()
    people = "\n\n".join(f"{name}:\n{text}" for name, text in profiles.items())
    for r in rows:
        if not (r["dbcfg"].get("bot") or {}).get("fit"):
            continue
        details = _details(r)
        key = hashlib.sha1(json.dumps([r["title"], details, r.get("page_hash", ""), psig]).encode()).hexdigest()
        prev = memo.get(r["notion_url"]) or {}
        if prev.get("key") == key and prev.get("fit"):
            fits[r["notion_url"]] = prev["fit"]
            stats["fit_reused"] += 1
            continue
        if not scorer or stats["fit_asked"] >= max_calls:
            continue
        stats["fit_asked"] += 1
        out = scorer.ask(FIT_SYSTEM, FIT_TMPL.format(title=r["title"], db=r["db"], details=details,
                                                     page=r.get("excerpt") or "(not readable)", people=people))
        lines = []
        for f in (out or {}).get("fits") or []:
            name = str(f.get("person", "")).strip()
            if name not in profiles:          # never write a line for someone who has not consented
                continue
            try:
                score = max(0, min(10, int(round(float(f.get("score"))))))
            except (TypeError, ValueError):
                continue
            lines.append(f"{name} {score}/10: {_no_em_dash(str(f.get('reason', '')))[:160]}")
        if lines:
            fits[r["notion_url"]] = "\n".join(lines)
            memo[r["notion_url"]] = {"key": key, "fit": fits[r["notion_url"]]}
    return fits, stats


def _same(kind: str, cur, new) -> bool:
    if kind == "date":
        return (cur or "")[:10] == (new or "")[:10]
    if kind == "checkbox":
        return bool(cur) == bool(new)
    return (cur or "") == (new or "")


def _payload(kind: str, value) -> dict:
    if kind == "date":
        return {"date": {"start": value} if value else None}
    if kind == "checkbox":
        return {"checkbox": bool(value)}
    if kind == "status":
        return {"status": {"name": value}}
    return {"rich_text": [_text(value)] if value else []}


def plan_writes(rows: list[dict], ncfg: dict, today: date, fits: dict) -> list[dict]:
    """What would change in each row. Only values that differ from Notion are included."""
    note = ncfg.get("confirm_note", "Confirm with Guideline")
    plans = []
    for r in rows:
        db, cur = r["dbcfg"], r["props"]
        bot = db.get("bot") or {}
        props, changes, routine = {}, [], []

        def put(name, kind, value, label, minor=False):
            if kind == "rich_text":
                value = _no_em_dash(value)[:2000]   # compare what Notion would actually hold
            if not name or name not in cur or _same(kind, cur[name], value):
                return
            props[name] = _payload(kind, value)
            (routine if minor else changes).append(label)

        if r.get("read"):
            put(bot.get("last_checked"), "date", today.isoformat(), "Last Checked", minor=True)
            put(bot.get("page_changed"), "checkbox", r["changed"],
                "Page Changed ticked" if r["changed"] else "Page Changed cleared", minor=not r["changed"])
        found = r.get("found")
        if found:
            put(bot.get("deadline_found"), "date", found["deadline"], f"Deadline Found {found['deadline']}")
            put(bot.get("evidence"), "rich_text", found["evidence"], "Evidence")
        elif r.get("openings") and r.get("quote"):
            put(bot.get("evidence"), "rich_text", r["quote"], "Evidence (openings)")

        human = (r.get("human_deadline") or "")[:10]
        if found and db.get("deadline"):
            if not human:
                put(db["deadline"], "date", found["deadline"], f"{db['deadline']} filled with {found['deadline']}")
                human = found["deadline"]
            elif human != found["deadline"] and db.get("notes") and not cur.get(db["notes"]):
                put(db["notes"], "rich_text", note,
                    f"{db['notes']} set to \"{note}\" (human date {human}, page says {found['deadline']})")

        cyc = db.get("cycle") or {}
        if cyc and cur.get(cyc.get("property")) == cyc.get("value") and r["status"] in (db.get("move_from") or []):
            known = [d for d in (human, (found or {}).get("deadline"), (cur.get(bot.get("deadline_found", "")) or "")[:10]) if d]
            if known and max(known) < today.isoformat():
                put(db.get("status"), "status", db["awaiting"],
                    f"Status moved from {r['status']} to {db['awaiting']} (deadline {max(known)} passed)")

        if r["notion_url"] in fits:
            put(bot.get("fit"), "rich_text", fits[r["notion_url"]], "Fit")

        if props:
            plans.append({"row": r, "props": props, "changes": changes, "routine": routine})
    return plans


def describe(plans: list[dict]) -> list[str]:
    """One entry per row, with the values, for the private log."""
    out = []
    for p in plans:
        vals = []
        for name, v in p["props"].items():
            kind = next(iter(v))
            val = v[kind]
            if kind == "rich_text":
                val = "".join(t["text"]["content"] for t in val).replace("\n", " | ")
            elif kind == "date":
                val = (val or {}).get("start")
            elif kind == "status":
                val = val["name"]
            vals.append(f"{name} = {val!r}")
        r = p["row"]
        out.append(f"  {r['db']} | {r['title'][:60]} | {r['notion_url']}\n      " + "\n      ".join(vals))
    return out


def _call(method: str, url: str, token: str, body: dict) -> tuple[int, dict]:
    """One call at a polite pace, retried on 429. Returns (HTTP status, JSON); status 0 on a network error."""
    for attempt in range(3):
        time.sleep(0.4)   # Notion allows about 3 requests a second
        try:
            r = requests.request(method, url, headers=_headers(token), json=body, timeout=30)
        except requests.RequestException as e:
            log.info("  Notion: request failed (%s)", type(e).__name__)
            return 0, {}
        if r.status_code == 429 and attempt < 2:
            wait = r.headers.get("retry-after", "2")
            time.sleep(min(30, int(wait) if wait.isdigit() else 2))
            continue
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, {}
    return 429, {}


def _request(method: str, url: str, token: str, body: dict) -> int:
    return _call(method, url, token, body)[0]


def apply_writes(plans: list[dict], ncfg: dict) -> tuple[list[dict], int]:
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    if not token:
        log.info("Notion: no token set, nothing written")
        return [], len(plans)
    done, failed = [], 0
    for p in plans:
        code = _request("PATCH", f"{API}/pages/{p['row']['id']}", token, {"properties": p["props"]})
        if code == 200:
            done.append(p)
        else:
            failed += 1
            # 403 usually means the integration lacks the "Update content" capability.
            log.info("  Notion: write to %s returned %s", p["row"]["title"][:50], code)
    return done, failed


def _text(s: str) -> dict:
    return {"type": "text", "text": {"content": _no_em_dash(s)[:2000]}}


def activity_entry(plans: list[dict], failed: int, nstats: dict, fstats: dict, today: date, dry: bool) -> list[dict]:
    """Blocks for one Bot Activity Log entry: date, counts, links to the rows with real changes."""
    real = [p for p in plans if p["changes"]]

    def count(word: str) -> int:
        return sum(any(c.startswith(word) for c in p["changes"]) for p in real)

    fills = sum(any(" filled with " in c for c in p["changes"]) for p in real)
    notes = sum(any(" set to " in c for c in p["changes"]) for p in real)
    summary = (f"Rows checked {nstats.get('checked', 0)}, unreadable {nstats.get('unreadable', 0)}, "
               f"no link {nstats.get('no_link', 0)}. Rows written {len(plans)}: {len(real)} with changes (listed below) "
               f"and {len(plans) - len(real)} with only Last Checked or Page Changed updated. "
               f"Deadline Found {count('Deadline Found')}, human deadline filled {fills}, "
               f"\"Confirm with Guideline\" notes {notes}, moved to Awaiting Next Cycle {count('Status moved')}, "
               f"Fit lines {count('Fit')} (AI calls {fstats.get('fit_asked', 0)}), Page Changed ticked "
               f"{count('Page Changed ticked')}. Failed writes {failed}. Nothing deleted, no existing notes changed.")
    title = f"{today.isoformat()} Scholar Hunt weekly run (bot)" + (" DRY RUN PREVIEW, not written" if dry else "")
    blocks = [{"object": "block", "type": "heading_2", "heading_2": {"rich_text": [_text(title)]}},
              {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_text(summary)]}}]
    for p in real:
        rt = [{"type": "mention", "mention": {"type": "page", "page": {"id": p["row"]["id"]}}},
              _text(": " + ", ".join(p["changes"]))]
        blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt}})
    return blocks


def entry_text(blocks: list[dict]) -> list[str]:
    out = []
    for b in blocks:
        s = "".join(t["text"]["content"] if t["type"] == "text" else "[row link]" for t in b[b["type"]]["rich_text"])
        out.append(("- " if b["type"] == "bulleted_list_item" else "") + s)
    return out


def append_log(ncfg: dict, blocks: list[dict]) -> bool:
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    page = ncfg.get("log_page")
    if not token or not page:
        log.info("Notion: no log page configured, activity entry not written")
        return False
    blocks = [{"object": "block", "type": "divider", "divider": {}}] + blocks
    for i in range(0, len(blocks), 100):
        code = _request("PATCH", f"{API}/blocks/{page}/children", token, {"children": blocks[i:i + 100]})
        if code != 200:
            log.info("  Notion: activity log append returned %s", code)
            return False
    return True


# ---------------------------------------------------------------- Bot Finds
# A staging table for new opportunities. The bot adds finds there, humans give feedback,
# and "Promote" copies a find into the main table as Inbox. Nothing is ever deleted.

def _norm_url(u: str) -> str:
    u = re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower())
    return re.split(r"[?#]", u)[0].rstrip("/")


def _name_key(s: str) -> str:
    return _norm(re.sub(r"\([^)]*\)", " ", s or ""))


def _known(*row_lists: list[dict]) -> tuple[set, list]:
    urls, names = set(), []
    for rows in row_lists:
        for r in rows:
            for u in r.get("urls") or []:
                if u:
                    urls.add(_norm_url(u))
            k = _name_key(r.get("title", ""))
            if k:
                names.append(k)
    return urls, names


def _is_dup(urls: list[str], name: str, known_urls: set, known_names: list) -> bool:
    if any(_norm_url(u) in known_urls for u in urls if u):
        return True
    k = _name_key(name)
    if not k:
        return False
    # Same name, or one contains the other when it is long enough to be specific.
    return any(k == n or (min(len(k), len(n)) >= 15 and (k in n or n in k)) for n in known_names)


def fetch_finds(ncfg: dict) -> list[dict]:
    """All rows of the Bot Finds table, with the columns named in config."""
    fcfg = ncfg.get("finds") or {}
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    if not (fcfg.get("id") and token):
        return []
    pages = _query(fcfg["id"], token)
    if pages is None:
        log.info("  Notion Bot Finds: unreadable")
        return []
    pc, rows = fcfg["props"], []
    for p in pages:
        props = {k: _plain(v) for k, v in p.get("properties", {}).items()}
        rows.append({"id": p["id"], "notion_url": p.get("url", ""), "props": props,
                     "title": props.get(pc["title"]) or "",
                     "urls": [props.get(pc["link"]), props.get(pc["found_via"])],
                     "feedback": props.get(pc["feedback"]), "type": props.get(pc["type"])})
    log.info("  Notion Bot Finds: %d rows", len(rows))
    return rows


def feedback_note(finds: list[dict], ncfg: dict, limit: int = 15) -> str:
    """Names humans liked or rejected, appended to the scoring prompt so the next feed review learns."""
    opt = (ncfg.get("finds") or {}).get("options", {})
    good = [f["title"] for f in finds if f["feedback"] in opt.get("learn_good", [])][-limit:]
    bad = [f["title"] for f in finds if f["feedback"] in opt.get("learn_bad", [])][-limit:]
    if not (good or bad):
        return ""
    return ("\n\nTEAM FEEDBACK ON EARLIER FINDS (judge similar items the same way)\n"
            f"Liked: {'; '.join(good) or 'none yet'}\nRejected as not relevant or wrong: {'; '.join(bad) or 'none yet'}\n")


def pick_finds(good: list, ncfg: dict) -> list[dict]:
    """Turn the week's best feed reviews into candidate finds."""
    fcfg = ncfg.get("finds") or {}
    min_score = fcfg.get("min_score", 7)
    out = []
    for j in good:
        l = j.llm
        if l.get("model") == "none" or l["status"] == "SKIP" or l["score"] < min_score:
            continue
        dl = str(l.get("deadline", "")).strip()
        stated = f"Posting says: {dl}. " if dl and dl.lower() not in ("unknown", "rolling", "not stated") else ""
        cover = {"fully_funded": "Fully funded (per the posting)", "partial": "Partial funding (per the posting)"}.get(l.get("funding"), "")
        level = {"phd": ["PhD"], "msc": ["Masters"]}.get(l.get("level"), [])
        out.append({"title": clean_text(j.title)[:150], "type": fcfg["options"]["scheme"], "found_via": j.url,
                    "org": str(l.get("university") or j.company)[:150], "level": level, "coverage": cover,
                    "notes": (stated + (ncfg.get("confirm_note", "Confirm with Guideline")))[:300],
                    # Neutral on purpose: the table is shared, so nothing from anyone's profile goes in it.
                    "why": (f"Scored {l['score']}/10 by the bot on its feed review. "
                            f"{(level[0] + ' level, ') if level else ''}{(cover.lower() + ', ') if cover else ''}"
                            f"host: {str(l.get('university') or j.company)[:80]}.")[:300],
                    "source": j.source, "blockers": str(l.get("blockers", ""))})
    return out


def plan_finds(cands: list[dict], main_rows: list[dict], finds: list[dict], ncfg: dict) -> tuple[list[dict], int]:
    """New finds to add (at most max_per_run) and how many were skipped as already known anywhere."""
    fcfg = ncfg.get("finds") or {}
    urls, names = _known([{"urls": [r["link"]], "title": r["title"]} for r in main_rows], finds)
    new, dups = [], 0
    for c in sorted(cands, key=lambda c: -int(re.search(r"(\d+)/10", c["why"]).group(1))):
        if _is_dup([c["found_via"]], c["title"], urls, names):
            dups += 1
            continue
        if len(new) >= fcfg.get("max_per_run", 5):
            continue
        new.append(c)
        urls.add(_norm_url(c["found_via"]))
        names.append(_name_key(c["title"]))
    return new, dups


def _find_props(c: dict, fcfg: dict) -> dict:
    pc = fcfg["props"]
    props = {pc["title"]: {"title": [_text(c["title"])]},
             pc["type"]: {"select": {"name": c["type"]}},
             pc["found_via"]: {"url": c["found_via"] or None},
             pc["org"]: {"rich_text": [_text(c["org"])] if c["org"] else []},
             pc["level"]: {"multi_select": [{"name": n} for n in c["level"]]},
             pc["coverage"]: {"rich_text": [_text(c["coverage"])] if c["coverage"] else []},
             pc["notes"]: {"rich_text": [_text(c["notes"])]},
             pc["why"]: {"rich_text": [_text(c["why"])]}}
    return props


def _body(lines: list[str]) -> list[dict]:
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_text(s)]}} for s in lines if s]


def create_finds(new: list[dict], ncfg: dict, today: date) -> list[dict]:
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    fcfg, made = ncfg["finds"], []
    for c in new:
        body = {"parent": {"database_id": fcfg["id"]}, "properties": _find_props(c, fcfg),
                "children": _body([f"Found by the weekly bot on {today.isoformat()} in the feed {c['source'].replace('rss:', '')}.",
                                   f"Possible blockers: {c['blockers']}" if c["blockers"] not in ("", "None") else "",
                                   "Not yet checked against an official page. Set Human Feedback to review it."])}
        code, data = _call("POST", f"{API}/pages", token, body)
        if code == 200:
            made.append({**c, "id": data.get("id", "")})
        else:
            log.info("  Notion: could not add find %s (%s)", c["title"][:50], code)
    return made


def _map_level(kind: str, levels: list[str]) -> list[str] | str | None:
    if kind == "Programme":       # Degree Type is a single select with different names
        if not levels:
            return None
        return "Direct PhD" if levels[0] == "PhD" else levels[0]
    return levels


def plan_promotions(finds: list[dict], main_rows: list[dict], ncfg: dict) -> list[dict]:
    """Finds a human marked Promote. Already in a main table means Duplicate instead of a new row."""
    fcfg = ncfg.get("finds") or {}
    opt = fcfg.get("options", {})
    urls, names = _known([{"urls": [r["link"]], "title": r["title"]} for r in main_rows])
    out = []
    for f in finds:
        if f["feedback"] != opt.get("promote"):
            continue
        dup = _is_dup(f["urls"], f["title"], urls, names)
        out.append({"find": f, "duplicate": dup, "target": (fcfg.get("promote_to") or {}).get(f["type"] or opt["scheme"])})
    return out


def _main_props(f: dict, spec: dict, fcfg: dict) -> dict:
    pc, m, fp = fcfg["props"], spec["map"], f["props"]
    levels = fp.get(pc["level"]) or []
    props = {m["title"]: {"title": [_text(f["title"])]},
             m["status"]: {"status": {"name": spec.get("status", "Inbox")}},
             m["people"]: {"multi_select": [{"name": n} for n in (fp.get(pc["people"]) or [fcfg["options"].get("shared", "Shared")])]}}
    link = fp.get(pc["link"]) or fp.get(pc["found_via"])
    if link and m.get("link"):
        props[m["link"]] = {"url": link}
    if fp.get(pc["deadline"]) and m.get("deadline"):
        props[m["deadline"]] = {"date": {"start": fp[pc["deadline"]][:10]}}
    if fp.get(pc["notes"]) and m.get("notes"):
        props[m["notes"]] = {"rich_text": [_text(fp[pc["notes"]])]}
    if fp.get(pc["coverage"]) and m.get("coverage"):
        props[m["coverage"]] = {"rich_text": [_text(fp[pc["coverage"]])]}
    if fp.get(pc["org"]) and m.get("org"):
        props[m["org"]] = {"rich_text": [_text(fp[pc["org"]])]}
    lv = _map_level(f["type"] or "Scheme", levels)
    if lv and m.get("level"):
        props[m["level"]] = {"select": {"name": lv}} if isinstance(lv, str) else {"multi_select": [{"name": n} for n in lv]}
    return props


def apply_promotions(plans: list[dict], ncfg: dict, today: date) -> list[dict]:
    token = os.environ.get(ncfg.get("token_env", "NOTION_TOKEN"))
    fcfg = ncfg["finds"]
    opt, fb = fcfg["options"], fcfg["props"]["feedback"]
    dbs = {d["name"]: d["id"] for d in ncfg.get("databases") or []}
    done = []
    for p in plans:
        f, spec = p["find"], p["target"]
        if p["duplicate"] or not spec or spec["database"] not in dbs:
            result, new_id = opt["duplicate"], ""
        else:
            fp = f["props"]
            body = {"parent": {"database_id": dbs[spec["database"]]}, "properties": _main_props(f, {**spec, "status": ncfg.get("promote_status", "Inbox")}, fcfg),
                    "children": _body([f"Promoted from Bot Finds on {today.isoformat()}. Found via: {fp.get(fcfg['props']['found_via']) or 'n/a'}.",
                                       f"Why the bot picked it: {fp.get(fcfg['props']['why']) or 'n/a'}",
                                       "Not yet verified against an official page: confirm deadline and eligibility before relying on it."])}
            code, data = _call("POST", f"{API}/pages", token, body)
            if code != 200:
                log.info("  Notion: could not promote %s (%s)", f["title"][:50], code)
                continue
            result, new_id = opt["promoted"], data.get("id", "")
        if _request("PATCH", f"{API}/pages/{f['id']}", token, {"properties": {fb: {"select": {"name": result}}}}) == 200:
            done.append({**p, "result": result, "new_id": new_id})
    return done


def describe_finds(new: list[dict], dups: int, promos: list[dict]) -> list[str]:
    out = [f"  add find: {c['title'][:70]} | {c['found_via']} | {c['why'][:80]}" for c in new]
    out.append(f"  skipped as already known: {dups}")
    out += [f"  promote: {p['find']['title'][:70]} -> {'DUPLICATE, will be marked Duplicate' if p['duplicate'] else (p['target'] or {}).get('database', 'no target')}"
            for p in promos]
    return out


def finds_blocks(added: list[dict], dups: int, promoted: list[dict], dry: bool) -> list[dict]:
    """Extra Activity Log lines for the Bot Finds table."""
    word = "Would add" if dry else "Added"
    blocks = [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [_text(
        f"Bot Finds: {word.lower()} {len(added)} new finds, skipped {dups} already known in a main table or in Bot Finds, "
        f"promotions handled {len(promoted)}.")]}}]
    for a in added:
        rt = ([{"type": "mention", "mention": {"type": "page", "page": {"id": a["id"]}}}] if a.get("id") else [_text(a["title"])])
        blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt + [_text(": new find in Bot Finds")]}})
    for p in promoted:
        rt = [{"type": "mention", "mention": {"type": "page", "page": {"id": p["new_id"] or p["find"]["id"]}}},
              _text(": promoted from Bot Finds" if p["new_id"] else ": marked Duplicate, already in a main table")]
        blocks.append({"object": "block", "type": "bulleted_list_item", "bulleted_list_item": {"rich_text": rt}})
    return blocks
