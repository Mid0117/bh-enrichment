"""Rewrite the auto-summary note for every deal that changed in the last 24h
(stage_change_time, last_activity_date, or notes added today). Writes:
  - one POST /notes per refreshed deal (we never delete the prior summary
    note; we add a NEW one with today's date so history is preserved).
"""
from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.pipedrive import PD, F_ADDRESS
from lib.caller_extractor import extract_caller_from_notes
from lib.note_parser import extract_all_recordings


def build_user_resolver(pd: PD):
    """Cache /users -> id->name lookup for the run."""
    cache_path = os.path.join(STATE_DIR, "users_cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            cache = json.load(open(cache_path, encoding="utf-8"))
        except Exception:
            cache = {}
    if not cache:
        st, payload = pd.get("/users")
        if st == 200 and payload.get("data"):
            for u in payload["data"]:
                if u.get("id") and u.get("name"):
                    cache[str(u["id"])] = u["name"]
            json.dump(cache, open(cache_path, "w", encoding="utf-8"), indent=2)

    def resolve(uid):
        return cache.get(str(uid))
    return resolve

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")

DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
DATE_HUMAN = datetime.now(timezone.utc).strftime("%Y-%m-%d")
DEALS_PATH = os.path.join(STATE_DIR, f"deals_{DATE}.json")
NOTES_PATH = os.path.join(STATE_DIR, f"notes_{DATE}.json")
OUT_PATH   = os.path.join(STATE_DIR, f"summaries_{DATE}.json")

SUMMARY_HEADER = "AUTO-SUMMARY"


def is_summary_note(content: str) -> bool:
    return SUMMARY_HEADER in (content or "")


def build_summary(deal: dict, deal_notes: list, user_resolver=None) -> str:
    status = (deal.get("status") or "").upper()
    owner_name = (deal.get("user_id") or {}).get("name") if isinstance(deal.get("user_id"), dict) else ""
    owner_name = owner_name or "?"

    # Sort notes ascending for "first contact"
    sorted_asc = sorted(deal_notes, key=lambda n: (n.get("add_time") or ""))
    first = next((n for n in sorted_asc if not is_summary_note(n.get("content") or "")), None)
    first_caller, first_t = (None, None)
    if first:
        first_caller, first_t = extract_caller_from_notes([first], default=None, user_resolver=user_resolver)

    last_caller, last_t = extract_caller_from_notes(
        [n for n in deal_notes if not is_summary_note(n.get("content") or "")],
        default=None, user_resolver=user_resolver,
    )

    phone = ""
    address = (deal.get(F_ADDRESS) or "").strip()
    motivation = ""
    next_step = (deal.get("next_activity_subject") or "").strip()

    recordings = extract_all_recordings(
        [n for n in deal_notes if not is_summary_note(n.get("content") or "")],
        limit=10, user_resolver=user_resolver,
    )

    body = []
    body.append(f"AUTO-SUMMARY ({DATE_HUMAN})")
    body.append(f"Status: {status or '?'}")
    body.append(f"Owner: {owner_name}")
    body.append(f"First contact: {first_caller or '?'} · {(first_t or '')[:10]}")
    body.append(f"Last contact: {last_caller or '?'} · {(last_t or '')[:10]}")
    if phone:
        body.append(f"Phone: {phone}")
    if motivation:
        body.append(f"Motivation: {motivation}")
    if next_step:
        body.append(f"Next step: {next_step}")
    if recordings:
        body.append("")
        body.append("Call recordings:")
        for r in recordings:
            d = (r.get("add_time") or "")[:10]
            body.append(f"- {d} · {r.get('agent') or '?'} · {r['url']}")

    return "\n".join(body)


def main():
    if not os.path.exists(DEALS_PATH):
        print(f"missing {DEALS_PATH}", file=sys.stderr); sys.exit(2)
    deals = json.load(open(DEALS_PATH, encoding="utf-8"))
    notes_by_deal = json.load(open(NOTES_PATH, encoding="utf-8")) if os.path.exists(NOTES_PATH) else {}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=26)).isoformat()
    pd = PD()
    user_resolver = build_user_resolver(pd)

    posted = 0; skipped = 0; errors = []
    log_rows = []

    for d in deals:
        did = d.get("id")
        if not did or d.get("deleted"):
            continue
        # Activity check: changed today OR has a note today
        last_activity = (d.get("last_activity_date") or "")
        update_time = (d.get("update_time") or "")
        recent_notes = notes_by_deal.get(str(did), [])
        if not recent_notes and (update_time or "") < cutoff[:10]:
            continue
        try:
            summary_text = build_summary(d, recent_notes, user_resolver=user_resolver)
        except Exception as e:
            errors.append({"deal_id": did, "phase": "BUILD", "err": repr(e)})
            continue
        try:
            st, payload = pd.post("/notes", {"deal_id": did, "content": summary_text})
            if st in (200, 201):
                posted += 1
                log_rows.append({"deal_id": did, "ok": True})
            else:
                errors.append({"deal_id": did, "phase": "POST", "status": st, "payload": payload})
        except Exception as e:
            errors.append({"deal_id": did, "phase": "EXC", "err": repr(e)})

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "deals_processed": len(deals),
        "summaries_posted": posted,
        "errors": errors,
        "log_count": len(log_rows),
    }
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
    print(json.dumps({k: out[k] for k in ("deals_processed", "summaries_posted", "errors")}, indent=2, default=str))


if __name__ == "__main__":
    main()
