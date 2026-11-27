"""Entry point: python -m researchhunt.run  (run from the search/ folder).

Pipeline: curated watchlist + RSS feeds + public Telegram channels -> dedupe
-> keyword filter -> free LLM review -> RESEARCH_PROGRAMMES_TRACKER.csv
-> Notion Research Programmes DB -> weekly email digest.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import feedparser
import yaml

from jobhunt import emailer
from jobhunt.http import get
from jobhunt.llm import Scorer
from jobhunt.models import Job, clean_text, parse_date
from researchhunt import notion
from scholarhunt import telegram

SEARCH = Path(__file__).resolve().parents[1]
ROOT = SEARCH.parent
CFG = SEARCH / "research" / "config.yaml"
STATE = Path(__file__).resolve().parent / "state" / "seen.json"
TRACKER = ROOT / "RESEARCH_PROGRAMMES_TRACKER.csv"
COLS = [
    "Programme",
    "Organization",
    "Lead_Or_PI",
    "Type",
    "Research_Areas",
    "Cost_Or_Funding",
    "Target_Output",
    "Deadline",
    "Status",
    "Date_Found",
    "Contact_Or_URL",
    "Note",
]
log = logging.getLogger("researchhunt")
E = html.escape

PROMPT = """CANDIDATE
{profile}

RESEARCH PROGRAMME / COLLABORATION POSTING
Title: {title}
Source: {source}
Description (truncated):
{description}

Judge this for Ryan's goal: finding and enrolling in organized remote research programmes or international research collaborations to learn formal research methodology, co-author peer-reviewed papers, and strengthen his Fall 2028 PhD application.

