"""For each (deal, neighbor_deal_ids) pair flagged today, append the new
neighbor's Pipedrive deal URL to the existing 'Connected Properties' field
of the OTHER deal(s) on the same block. v7 Fix A3 format:
  https://bhinvestments.pipedrive.com/deal/{deal_id}

Idempotent: GET-before-PUT, only append URLs not already present.

Writes are minimal -- one PUT per affected deal. Errors collected for the
email report.
"""
from __future__ import annotations
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.pipedrive import PD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR   = os.path.join(ROOT, "run_log")

DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
NC_PATH = os.path.join(STATE_DIR, f"neighbor_changes_{DATE}.json")
OUT_PATH = os.path.join(STATE_DIR, f"cp_updates_{DATE}.json")


def resolve_cp_field_key(pd: PD) -> str:
    """Look up the Connected Properties field key dynamically each run -- it's
    a created field whose UUID lives in Pipedrive metadata."""
    cache_path = os.path.join(STATE_DIR, "field_keys.json")
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("Connected Properties"):
            return cache["Connected Properties"]
    fields = list(pd.paged("/dealFields"))
    keys = {}
    for f in fields:
        nm = (f.get("name") or "").strip()
        if nm in ("Connected Properties", "ACRIS Links", "Lead Source", "Resid FAR"):
            keys[nm] = f.get("key")
    if not keys.get("Connected Properties"):
        raise SystemExit("Connected Properties field not found in /dealFields")
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(keys, f, indent=2)
    return keys["Connected Properties"]


def deal_url(deal_id) -> str:
    return f"https://bhinvestments.pipedrive.com/deal/{deal_id}"


def main():
    if not os.path.exists(NC_PATH):
        print(f"no neighbor_changes file at {NC_PATH}; skipping", file=sys.stderr)
        json.dump({"updated": 0, "errors": [], "skipped": "no_neighbor_changes"}, open(OUT_PATH, "w", encoding="utf-8"))
        return

    nc = json.load(open(NC_PATH, encoding="utf-8"))
    changes = nc.get("changes") or []
    if not changes:
        json.dump({"updated": 0, "errors": []}, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
        print(json.dumps({"updated": 0}, indent=2))
        return

    pd = PD()
    cp_key = resolve_cp_field_key(pd)

    updated = 0; errors = []
    log_rows = []

    # Build per-existing-deal task list: each existing deal needs the new deal
    # added, and the new deal needs all existing neighbors added.
    work = {}  # deal_id -> set of neighbor_deal_ids to ensure
    for ch in changes:
        new_id = ch["new_deal_id"]
        existing = ch["existing_neighbor_deal_ids"]
        for ex in existing:
            work.setdefault(ex, set()).add(new_id)
        work.setdefault(new_id, set()).update(existing)

    for did, neighbors in work.items():
        try:
            st, payload = pd.get(f"/deals/{did}")
            if st != 200 or not payload.get("data"):
                errors.append({"deal_id": did, "phase": "GET", "status": st})
                continue
            d = payload["data"]
            current_cp = (d.get(cp_key) or "").strip()
            existing_urls = [u.strip() for u in current_cp.split("\n") if u.strip()] if current_cp else []
            existing_set = set(existing_urls)
            to_add = [deal_url(n) for n in neighbors if deal_url(n) not in existing_set]
            if not to_add:
                log_rows.append({"deal_id": did, "skip": "all_present"})
                continue
            new_cp = "\n".join(existing_urls + to_add)
            st2, payload2 = pd.put(f"/deals/{did}", {cp_key: new_cp})
            if st2 == 200:
                updated += 1
                log_rows.append({"deal_id": did, "added": to_add})
            else:
                errors.append({"deal_id": did, "phase": "PUT", "status": st2, "payload": payload2})
        except Exception as e:
            errors.append({"deal_id": did, "phase": "EXC", "err": repr(e)})

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "neighbor_changes_count": len(changes),
        "deals_touched": len(work),
        "updated": updated,
        "errors": errors,
        "log": log_rows,
    }
    json.dump(out, open(OUT_PATH, "w", encoding="utf-8"), indent=2)
    print(json.dumps({k: out[k] for k in ("deals_touched", "updated")}, indent=2))


if __name__ == "__main__":
    main()
