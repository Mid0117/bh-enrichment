"""Pull all non-deleted deals + paginate, save snapshot for today's run.

Output:
  state/deals_<YYYYMMDD>.json   (list of deal dicts as returned by Pipedrive)
  state/notes_<YYYYMMDD>.json   (dict {deal_id_str: [note_dicts]})
  run_log/run_<YYYYMMDDTHHMMSSZ>.json  (skeleton run-summary, appended by later steps)
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.pipedrive import PD

DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
TS   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR   = os.path.join(ROOT, "run_log")
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

DEALS_PATH = os.path.join(STATE_DIR, f"deals_{DATE}.json")
NOTES_PATH = os.path.join(STATE_DIR, f"notes_{DATE}.json")
RUN_LOG    = os.path.join(LOG_DIR, f"run_{TS}.json")
LATEST_RUN = os.path.join(LOG_DIR, "latest_run.json")


def main():
    pd = PD()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Pulling deals...", flush=True)
    deals = list(pd.paged("/deals", {"status": "all_not_deleted"}))
    print(f"  total deals: {len(deals)}", flush=True)

    with open(DEALS_PATH, "w", encoding="utf-8") as f:
        json.dump(deals, f)

    # Pull notes for last 7 days (caps run time; older notes are stable). The
    # summary writer below uses this snapshot.
    from datetime import timedelta as _td
    start_date = (datetime.now(timezone.utc).date() - _td(days=7)).isoformat()  # YYYY-MM-DD
    print(f"  pulling notes added since {start_date}...", flush=True)
    notes_by_deal = {}
    for n in pd.paged("/notes", {"start_date": start_date, "sort": "add_time DESC"}):
        did = n.get("deal_id")
        if not did:
            continue
        notes_by_deal.setdefault(str(did), []).append(n)

    with open(NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(notes_by_deal, f)
    print(f"  notes captured for {len(notes_by_deal)} deals", flush=True)

    summary = {
        "ts": TS,
        "date": DATE,
        "deals_count": len(deals),
        "notes_deals_count": len(notes_by_deal),
        "deals_path": DEALS_PATH,
        "notes_path": NOTES_PATH,
        "errors": [],
    }
    with open(RUN_LOG, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with open(LATEST_RUN, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
