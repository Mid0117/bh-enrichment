"""Pipedrive note parsing utilities.

- extract_call_recordings: walks notes newest-first, returns list of
  dicts {add_time, agent, url} for every recording URL detected.
- recording URL match domains: aircall.io, calltools.com, .mp3 ext, or any
  URL where the path/host clearly screams "recording".
"""
from __future__ import annotations
import re
from html import unescape
from typing import Dict, List, Optional, Tuple

from .caller_extractor import extract_caller, strip_html, _is_excluded

# Capture URLs: from href= and from bare http(s) text
_HREF_RX = re.compile(r'href\s*=\s*"([^"]+)"', re.I)
_URL_RX  = re.compile(r'(https?://[^\s<>"\']+)', re.I)


def _is_recording_url(url: str) -> bool:
    u = url.lower()
    if "aircall.io" in u:
        return True
    if "calltools.com" in u:
        return True
    if "misc-media-ct" in u:  # Calltools recordings on S3
        return True
    if u.endswith(".mp3") or u.endswith(".wav"):
        return True
    if "recording" in u and u.startswith("http"):
        return True
    return False


def find_urls(html: str) -> List[str]:
    if not html:
        return []
    urls: List[str] = []
    seen = set()
    for m in _HREF_RX.finditer(html):
        u = unescape(m.group(1))
        if u not in seen:
            seen.add(u); urls.append(u)
    # bare URLs in text content
    text = strip_html(html)
    for m in _URL_RX.finditer(text):
        u = m.group(1)
        # strip trailing punctuation
        u = u.rstrip(").,;]")
        if u not in seen:
            seen.add(u); urls.append(u)
    return urls


def extract_recordings_from_note(note: Dict, user_resolver=None) -> List[Dict[str, str]]:
    html = note.get("content") or ""
    add_time = note.get("add_time")
    out = []
    for u in find_urls(html):
        if _is_recording_url(u):
            agent = extract_caller(html, default=None)
            if (not agent or _is_excluded(agent)) and user_resolver and note.get("user_id"):
                cand = user_resolver(note.get("user_id"))
                if cand and not _is_excluded(cand):
                    agent = cand
                else:
                    agent = None
            if agent and _is_excluded(agent):
                agent = None
            out.append({"add_time": add_time, "agent": agent or "unknown", "url": u})
    return out


def extract_all_recordings(notes: List[Dict],
                           limit: Optional[int] = None,
                           user_resolver=None) -> List[Dict[str, str]]:
    """Return recordings sorted by add_time DESC. Optionally limit count."""
    if not notes:
        return []
    sorted_notes = sorted(notes, key=lambda n: (n.get("add_time") or ""), reverse=True)
    out: List[Dict[str, str]] = []
    seen_urls = set()
    for n in sorted_notes:
        for rec in extract_recordings_from_note(n, user_resolver=user_resolver):
            if rec["url"] in seen_urls:
                continue
            seen_urls.add(rec["url"])
            out.append(rec)
            if limit and len(out) >= limit:
                return out
    return out
