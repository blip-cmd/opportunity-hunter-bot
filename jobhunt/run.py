"""Entry point: python -m jobhunt.run  (run from the search/ folder).

Pipeline: fetch every source -> dedupe -> hard filters -> heuristic score ->
free LLM review of the shortlist -> rank -> tracker + email top 2-3.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import os
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import yaml

from . import emailer, tracker, watchlist
from .filters import Filters
from .llm import Scorer
from .models import Job
from .sources import ats, boards, feeds, jobspy_src
from .store import Store

HERE = Path(__file__).resolve().parents[1]
log = logging.getLogger("jobhunt")


def collect(cfg: dict, f: Filters, use_jobspy: bool) -> list[Job]:
    keep = f.keep
    tasks = []
    for board, companies in cfg["companies"].items():
        fetch = ats.FETCHERS[board]
        for slug, name in companies.items():
            tasks.append((f"{board}:{slug}", lambda fetch=fetch, slug=slug, name=name: list(fetch(slug, name, keep))))
    tasks.append(("amazon", lambda: list(ats.amazon(cfg["amazon_queries"], keep))))
    for name, fn in boards.BOARDS.items():
        if name in cfg["boards"]["enabled"]:
            args = cfg["boards"].get("tags" if name == "jobicy" else "searches", [])
            tasks.append((name, lambda fn=fn, args=args: list(fn(args, keep))))
    tasks.append(("opportunity-rss", lambda: list(feeds.rss(cfg["opportunity_feeds"]["feeds"], f.keep_feed))))
    tasks.append(("hackernews", lambda: list(feeds.hackernews(keep))))

    jobs: list[Job] = []
    per_source: Counter = Counter()
    with cf.ThreadPoolExecutor(max_workers=cfg["run"]["workers"]) as ex:
        futs = {ex.submit(fn): name for name, fn in tasks}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                got = fut.result()
            except Exception as e:
                log.warning("  %s failed: %s", name, str(e)[:150])
                continue
            per_source[name.split(":")[0]] += len(got)
            jobs.extend(got)
    if use_jobspy:
        js = cfg["jobspy"]
        got = list(jobspy_src.jobspy(js["queries"], keep, js["hours_old"],
                                     js.get("pause_seconds", 30), js.get("max_blocked_in_a_row", 3)))
        per_source["jobspy"] += len(got)
        jobs.extend(got)
    log.info("Fetched %d candidate postings: %s", len(jobs), dict(per_source))
    return jobs


def heuristic_llm(job: Job) -> dict:
    """Fallback verdict when no free LLM quota is left."""
    # Capped at 6 so keyword-only matches never outrank AI-reviewed ones.
    s = max(0, min(6, round(2 + job.pre_score / 2)))
    return {
        "score": s, "status": "STRETCH" if s >= 5 else "SKIP", "gates": {}, "cv_profile": "software",
        "role_summary": job.title, "why_fit": "Keyword match only: no AI review this run (add a free GEMINI_API_KEY secret, see HUMAN_TASKS.md).",
        "prerequisites_blockers": "Check listing", "gaps": "Not assessed", "cover_note": "",
        "deadline": job.deadline or "Rolling", "model": "heuristic",
    }


def rank_value(job: Job) -> float:
    l = job.llm or {}
    status_bonus = {"APPLY": 6, "STRETCH": 0, "WATCH": -8, "SKIP": -40}.get(l.get("status"), -40)
    fails = sum(1 for v in (l.get("gates") or {}).values() if v == "fail")
    age = job.age_days()
    fresh = 2 if age is not None and age <= 3 else (1 if age is not None and age <= 10 else 0)
    return l.get("score", 0) * 10 + status_bonus - 8 * fails + job.pre_score + fresh


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no email, no tracker/state writes")
    ap.add_argument("--no-jobspy", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--max-llm", type=int, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")
    for noisy in ("urllib3", "JobSpy", "jobspy"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = yaml.safe_load((HERE / "config.yaml").read_text(encoding="utf-8"))
    profile = (HERE / "profile.md").read_text(encoding="utf-8")
    emailer.set_cv_files(cfg.get("email", {}).get("cv_files"))
    f = Filters(cfg)
    store = Store(HERE / "state" / "seen.json")
    run = cfg["run"]

    # 1. Fetch and dedupe
    raw = collect(cfg, f, use_jobspy=cfg["jobspy"]["enabled"] and not a.no_jobspy)
    fresh, keys, fps = [], set(), set()
    for j in raw:
        if j.key in keys or j.fingerprint in fps or store.seen(j):
            continue
        keys.add(j.key)
        fps.add(j.fingerprint)
        fresh.append(j)

    # 2. Hard filters + heuristic score
    survivors, rejected = [], Counter()
    for j in fresh:
        why = f.hard_reject(j)
        if why:
            rejected[why.split()[0]] += 1
            if not a.dry_run:
                store.record(j, rejected=why)
            continue
        j.pre_score = f.pre_score(j)
        survivors.append(j)
    survivors.sort(key=lambda j: j.pre_score, reverse=True)

    # 3. Shortlist (diverse: max N per company), then LLM review
    per_co: defaultdict = defaultdict(int)
    shortlist = []
    for j in survivors:
        if j.pre_score < run["min_pre_score"]:
            break
        if per_co[j.company.lower()] >= run["max_per_company"]:
            continue
        per_co[j.company.lower()] += 1
        shortlist.append(j)
    max_calls = a.max_llm if a.max_llm is not None else run["max_llm_calls"]
    shortlist = shortlist[: max(max_calls, run["min_shortlist"])]
    log.info("New: %d | rejected: %s | survivors: %d | shortlist: %d", len(fresh), dict(rejected), len(survivors), len(shortlist))

    scorer = None if a.no_llm else Scorer(profile, max_calls)
    scored = []
    for i, j in enumerate(shortlist, 1):
        verdict = scorer.score(j) if scorer and scorer.available else None
        j.llm = verdict or heuristic_llm(j)
        log.info("  [%d/%d] %s/10 %-8s %s | %s", i, len(shortlist), j.llm["score"], j.llm["status"], j.company[:24], j.title[:60])
        scored.append(j)
        if not a.dry_run:
            store.record(j, rejected="llm-skip" if j.llm["status"] == "SKIP" else None)

    # 4. Rank new + backlog (good jobs from earlier runs not yet emailed)
    pool = {j.key: j for j in scored}
    for j in store.backlog(run["backlog_days"]):
        pool.setdefault(j.key, j)
    ranked = sorted(
        (j for j in pool.values() if (j.llm or {}).get("status") in {"APPLY", "STRETCH"}),
        key=rank_value, reverse=True,
    )
    picks = [j for j in ranked if j.llm["score"] >= run["pick_min_score"]][: run["picks_max"]]
    if len(picks) < run["picks_min"]:  # never send fewer than picks_min if anything decent exists
        extra = [j for j in ranked if j not in picks and j.llm["score"] >= run["pick_floor_score"]]
        picks += extra[: run["picks_min"] - len(picks)]
    runners = [j for j in ranked if j not in picks and j.llm["score"] >= run["tracker_min_score"]][: run["runners_up_max"]]
    watch_found = [j for j in scored if j.llm.get("status") == "WATCH" and j.llm["score"] >= run["tracker_min_score"]]

    # 5. Watchlist programmes
    alerts = watchlist.check(cfg["watchlist"], store.data["watch"])

    stats = {
        "fetched": len(raw), "new": len(fresh), "AI-reviewed": len(scored),
        "LLM": ",".join(sorted({(j.llm or {}).get("model", "?") for j in scored})) or "none",
    }
    subject, html_body, md = emailer.build(picks, runners, alerts, stats)
    print("\n" + md)
    if a.dry_run:
        log.info("Dry run: nothing written or sent.")
        return

    # 6. Tracker, digest file, email, state
    today = date.today().isoformat()
    sent = False
    if picks or alerts:
        try:
            sent = emailer.send(subject, html_body, md, os.environ.get("ALERT_TO") or cfg["email"]["to"])
        except Exception as e:
            log.error("Email failed: %s", e)
    for i, j in enumerate(picks, 1):
        tracker.add_job(j, f"TOP PICK #{i} ({'emailed' if sent else 'found'} {today}): tailor CV and apply")
        # Only an actually-sent email retires a pick; otherwise it stays in the backlog.
        store.mark(j.key, tracked=True, **({"emailed": today} if sent else {}))
    for j in runners + watch_found:
        if tracker.add_job(j, "Auto-found: review and apply" if j not in watch_found else "Talent pool / future intake: register"):
            store.mark(j.key, tracked=True)
    for w in cfg["watchlist"]:
        tracker.add_watch(w["name"], w["url"], w.get("expected_open", "TBD"), w.get("cv_profile", "software"), w.get("note", ""))

    digest_dir = HERE / "digests"
    digest_dir.mkdir(exist_ok=True)
    (digest_dir / f"{today}.md").write_text(md, encoding="utf-8")
    store.save()
    log.info("Done. Picks: %d | runners-up: %d | watch alerts: %d", len(picks), len(runners), len(alerts))


if __name__ == "__main__":
    main()
