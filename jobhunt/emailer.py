"""Builds the ranked digest (HTML email + markdown copy) and sends it via Gmail SMTP."""
from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from datetime import date
from email.message import EmailMessage

from . import verify
from .models import Job

log = logging.getLogger("jobhunt")
E = html.escape

# CV file names live in the private config (email.cv_files); generic defaults here
# so the public runner repo carries no personal details.
CV_FILES = {"data": "Data CV", "software": "Software CV", "ml": "ML CV", "robotics": "Robotics CV"}


HOME = "home country"


def set_home(name: str) -> None:
    global HOME
    HOME = name


def set_cv_files(mapping: dict | None) -> None:
    if mapping:
        CV_FILES.update(mapping)


def _card(rank: int, j: Job) -> str:
    l = j.llm or {}
    g = l.get("gates") or {}
    gate_html = " ".join(
        f'<span style="padding:2px 6px;border-radius:4px;font-size:12px;background:'
        f'{"#d9f2e3" if v == "pass" else "#fff1cc" if v == "flag" else "#fde0dc"}">{E(k)}: {E(str(v))}</span>'
        for k, v in g.items()
    )
    posted = f"{j.age_days():.0f} days ago" if j.age_days() is not None else "date unknown"
    return f"""
<div style="border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin:0 0 14px">
  <div style="font-size:13px;color:#666">#{rank} &middot; score {l.get('score', '?')}/10 &middot; {E(l.get('status', ''))} &middot; {E(j.source)} &middot; posted {posted}</div>
  <div style="font-size:18px;font-weight:600;margin:4px 0">{E(j.title)}</div>
  <div style="font-size:14px;margin-bottom:8px"><b>{E(j.company)}</b> &middot; {E(j.location)}</div>
  <div style="margin-bottom:8px">{gate_html}</div>
  <p style="margin:4px 0"><b>The role:</b> {E(l.get('role_summary', ''))}</p>
  <p style="margin:4px 0"><b>Why you:</b> {E(l.get('why_fit', ''))}</p>
  <p style="margin:4px 0"><b>Gaps:</b> {E(l.get('gaps', ''))}</p>
  <p style="margin:4px 0"><b>Blockers:</b> {E(l.get('prerequisites_blockers', ''))}</p>
  <p style="margin:4px 0"><b>Open to {E(HOME)}:</b> {E(verify.summary(j))}</p>
  <p style="margin:4px 0"><b>Lead with:</b> {E(l.get('cover_note', ''))}</p>
  <p style="margin:4px 0"><b>CV to send:</b> {E(CV_FILES.get(l.get('cv_profile', ''), 'General 2-page CV'))} &middot; <b>Deadline:</b> {E(str(l.get('deadline', 'Rolling')))}</p>
  <p style="margin:10px 0 0"><a href="{E(j.url)}" style="background:#1a56db;color:#fff;padding:8px 14px;border-radius:6px;text-decoration:none">Open and apply</a></p>
</div>"""


def build(picks: list[Job], runners_up: list[Job], alerts: list[dict], stats: dict) -> tuple[str, str, str]:
    today = date.today().isoformat()
    subject = f"Job picks {today}: " + (
        ", ".join(f"{j.company}" for j in picks[:3]) if picks else "no strong matches this round"
    )
    cards = "".join(_card(i + 1, j) for i, j in enumerate(picks)) or "<p>No new roles cleared the bar this round.</p>"
    alert_html = ""
    if alerts:
        rows = "".join(
            f'<li><b>{E(a["name"])}</b>: {E(a["detail"])} <a href="{E(a["url"])}">programme page</a></li>'
            for a in alerts
        )
        alert_html = f"<h3>Programme watchlist</h3><ul>{rows}</ul>"
    ru_html = ""
    if runners_up:
        rows = "".join(
            f'<li>{(j.llm or {}).get("score", "?")}/10 &middot; <a href="{E(j.url)}">{E(j.title)}</a> at {E(j.company)} ({E(j.location)}) &middot; {E(HOME)}: {E(((j.llm or {}).get("eligibility") or {}).get("eligible", "not checked"))}</li>'
            for j in runners_up
        )
        ru_html = f"<h3>Also worth a look (added to tracker)</h3><ul>{rows}</ul>"
    stat_line = " &middot; ".join(f"{k}: {v}" for k, v in stats.items())
    body = f"""<div style="font-family:Segoe UI,Arial,sans-serif;max-width:680px;color:#111">
<h2 style="margin-bottom:4px">Your top picks for {today}</h2>
<p style="color:#555;margin-top:0">Ranked by fit. Apply to these first. All are in APPLICATIONS_TRACKER.csv.</p>
{cards}{alert_html}{ru_html}
<p style="color:#888;font-size:12px;margin-top:24px">{stat_line}<br>Sources include Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Amazon, Remotive, RemoteOK, Himalayas, Jobicy, Arbeitnow, Working Nomads, We Work Remotely, Hacker News, opportunity feeds and JobSpy.</p>
</div>"""

    md = [f"# Job picks {today}", ""]
    for i, j in enumerate(picks, 1):
        l = j.llm or {}
        md += [
            f"## {i}. {j.title} at {j.company} ({l.get('score', '?')}/10, {l.get('status', '')})",
            f"- Location: {j.location}",
            f"- Link: {j.url}",
            f"- Why: {l.get('why_fit', '')}",
            f"- Gaps: {l.get('gaps', '')}",
            f"- Blockers: {l.get('prerequisites_blockers', '')}",
            f"- Open to {HOME}: {verify.summary(j)}",
            f"- Lead with: {l.get('cover_note', '')}",
            f"- CV: {CV_FILES.get(l.get('cv_profile', ''), 'General')}",
            "",
        ]
    if alerts:
        md += ["## Watchlist", *[f"- {a['name']}: {a['detail']} {a['url']}" for a in alerts], ""]
    if runners_up:
        md += ["## Also found", *[f"- {(j.llm or {}).get('score', '?')}/10 {j.title} at {j.company}: {j.url}" for j in runners_up], ""]
    md.append(f"_{stat_line.replace(' &middot; ', ' | ')}_")
    return subject, body, "\n".join(md)


def send(subject: str, html_body: str, text_body: str, to: str) -> bool:
    user = os.environ.get("GMAIL_ADDRESS") or to
    pwd = os.environ.get("GMAIL_APP_PASSWORD")
    if not pwd:
        log.warning("GMAIL_APP_PASSWORD not set; email not sent (digest still saved).")
        return False
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, f"Job Hunter <{user}>", to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(user, pwd.replace(" ", ""))
        s.send_message(msg)
    log.info("Email sent to %s", to)
    return True
