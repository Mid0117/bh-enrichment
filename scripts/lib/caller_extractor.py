"""Caller-name extraction from Pipedrive note HTML.

Goal: find the caller (BH agent) from the most recent note that records
a phone interaction, NOT from Pipedrive `user_id` (which is often Mike Kent
because Mostafa Khirat aliases as Jason Garcia and many notes are posted
manually under one shared user).

Roster of BH callers we look for explicitly. Order = priority on tie.
"""
from __future__ import annotations
import re
from html import unescape
from typing import List, Optional, Tuple

# Names per Zach lane briefing. "Mike" alone is ambiguous (could be the
# Pipedrive user "Mike Kent" used by everyone) -- handled below.
BH_CALLERS = [
    "Regina", "Huney", "John", "Nick", "Ashley",
    "Jason", "Mike", "Shyrine",
]

# Per Mido directive 2026-05-08: never attribute last-contact / caller
# to Mostafa Khirat in any BH automation output. His Pipedrive alias is
# "Jason Garcia" (user_id 14245674), so we exclude both surface forms
# (case-insensitive, normalized).
EXCLUDE_CALLERS = {
    "mostafa khirat",
    "mostafa",
    "jason garcia",
    "jason",
}


def _is_excluded(name: Optional[str]) -> bool:
    if not name:
        return False
    return name.strip().lower() in EXCLUDE_CALLERS


# When we see "Mike" alone we want to disambiguate. If the note also mentions
# the @Mike Kent user-chip OR uses Mike in 3rd person ("Mike called"), we
# default to Mike Kent.
_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX  = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    txt = _TAG_RX.sub(" ", s)
    txt = unescape(txt)
    txt = _WS_RX.sub(" ", txt).strip()
    return txt


# Action verbs that signal the named person was the caller/contact agent.
ACTION_VERBS = (
    "called", "calling", "spoke", "spoken", "talked",
    "dialed", "reached", "rang", "messaged", "texted",
    "left a vm", "left vm", "lvm", "left a message",
)

# pattern: NAME <action> ... or NAME <action_back> or "spoken with NAME"
def _attr_patterns(name: str) -> List[re.Pattern]:
    n = re.escape(name)
    return [
        re.compile(rf"\b{n}\s+(?:has\s+|just\s+)?(?:{'|'.join(ACTION_VERBS)})\b", re.I),
        re.compile(rf"\b(?:by|from|with|w\/)\s+{n}\b", re.I),
        re.compile(rf"\b{n}\s*[:\-]\s+", re.I),  # "Regina: spoke with..."
        re.compile(rf"^\s*{n}\b", re.I | re.M),  # name at start of line
    ]


def extract_caller(html_or_text: str, default: Optional[str] = "Mike Kent") -> Optional[str]:
    """Return the most likely BH caller name in this note, or default.

    Heuristic:
      1. Strip HTML.
      2. Walk BH_CALLERS in priority order. First name with a strong
         attribution pattern wins.
      3. For "Mike", we accept if the note clearly attributes (verb pattern),
         and we always normalize to "Mike Kent".
      4. Excluded names (EXCLUDE_CALLERS) are never returned -- we skip them
         and continue searching the same note for another candidate.
    """
    text = strip_html(html_or_text or "")
    if not text:
        return default

    # Priority pass: VA names (less ambiguous) first.
    # NOTE: "Jason" stays in the priority list so its pattern is *checked*,
    # but we then drop the match via _is_excluded() so we never RETURN it.
    # This ensures a later name in the list (e.g. Regina also named in same
    # note) is not silently shadowed -- we still iterate past Jason.
    priority = ["Regina", "Huney", "Shyrine", "Jason", "Nick", "John", "Ashley"]
    for name in priority:
        for rx in _attr_patterns(name):
            if rx.search(text):
                # Disambiguate "John" vs neighbor John (substring "neighbor John")
                if name == "John" and re.search(r"neighbor[s]?\s+John\b", text, re.I):
                    continue
                resolved = _full_name(name)
                if _is_excluded(resolved) or _is_excluded(name):
                    # Skip this match; keep scanning other names.
                    break
                return resolved

    # Now Mike (ambiguous). Skip if the only Mike mention is an @-chip-style
    # addressee ("Hi @Mike Kent, I dialed..."): the writer of the note is the
    # caller in that case, not Mike. We treat Mike as caller only when an
    # action verb is directly attached to the bare name ("Mike called").
    bare_mike = re.compile(r"\bMike\s+(?:has\s+|just\s+)?(?:" + "|".join(ACTION_VERBS) + r")\b", re.I)
    if bare_mike.search(text):
        # And only if "Mike Kent" is NOT addressed via @-chip immediately followed
        # by first-person "I dialed/spoke/called"
        addressee_pat = re.compile(r"@\s*Mike\s+Kent[^\.\n]{0,80}\bI\s+(?:dialed|spoke|called|talked|reached)\b", re.I)
        if not addressee_pat.search(text):
            return "Mike Kent"

    return default


def _full_name(short: str) -> str:
    """Map short form back to full name we want to display in summary."""
    return {
        "Regina": "Regina",
        "Huney": "Huney",
        "Shyrine": "Shyrine",
        "Jason": "Jason Garcia",   # alias for Mostafa Khirat -- excluded downstream
        "Nick": "Nick",
        "John": "John",
        "Ashley": "Ashley",
        "Mike": "Mike Kent",
    }.get(short, short)


def extract_caller_from_notes(notes: List[dict], default: Optional[str] = "Mike Kent",
                              user_resolver=None) -> Tuple[Optional[str], Optional[str]]:
    """Walk notes newest-first; return (caller_name, note_add_time).

    `notes`: list of dicts with `content`, `add_time`, optionally `user_id`.
    `user_resolver`: optional callable user_id -> name. When the note content
    has no clear caller attribution, we fall back to the note's author name
    (the user who wrote the note in Pipedrive, which is the BH agent).

    Excluded names (EXCLUDE_CALLERS, e.g. Mostafa Khirat / Jason Garcia) are
    never returned. If both content extraction and user_id resolution yield
    only excluded names for a given note, we walk back to the next-most-recent
    note. If no non-excluded caller is found in any note, we return
    (default, latest_note_time) -- callers passing default=None will then get
    "" / None which the summary formatter renders as empty.
    """
    if not notes:
        return default, None
    sorted_notes = sorted(notes, key=lambda n: (n.get("add_time") or ""), reverse=True)
    for n in sorted_notes:
        cand = extract_caller(n.get("content") or "", default=None)
        if cand and not _is_excluded(cand):
            return cand, n.get("add_time")
        # Fallback: use note author from Pipedrive user_id
        if user_resolver and n.get("user_id"):
            uname = user_resolver(n.get("user_id"))
            if uname and not _is_excluded(uname):
                return uname, n.get("add_time")
        # If we got here, this note's caller (content or user_id) was either
        # missing or excluded -- continue to the next-older note.
    # No non-excluded caller in any note. Return default + latest add_time
    # so downstream formatters can still show a recency stamp.
    return default, sorted_notes[0].get("add_time") if sorted_notes else None