Return ONE JSON object with exactly these keys:
{{
 "score": integer 0-10 (9-10 = free or stipended remote research programme/fellowship open internationally with mentorship and publication path in AI/ML/CV/Robotics/Edge AI; 7-8 = strong unpaid or APC-supported collaboration; <=4 = requires paid tuition/fees with no clear full scholarship, or on-site only, or off-topic),
 "status": "APPLY" (open now, remote/international, free or stipended) | "WATCH" (fits well, rolling or opens later, or needs verification) | "SKIP" (off-topic, on-site only, or paid-only without scholarship),
 "programme": "concise programme name",
 "organization": "host organization or collective",
 "lead_or_pi": "lead researcher or 'Team / Mentors'",
 "type": "remote_research_collab" | "structured_programme" | "mentorship_fellowship" | "research_internship",
 "research_areas": "comma-separated relevant areas",
 "cost_or_funding": "Free" | "Stipend" | "APC Support" | "Scholarship / Waiver" | "Paid (Low Priority)" | "Unclear",
 "target_output": "e.g. NeurIPS/ICML workshop, Q1/Q2 journal, arXiv preprint",
 "deadline": "deadline as stated, else 'Rolling' or 'Check page'",
 "why_fit": "one concise sentence, max 160 chars, zero em dashes"
}}
Rules:
- Payment policy: if a programme requires participant payment/tuition without a clear full scholarship or fee waiver, it is LOW PRIORITY (score <= 4, cost_or_funding = 'Paid (Low Priority)').
- Prefer remote-friendly programmes open to applicants in Ghana / worldwide.
- Do not use em dashes anywhere."""


def _no_em_dash(s: str) -> str:
    return re.sub(r"\s*\u2014\s*", ", ", s or "").strip()


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": {}, "pages": {}, "notion_hashes": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def fetch_feeds(cfg: dict) -> list[Job]:
    inc = re.compile("|".join(cfg["filters"]["include"]), re.I)
    exc = re.compile("|".join(cfg["filters"]["exclude"]), re.I)
    out = []
    for f in cfg.get("feeds") or []:
        text = get(f["url"], as_json=False)
        if not text:
            log.info("  feed %s: unreachable", f["name"])
            continue
        n = 0
        for e in feedparser.parse(text).entries:
            title = clean_text(e.get("title"))
            body = clean_text(
                (e.get("content") or [{}])[0].get("value") if e.get("content") else e.get("summary"),
                5000,
            )
            blob = f"{title} {body}"
            if not inc.search(blob) or exc.search(title):
                continue
            out.append(
                Job(
                    source=f"rss:{f['name']}",
                    company=f["name"],
                    title=title,
                    location="Remote / See listing",
                    url=e.get("link", ""),
                    description=body,
                    posted=parse_date(e.get("published")),
                )
            )
            n += 1
        log.info("  feed %s: %d relevant", f["name"], n)

    tcfg = cfg.get("telegram") or {}
    if tcfg.get("channels"):
        posts = [
            j
            for j in telegram.fetch(tcfg)
            if inc.search(f"{j.title} {j.description}") and not exc.search(j.title)
        ]
        if posts:
            log.info("  telegram: %d relevant", len(posts))
        out.extend(posts)
    return out


_WATCH_TRIGGERS = re.compile(
    r"apply|application|cohort|fellowship|collaborat|open|mentor|research|scholar", re.I
)


def fetch_watchlist(cfg: dict, state: dict) -> tuple[list[Job], list[dict]]:
    """Fetch curated research programme pages. Score on first sight or when changed."""
    items, alerts = [], []
    pages = state.setdefault("pages", {})
    for w in cfg.get("watchlist") or []:
        url = w["url"]
        html_text = get(url, as_json=False)
        if not html_text:
            log.info("  watchlist %s: unreachable", w["name"])
            continue
        text = clean_text(html_text, 6000)
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()
        prev = pages.get(url)
        changed = prev is not None and prev != h
        pages[url] = h
        if prev is None or (changed and _WATCH_TRIGGERS.search(text)):
            items.append(
                Job(
                    source="watchlist",
                    company=w.get("org", w["name"]),
                    title=w["name"],
                    location="Remote",
                    url=url,
                    description=text,
                    posted=None,
                )
            )
            if changed:
                alerts.append({"name": w["name"], "url": url, "detail": "Programme page updated"})
        log.info("  watchlist %s: %s", w["name"], "new" if prev is None else ("changed" if changed else "unchanged"))
    return items, alerts


def append_tracker(finds: list[dict]) -> int:
    existing_urls, existing_names = set(), set()
    rows = []
    if TRACKER.exists():
        with TRACKER.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                rows.append(r)
                if r.get("Contact_Or_URL"):
                    for part in r["Contact_Or_URL"].split("|"):
                        u = part.strip().rstrip("/").lower()
                        if u.startswith("http"):
                            existing_urls.add(u)
                if r.get("Programme"):
                    existing_names.add(r["Programme"].strip().lower())

    added = 0
    today_iso = date.today().isoformat()
    for item in finds:
        u_canon = (item.get("url") or "").strip().rstrip("/").lower()
        p_low = (item.get("programme") or "").strip().lower()
        if (u_canon and u_canon in existing_urls) or (p_low and p_low in existing_names):
            continue
        rows.append(
            {
                "Programme": _no_em_dash(item["programme"]),
                "Organization": _no_em_dash(item.get("organization", "")),
                "Lead_Or_PI": _no_em_dash(item.get("lead_or_pi", "TBD")),
                "Type": item.get("type", "structured_programme"),
                "Research_Areas": _no_em_dash(item.get("research_areas", "")),
                "Cost_Or_Funding": _no_em_dash(item.get("cost_or_funding", "Unclear")),
                "Target_Output": _no_em_dash(item.get("target_output", "")),
                "Deadline": _no_em_dash(item.get("deadline", "Check page")),
                "Status": item.get("status", "WATCH"),
                "Date_Found": today_iso,
                "Contact_Or_URL": item["url"],
                "Note": _no_em_dash(f"[{item.get('score', '?')}/10] {item.get('why_fit', '')}"),
            }
        )
        if u_canon:
            existing_urls.add(u_canon)
        if p_low:
            existing_names.add(p_low)
        added += 1

    if added:
        with TRACKER.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=COLS)
            writer.writeheader()
            writer.writerows(rows)
    return added


def build_email(picks: list[dict], alerts: list[dict], stats: str) -> tuple[str, str, str]:
    subject = f"Research Programmes Digest ({date.today().isoformat()}): {len(picks)} picks"
    lines_html = [f"<h2>Remote Research Programmes ({E(date.today().isoformat())})</h2>", f"<p>{E(stats)}</p>"]
    lines_txt = [f"Remote Research Programmes ({date.today().isoformat()})", stats, ""]

    if picks:
        lines_html.append("<h3>New Qualifying Programmes</h3><ul>")
        for p in picks:
            lines_html.append(
                f"<li><b>{E(p['programme'])}</b> ({E(p['organization'])}) - "
                f"<b>{p['score']}/10</b> [{E(p['cost_or_funding'])}] | Deadline: {E(p['deadline'])}<br>"
                f"{E(p['why_fit'])}<br><a href=\"{E(p['url'])}\">{E(p['url'])}</a></li>"
            )
            lines_txt.append(
                f"- {p['programme']} ({p['organization']}) [{p['score']}/10, {p['cost_or_funding']}, {p['deadline']}]: "
                f"{p['why_fit']} {p['url']}"
            )
        lines_html.append("</ul>")

    if alerts:
        lines_html.append("<h3>Watchlist Changes</h3><ul>")
        for a in alerts:
            lines_html.append(f"<li><b>{E(a['name'])}</b>: {E(a['detail'])} (<a href=\"{E(a['url'])}\">link</a>)</li>")
            lines_txt.append(f"- Watchlist change: {a['name']} ({a['detail']}) {a['url']}")
        lines_html.append("</ul>")

    return subject, "\n".join(lines_html), "\n".join(lines_txt)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    profile = (SEARCH / "profile.md").read_text(encoding="utf-8")
    state = load_state()
    seen = state.setdefault("seen", {})

    log.info("Fetching research programme sources...")
    feed_items = fetch_feeds(cfg)
    watch_items, alerts = fetch_watchlist(cfg, state)
    candidates = []
    for j in watch_items + feed_items:
        key = j.url.strip().rstrip("/").lower()
        if key and key not in seen:
            candidates.append(j)

    log.info("New candidates to score: %d", len(candidates))
    max_calls = int(cfg["run"].get("max_llm_calls", 30))
    scorer = Scorer(profile, max_calls)
    tracker_min = int(cfg["run"].get("tracker_min_score", 6))
    pick_min = int(cfg["run"].get("pick_min_score", 7))

    qualifying = []
    for j in candidates[:max_calls]:
        prompt = PROMPT.format(
            profile=profile,
            title=j.title,
            source=j.source,
            description=j.description[:4000],
        )
        res = None
        if scorer.available:
            msgs = [
                {"role": "system", "content": "You are a strict research-programme advisor. Reply with one JSON object only."},
                {"role": "user", "content": prompt},
            ]
            for p in scorer.usable():
                if p["name"] in scorer.dead:
                    continue
                scorer.calls += 1
                res = scorer._parse(scorer._chat(p, msgs))
                if res:
                    break
        if not res:
            log.info("  LLM unavailable for %s", j.title)
            continue
        score = int(res.get("score", 0))
        status = str(res.get("status", "SKIP")).upper()
        cost = str(res.get("cost_or_funding", "Unclear"))
        if "paid (low" in cost.lower() and score > 4:
            score = 4
        seen[j.url.strip().rstrip("/").lower()] = {
            "title": j.title,
            "score": score,
            "date": date.today().isoformat(),
        }
        log.info("  [%d/10 %s | %s] %s", score, status, cost, j.title)
        if score >= tracker_min and status != "SKIP":
            qualifying.append(
                {
                    "programme": res.get("programme") or j.title,
                    "organization": res.get("organization") or j.company,
                    "lead_or_pi": res.get("lead_or_pi") or "TBD",
                    "type": res.get("type") or "structured_programme",
                    "research_areas": res.get("research_areas") or "AI/ML",
                    "cost_or_funding": cost,
                    "target_output": res.get("target_output") or "Peer-reviewed paper / arXiv",
                    "deadline": res.get("deadline") or "Check page",
                    "status": "APPLY" if status == "APPLY" else "WATCH",
                    "url": j.url,
                    "why_fit": _no_em_dash(res.get("why_fit") or ""),
                    "score": score,
                    "found_via": "Watchlist" if j.source == "watchlist" else ("Telegram" if j.source.startswith("telegram") else "RSS"),
                    "note": _no_em_dash(f"[{score}/10] {res.get('why_fit') or ''}"),
                }
            )

    qualifying.sort(key=lambda x: x["score"], reverse=True)
    added_csv = 0 if args.dry_run else append_tracker(qualifying)
    notion_stats = notion.sync(cfg, qualifying, state, dry_run=args.dry_run)

    picks = [q for q in qualifying if q["score"] >= pick_min][: int(cfg["run"].get("picks_max", 5))]
    stats = (
        f"Scored {min(len(candidates), max_calls)} candidates | "
        f"CSV +{added_csv} | Notion +{notion_stats['inserted']} "
        f"(checked {notion_stats['existing']}, {notion_stats['changed']} changed)"
    )

    if not args.dry_run:
        save_state(state)
        to = (cfg.get("alerts") or {}).get("email")
        if to and (picks or alerts):
            subj, html_body, text_body = build_email(picks, alerts, stats)
            emailer.send(subj, html_body, text_body, to)

    log.info("Done. %s", stats)


if __name__ == "__main__":
    main()
