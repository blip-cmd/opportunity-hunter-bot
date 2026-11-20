"""Free-tier LLM scoring with provider fallback.

Order (all OpenAI-compatible chat endpoints, all free with no credit card):
  1. Google Gemini - GEMINI_API_KEY from aistudio.google.com/apikey
  2. Groq          - GROQ_API_KEY from console.groq.com/keys
  3. OpenRouter    - OPENROUTER_API_KEY from openrouter.ai/keys (":free" models)
Set at least one. If none is set or all are exhausted, jobs keep their
keyword score only (capped so they never outrank AI-reviewed jobs).
GitHub Models was the original first choice but GitHub retired it on 2026-07-30.

Model names can be overridden without code changes via GEMINI_MODEL,
GROQ_MODEL and OPENROUTER_MODEL, in case a provider renames its models.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from .models import Job

log = logging.getLogger("jobhunt")

PROVIDERS = [
    {
        "name": "gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model": os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash",
        "min_interval": 6.5,  # free tier: ~10 requests/minute
    },
    {
        "name": "groq",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "key_env": "GROQ_API_KEY",
        "model": os.environ.get("GROQ_MODEL") or "llama-3.3-70b-versatile",
        "min_interval": 2.5,  # free tier: ~30 requests/minute
    },
    {
        "name": "openrouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model": os.environ.get("OPENROUTER_MODEL") or "meta-llama/llama-3.3-70b-instruct:free",
        "min_interval": 4.0,  # free models: ~20 requests/minute
    },
]

SYSTEM = """You are a blunt, accurate career advisor screening job postings for one specific candidate.
You judge fit honestly: never inflate scores, never invent facts about the candidate.
Reply with ONE JSON object only, no markdown."""

USER_TMPL = """CANDIDATE PROFILE
{profile}

JOB POSTING
Company: {company}
Title: {title}
Location: {location}
Source: {source}
Posted: {posted}
Description (truncated):
{description}

Evaluate this posting for the candidate. Return JSON with exactly these keys:
{{
 "score": integer 0-10 (10 = near-certain strong fit he can realistically get; 7 = good shot; 5 = long shot; <=3 = not worth time),
 "status": "APPLY" (open now and realistic) | "STRETCH" (open now, long shot but worth it) | "WATCH" (programme not open yet / talent pool / future intake) | "SKIP",
 "gates": {{"location": "pass|flag|fail", "timeline": "pass|flag|fail", "level": "pass|flag|fail", "direction": "pass|flag|fail"}},
 "cv_profile": "data" | "software" | "ml" | "robotics",
 "role_summary": "one short sentence on what the job actually is",
 "why_fit": "one sentence citing his concrete matching evidence",
 "prerequisites_blockers": "work authorization, NSS, degree or other hard requirements; 'None' if none",
 "gaps": "main gaps vs requirements, short",
 "cover_note": "one sentence: which of his projects/experience to lead with",
 "deadline": "deadline or opening date if stated, else 'Rolling'"
}}
Rules: location gate FAILS if the role requires existing work authorization he lacks (e.g. US-only, EU citizens only) and offers no sponsorship or worldwide remote.
Level gate FAILS if it needs 3+ years of professional experience. Senior/staff/lead roles are SKIP.
If it is a fellowship, scholarship or programme rather than a job, judge it the same way."""


class Scorer:
    def __init__(self, profile: str, max_calls: int):
        self.profile = profile
        self.max_calls = max_calls
        self.calls = 0
        self.last_call = 0.0
        self.providers = []
        for p in PROVIDERS:
            key = (os.environ.get(p["key_env"]) or "").strip()
            if key:
                self.providers.append({**p, "key": key})
        self.dead: set[str] = set()
        if self.providers:
            log.info("LLM providers available: %s", [p["name"] for p in self.providers])
        else:
            log.warning("No AI key set (GEMINI_API_KEY, GROQ_API_KEY or OPENROUTER_API_KEY). "
                        "Using keyword scores only. See search/HUMAN_TASKS.md.")

    @property
    def available(self) -> bool:
        return self.calls < self.max_calls and any(p["name"] not in self.dead for p in self.providers)

    def _chat(self, p: dict, messages: list[dict]) -> str | None:
        wait = p["min_interval"] - (time.time() - self.last_call)
        if wait > 0:
            time.sleep(wait)
        for attempt in range(3):
            self.last_call = time.time()
            try:
                r = requests.post(
                    p["url"],
                    headers={"Authorization": f"Bearer {p['key']}", "Content-Type": "application/json"},
                    json={"model": p["model"], "messages": messages, "temperature": 0.1, "max_tokens": 700},
                    timeout=90,
                )
            except requests.RequestException as e:
                log.debug("%s network error: %s", p["name"], e)
                time.sleep(5)
                continue
            if r.status_code == 429:
                retry = r.headers.get("retry-after")
                # Short waits are per-minute limits; long ones mean the daily quota is gone.
                if retry and retry.isdigit() and int(retry) <= 65 and attempt < 2:
                    time.sleep(int(retry) + 1)
                    continue
                log.warning("%s rate-limited (daily quota likely used); switching provider", p["name"])
                self.dead.add(p["name"])
                return None
            if r.status_code in (401, 403, 404):
                log.warning("%s unavailable (%s): %s", p["name"], r.status_code, r.text[:150])
                self.dead.add(p["name"])
                return None
            if r.status_code >= 400:
                log.debug("%s error %s: %s", p["name"], r.status_code, r.text[:200])
                time.sleep(3)
                continue
            try:
                return r.json()["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError):
                # Non-JSON or malformed body (e.g. a proxy stub): this provider is unusable here.
                log.warning("%s returned a non-API response (%s); switching provider",
                            p["name"], r.text[:40].replace("\n", " "))
                self.dead.add(p["name"])
                return None
        return None

    @staticmethod
    def _parse(text: str) -> dict | None:
        if not text:
            return None
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
        try:
            d["score"] = max(0, min(10, int(round(float(d.get("score", 0))))))
        except (TypeError, ValueError):
            return None
        d["status"] = str(d.get("status", "SKIP")).upper()
        if d["status"] not in {"APPLY", "STRETCH", "WATCH", "SKIP"}:
            d["status"] = "SKIP"
        return d

    def score(self, job: Job) -> dict | None:
        if not self.available:
            return None
        messages = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": USER_TMPL.format(
                    profile=self.profile,
                    company=job.company,
                    title=job.title,
                    location=job.location,
                    source=job.source,
                    posted=job.posted.date().isoformat() if job.posted else "unknown",
                    description=(job.description or "")[:3500],
                ),
            },
        ]
        for p in self.providers:
            if p["name"] in self.dead:
                continue
            self.calls += 1
            out = self._parse(self._chat(p, messages))
            if out:
                out["model"] = p["name"]
                return out
        return None
