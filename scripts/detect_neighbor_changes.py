"""Compare today's deals snapshot vs yesterday's (latest available previous
snapshot in state/). Detect new deals that landed on a (borough, block) where
BH already has at least one OTHER active deal -- those are "new neighbors" to
flag for connected-properties update.

Reads:
  state/deals_<TODAY>.json
  state/deals_<PREV_DATE>.json   (most recent dated file before today)

Writes:
  state/neighbor_changes_<TODAY>.json -- list of {deal_id, block_key, neighbor_deal_ids}
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.pluto import block_key, parse_bbl
from lib.pipedrive import F_ACRIS_LINKS, F_ZONING

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR   = os.path.join(ROOT, "run_log")

DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
TODAY_PATH = os.path.join(STATE_DIR, f"deals_{DATE}.json")
OUT_PATH   = os.path.join(STATE_DIR, f"neighbor_changes_{DATE}.json")
LATEST_RUN = os.path.join(LOG_DIR, "latest_run.json")


def previous_snapshot():
    cands = sorted(
        f for f in os.listdir(STATE_DIR)
        if f.startswith("deals_") and f.endswith(".json") and f != f"deals_{DATE}.json"
    )
    return os.path.join(STATE_DIR, cands[-1]) if cands else None


def deal_block_key(d):
    """Derive (boro, block) from the deal's ACRIS Links field. v7 ACRIS URL has
    hid_borough=N&hid_block=B&hid_lot=L so we can parse it directly. Fall back
    to None if not present."""
    acris = d.get(F_ACRIS_LINKS) or ""
    if not acris:
        return None
    # Take first line; works for single + multi-parcel deals
    line = acris.split("\n")[0]
    import re
    m = re.search(r"hid_borough=(\d)&hid_block=(\d+)", line)
    if not m:
        return None
    bnum = int(m.group(1)); block = int(m.group(2))
    boro_map = {1: "MN", 2: "BX", 3: "BK", 4: "QN", 5: "SI"}
    boro = boro_map.get(bnum)
    if not boro:
        return None
    return f"{boro}-{block}"


def index_by_block(deals):
    idx = {}
    for d in deals:
        bk = deal_block_key(d)
        if not bk:
            continue
        idx.setdefault(bk, []).append(d.get("id"))
    return idx


def main():
    if not os.path.exists(TODAY_PATH):
        print(f"ERROR: today snapshot missing: {TODAY_PATH}", file=sys.stderr); sys.exit(2)
    today = json.load(open(TODAY_PATH, encoding="utf-8"))
    prev_path = previous_snapshot()

    if not prev_path:
        # First run -- nothing to compare. Emit empty result.
        out = {"ts": datetime.now(timezone.utc).isoformat(), "first_run": True, "changes": []}
        json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
        print(json.dumps(out, indent=2))
        return

    prev = json.load(open(prev_path, encoding="utf-8"))

    today_idx = index_by_block(today)
    prev_idx  = index_by_block(prev)

    today_ids = {d.get("id") for d in today if d.get("id")}
    prev_ids  = {d.get("id") for d in prev  if d.get("id")}
    new_ids   = today_ids - prev_ids

    changes = []
    for did in new_ids:
        new_deal = next((d for d in today if d.get("id") == did), None)
        if not new_deal: continue
        bk = deal_block_key(new_deal)
        if not bk: continue
        # Existing neighbors today (excluding this new deal itself)
        siblings = [x for x in today_idx.get(bk, []) if x != did]
        if not siblings:
            continue
        # Only flag if at least one sibling existed yesterday (otherwise it's a
        # day-zero block, no "change").
        yesterday_block = set(prev_idx.get(bk, []))
        existing_yesterday = [s for s in siblings if s in yesterday_block]
        if not existing_yesterday:
            continue
        changes.append({
            "new_deal_id": did,
            "block_key": bk,
            "existing_neighbor_deal_ids": existing_yesterday,
            "new_deal_title": (new_deal.get("title") or "")[:120],
        })

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "today_path": TODAY_PATH,
        "prev_path": prev_path,
        "today_deal_count": len(today),
        "prev_deal_count": len(prev),
        "new_deal_count": len(new_ids),
        "neighbor_change_count": len(changes),
        "changes": changes,
    }
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
    print(json.dumps({k: out[k] for k in ("today_deal_count", "prev_deal_count", "new_deal_count", "neighbor_change_count")}, indent=2))


if __name__ == "__main__":
    main()
