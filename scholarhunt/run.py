"""Entry point: python -m scholarhunt.run  (run from the search/ folder).

Pipeline: scholarship RSS feeds -> dedupe -> keyword filter -> free LLM review
-> rank -> SCHOLARSHIPS_TRACKER.csv + deadline alerts for named programmes
-> one weekly email (only when there is something worth reading).

Reuses jobhunt's HTTP client, LLM provider fallback and Gmail sender, so the
same GitHub secrets (GMAIL_*, GEMINI/GROQ/OPENROUTER keys) power both hunters.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path

import hashlib

import feedparser
import yaml

from jobhunt import emailer
from jobhunt.http import get
from jobhunt.llm import Scorer
from jobhunt.models import Job, clean_text, parse_date
from scholarhunt import notion

SEARCH = Path(__file__).resolve().parents[1]
ROOT = SEARCH.parent
CFG = SEARCH / "scholarships" / "config.yaml"
STATE = Path(__file__).resolve().parent / "state" / "seen.json"
TRACKER = ROOT / "SCHOLARSHIPS_TRACKER.csv"
COLS = ["University", "Lab", "PI", "Level", "Funding", "Deadline", "Status", "MOI_Required",
        "GRE_Required", "AI_Score", "Strategy", "Date_Found", "Note"]
log = logging.getLogger("scholarhunt")
E = html.escape

PROMPT = """CANDIDATE
{profile}

SCHOLARSHIP / PROGRAMME POSTING
Title: {title}
Source: {source}
Description (truncated):
{description}

