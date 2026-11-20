from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("jobhunt")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
_session = requests.Session()
_session.headers.update({"User-Agent": UA, "Accept": "application/json, text/html, */*"})


def get(url: str, params: dict | None = None, timeout: int = 30, retries: int = 2, as_json: bool = True):
    """GET with small retry/backoff. Returns parsed JSON (or text) or None on failure."""
    for attempt in range(retries + 1):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429 and attempt < retries:
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code >= 400:
                log.debug("GET %s -> %s", url, r.status_code)
                return None
            return r.json() if as_json else r.text
        except Exception as e:  # network errors, bad JSON
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            log.debug("GET %s failed: %s", url, e)
            return None