Judge this for the candidate. Return ONE JSON object with exactly these keys:
{{
 "score": integer 0-10 (10 = fully funded, open to the candidate's nationality, fits his fields and timeline; <=3 = not worth time),
 "status": "APPLY" (open now, eligible, fits) | "STRETCH" (eligible but long shot) | "WATCH" (fits but opens later) | "SKIP",
 "university": "host university or funder",
 "level": "phd" | "msc" | "other",
 "funding": "fully_funded" | "partial" | "unclear",
 "deadline": "deadline as stated, else 'Rolling' or 'Unknown'",
 "english_rule": "what it says about IELTS/TOEFL/MOI, else 'Not stated'",
 "gre_required": "yes" | "no" | "Not stated",
 "why_fit": "one sentence",
 "blockers": "eligibility issues (nationality, NSS timing, work experience); 'None' if none"
}}
Rules: SKIP undergraduate-only, short courses, conferences, non-STEM fields, and anything not open to the candidate's nationality (see profile).
SKIP partial funding unless it is a famous named fellowship. Be strict; do not inflate."""


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"seen": {}, "alerted": {}}


def fetch(cfg: dict) -> list[Job]:
    inc = re.compile("|".join(cfg["filters"]["include"]), re.I)
    exc = re.compile("|".join(cfg["filters"]["exclude"]), re.I)
    out = []
    for f in cfg["feeds"]:
        text = get(f["url"], as_json=False)
        if not text:
            log.info("  %s: unreachable", f["name"])
            continue
        n = 0
        for e in feedparser.parse(text).entries:
            title = clean_text(e.get("title"))
            body = clean_text((e.get("content") or [{}])[0].get("value") if e.get("content") else e.get("summary"), 5000)
            blob = f"{title} {body}"
            if not inc.search(blob) or exc.search(title):
                continue
            out.append(Job(source=f"rss:{f['name']}", company=f["name"], title=title, location="See listing",
                           url=e.get("link", ""), description=body, posted=parse_date(e.get("published"))))
            n += 1
        log.info("  %s: %d relevant", f["name"], n)
    return out


_TRIGGERS = re.compile(r"ph\.?d (position|student|opening)|prospective students|funded|we are (hiring|recruiting)|join (us|the lab)|openings?", re.I)


def fetch_labs(cfg: dict, state: dict) -> list[Job]:
    """Watch lab 'join us' pages. Alert only when the page text changes AND mentions openings."""
    out = []
    hashes = state.setdefault("labs", {})
    for lab in cfg.get("labs") or []:
        page = get(lab["url"], as_json=False)
        if not page:
            log.info("  lab %s: unreachable", lab["name"])
            continue
        text = clean_text(page)
        h = hashlib.sha1(text.encode()).hexdigest()
        changed = hashes.get(lab["url"]) != h
        hashes[lab["url"]] = h
        if changed and _TRIGGERS.search(text):
            # Key includes the content hash so a changed page counts as new.
            out.append(Job(source=f"lab:{lab['name']}", company=lab.get("university", lab["name"]),
                           title=f"Lab page changed: {lab['name']} ({lab.get('university', '')})",
                           location="See listing", url=f"{lab['url']}#v{h[:8]}", description=text[:5000]))
        log.info("  lab %s: %s", lab["name"], "changed" if changed else "unchanged")
    return out


def deadline_alerts(cfg: dict, state: dict, today: date) -> list[dict]:
    """Named programmes: alert once per threshold (e.g. 30/14/7/3 days out)."""
    alerts = []
    for p in cfg.get("programmes", []):
        dl = p.get("deadline")
        if not dl:
            continue
        days = (date.fromisoformat(str(dl)) - today).days
        if days < 0:
            continue
        start = notion.applying_from(cfg.get("notion") or {})
        if start and str(dl)[:10] < start:      # before anyone configured starts applying
            continue
        hit = [t for t in cfg["alerts"]["days_before"] if days <= t]
        if not hit:
            continue
        tag = f"{p['name']}|{dl}|{min(hit)}"
        if tag in state["alerted"]:
            continue
        alerts.append({**p, "days": days, "tag": tag})
    return alerts


def write_tracker(rows: list[dict]) -> int:
    existing = set()
    if TRACKER.exists():
        with TRACKER.open(encoding="utf-8", newline="") as fh:
            existing = {(r.get("University", ""), r.get("Note", "")) for r in csv.DictReader(fh)}
    new = [r for r in rows if (r["University"], r["Note"]) not in existing]
    if not new:
        return 0
    write_header = not TRACKER.exists()
    with TRACKER.open("a", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS)
        if write_header:
            w.writeheader()
        w.writerows(new)
    return len(new)


def build_email(picks: list[Job], alerts: list[dict], stats: dict, notes: list[dict] = ()) -> tuple[str, str, str]:
    today = date.today().isoformat()
    subject = f"Scholarships {today}: {len(picks)} new, {len(alerts)} deadline alert(s)"
    if notes:
        subject += f", {len(notes)} from Notion"
    cards = ""
    md = [f"# Scholarships {today}", ""]
    for a in alerts:
        extra = f'<br><span style="color:#666;font-size:13px">{E(a["note"])}</span>' if a.get("note") else ""
        link = f' &middot; <a href="{E(a["notion_url"])}">Notion</a>' if a.get("notion_url") else ""
        cards += (f'<div style="border-left:4px solid #d93025;padding:8px 12px;margin:0 0 10px">'
                  f'<b>{a["days"]} days left</b>: {E(a["name"])} (deadline {E(str(a["deadline"]))}) '
                  f'<a href="{E(a["url"])}">open</a>{link}{extra}</div>')
        md.append(f"- DEADLINE in {a['days']} days: {a['name']} ({a['deadline']}) {a['url']}"
                  + (f" | {a['note']}" if a.get("note") else ""))
    if notes:
        cards += '<h3 style="margin:16px 0 6px">From the Notion workspace</h3>'
        md.append("## From the Notion workspace")
    for n in notes:
        if n["kind"] == "deadline":
            f = n["found"]
            line = f'Deadline found on the official page: {f["deadline"]}' + (f' ({f["applies_to"]})' if f.get("applies_to") else "")
            quote = f'"{f["evidence"]}"'
        else:
            line, quote = "Lab page changed and mentions openings", ""
        cards += (f'<div style="border-left:4px solid #1a73e8;padding:8px 12px;margin:0 0 10px">'
                  f'<b>{E(n["title"])}</b> <span style="color:#666">({E(n["db"])})</span><br>{E(line)}'
                  + (f'<br><span style="color:#666;font-size:13px">{E(quote)}</span>' if quote else "")
                  + f'<br><a href="{E(n["link"])}">official page</a> &middot; <a href="{E(n["notion_url"])}">Notion row</a></div>')
        md.append(f"- {n['title']} ({n['db']}): {line} {quote} {n['link']}")
    for i, j in enumerate(picks, 1):
        l = j.llm
        cards += f"""<div style="border:1px solid #ddd;border-radius:8px;padding:12px 14px;margin:0 0 12px">
<div style="font-size:13px;color:#666">#{i} &middot; {l.get('score')}/10 &middot; {E(l.get('status',''))} &middot; {E(l.get('level',''))} &middot; {E(l.get('funding',''))}</div>
<div style="font-size:17px;font-weight:600;margin:4px 0">{E(j.title)}</div>
<p style="margin:4px 0"><b>Why you:</b> {E(l.get('why_fit',''))}</p>
<p style="margin:4px 0"><b>Blockers:</b> {E(l.get('blockers',''))}</p>
<p style="margin:4px 0"><b>English:</b> {E(l.get('english_rule',''))} &middot; <b>GRE:</b> {E(l.get('gre_required',''))} &middot; <b>Deadline:</b> {E(l.get('deadline',''))}</p>
<p style="margin:8px 0 0"><a href="{E(j.url)}">Open listing</a></p></div>"""
        md += [f"## {i}. {j.title} ({l.get('score')}/10)", f"- {j.url}", f"- Why: {l.get('why_fit','')}",
               f"- Blockers: {l.get('blockers','')}", f"- Deadline: {l.get('deadline','')}", ""]
    stat = " | ".join(f"{k}: {v}" for k, v in stats.items())
    body = (f'<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px">'
            f"<h2>Scholarship hunt {today}</h2>{cards or '<p>Nothing new cleared the bar.</p>'}"
            f'<p style="color:#888;font-size:12px">{E(stat)}</p></div>')
    md.append(f"_{stat}_")
    return subject, body, "\n".join(md)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no email, no tracker/state writes")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    cfg = yaml.safe_load(CFG.read_text(encoding="utf-8"))
    state = load_state()
    today = date.today()
    run = cfg["run"]

    found = fetch(cfg) + fetch_labs(cfg, state)
    fresh, keys = [], set()
    for j in found:
        if j.key in keys or j.key in state["seen"]:
            continue
        keys.add(j.key)
        fresh.append(j)
    fresh = fresh[: run["max_llm_calls"]]
    log.info("Fetched %d relevant, %d new", len(found), len(fresh))

    profile = (SEARCH / "profile.md").read_text(encoding="utf-8")
    profile += "\n\nGRAD SCHOOL TARGETS\n" + yaml.safe_dump(
        {k: cfg[k] for k in ("candidate", "targets", "intakes", "countries", "english_proficiency")}, sort_keys=False)
    ncfg = cfg.get("notion") or {}
    finds_rows = notion.fetch_finds(ncfg) if ncfg.get("finds") else []
    profile += notion.feedback_note(finds_rows, ncfg)
    scorer = None if a.no_llm else Scorer(profile, run["max_llm_calls"])
    scored = []
    for j in fresh:
        verdict = None
        if scorer and scorer.available:
            msgs = [{"role": "system", "content": "You are a strict graduate-admissions advisor. Reply with one JSON object only."},
                    {"role": "user", "content": PROMPT.format(profile=profile, title=j.title, source=j.source,
                                                               description=j.description[:3500])}]
            for p in scorer.usable():
                if p["name"] in scorer.dead:
                    continue
                scorer.calls += 1
                verdict = scorer._parse(scorer._chat(p, msgs))
                if verdict:
                    verdict["model"] = p["provider"]
                    break
        j.llm = verdict or {"score": 4, "status": "WATCH", "model": "none", "university": j.company,
                            "level": "other", "funding": "unclear", "deadline": "Unknown",
                            "why_fit": "Keyword match only, no AI review this run.", "blockers": "Check listing",
                            "english_rule": "Not stated", "gre_required": "Not stated"}
        log.info("  %s/10 %-8s %s", j.llm["score"], j.llm["status"], j.title[:70])
        scored.append(j)

    good = sorted((j for j in scored if j.llm["status"] != "SKIP" and j.llm["score"] >= run["tracker_min_score"]),
                  key=lambda j: j.llm["score"], reverse=True)
    picks = [j for j in good if j.llm["score"] >= run["pick_min_score"]][: run["picks_max"]]
    alerts = deadline_alerts(cfg, state, today)

    notes, nstats, nrows, plans, fstats = [], {}, [], [], {}
    new_finds, find_dups, promos = [], 0, []
    if ncfg:
        log.info("Notion%s:", "" if ncfg.get("write") else " (read only)")
        main_rows = notion.fetch_rows(ncfg)
        nrows = notion.in_scope(main_rows, ncfg)
        if ncfg.get("write") and ncfg.get("finds"):
            new_finds, find_dups = notion.plan_finds(notion.pick_finds(good, ncfg), main_rows, finds_rows, ncfg)
            promos = notion.plan_promotions(finds_rows, main_rows, ncfg)
            log.info("Bot Finds %s:\n%s", "planned, NOT sent (dry run)" if a.dry_run else "to send",
                     "\n".join(notion.describe_finds(new_finds, find_dups, promos)))
        usable = scorer if scorer and scorer.available else None
        nstats = notion.check(nrows, usable, state, today, ncfg.get("max_llm_calls", 30))
        alerts += notion.deadline_alerts(nrows, state, today, cfg["alerts"]["days_before"], ncfg)
        notes = notion.news(nrows, state, ncfg)
        if ncfg.get("write"):
            fits, fstats = notion.score_fit(nrows, usable, state, notion.people_profiles(ncfg, SEARCH),
                                            ncfg.get("fit_max_calls", 40))
            plans = notion.plan_writes(nrows, ncfg, today, fits)
            log.info("Notion writes %s: %d rows (%d with changes)", "planned, NOT sent (dry run)" if a.dry_run else "to send",
                     len(plans), sum(bool(p["changes"]) for p in plans))
            log.info("\n".join(notion.describe(plans)) or "  none")

    stats = {"relevant": len(found), "new": len(fresh),
             "LLM": ",".join(sorted({j.llm.get("model", "?") for j in scored})) or "none"}
    if ncfg:
        stats.update({"notion rows": len(nrows), **{f"notion {k}": v for k, v in nstats.items()}})
    # A found deadline that already has a reminder this week is shown once, as the reminder.
    reminded = {a.get("notion_url") for a in alerts if a.get("notion_url")}
    shown = [n for n in notes if n["kind"] != "deadline" or n["notion_url"] not in reminded]
    subject, body, md = build_email(picks, alerts, stats, shown)
    print("\n" + md)
    if a.dry_run:
        if ncfg.get("write"):
            entry = notion.activity_entry(plans, 0, nstats, fstats, today, dry=True)
            entry += notion.finds_blocks(new_finds, find_dups, promos, dry=True)
            log.info("Bot Activity Log entry that a real run would append:\n%s", "\n".join(notion.entry_text(entry)))
        log.info("Dry run: nothing written or sent.")
        return

    rows = [{"University": j.llm.get("university") or j.company, "Lab": "N/A", "PI": "TBD",
             "Level": j.llm.get("level", ""), "Funding": j.llm.get("funding", ""),
             "Deadline": j.llm.get("deadline", ""), "Status": j.llm["status"],
             "MOI_Required": j.llm.get("english_rule", ""), "GRE_Required": j.llm.get("gre_required", ""),
             "AI_Score": j.llm["score"], "Strategy": j.llm.get("why_fit", ""),
             "Date_Found": today.isoformat(), "Note": j.url} for j in good]
    added = write_tracker(rows)

    sent = False
    if picks or alerts or shown:
        try:
            sent = emailer.send(subject, body, md, os.environ.get("ALERT_TO") or cfg["alerts"]["email"])
        except Exception as e:
            log.error("Email failed: %s", e)
    for j in scored:
        state["seen"][j.key] = {"title": j.title[:120], "score": j.llm["score"], "date": today.isoformat()}
    if sent:
        for al in alerts:
            state["alerted"][al["tag"]] = today.isoformat()
        notion.mark_reported(notes, state)
    written, failed, finds_made, promoted = [], 0, [], []
    if ncfg.get("write") and (plans or new_finds or promos or find_dups):
        written, failed = notion.apply_writes(plans, ncfg)
        finds_made = notion.create_finds(new_finds, ncfg, today) if new_finds else []
        promoted = notion.apply_promotions(promos, ncfg, today) if promos else []
        entry = notion.activity_entry(written, failed, nstats, fstats, today, dry=False)
        if ncfg.get("finds"):
            entry += notion.finds_blocks(finds_made, find_dups, promoted, dry=False)
        logged = notion.append_log(ncfg, entry)
        log.info("Notion: wrote %d rows, %d failed, finds added %d, promotions %d, activity log %s",
                 len(written), failed, len(finds_made), len(promoted), "added" if logged else "NOT added")
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("Done. tracker +%d | picks %d | alerts %d | notion rows %d checked %d deadlines %d changed %d "
             "written %d failed %d | emailed %s",
             added, len(picks), len(alerts), len(nrows), nstats.get("checked", 0), nstats.get("deadlines", 0),
             nstats.get("changed", 0), len(written), failed, sent)


if __name__ == "__main__":
    main()
