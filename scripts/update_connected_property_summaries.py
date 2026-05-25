"""Replace each parent deal's 'Connected Properties' field with a richly-
formatted summary block per linked property, while preserving Joel's
hand-written notes as a separate '--- MANUAL NOTES (preserved) ---'
section.

Per linked property we render:
    1) <ADDRESS or "(no address)">
       https://bhinvestments.pipedrive.com/deal/<id>
       Asking: <price or -->  |  Vacant: <yes/no/-->  |  Rent if occupied: <amount or -->
       Owner LLC: <name or -->
       Wants to sell: <likely (asking price set) | yes (per notes) | unknown>

Wants-to-sell derivation:
- non-empty Asking Price                                         -> "likely (asking price set)"
- else any case-insensitive note phrase match (see PHRASES)      -> "yes (per notes)"
- else                                                            -> "unknown"

Safety:
- Dumps every CP field BEFORE state to
  state/connected_properties_backup_<YYYYMMDD>.json
- Free text outside parseable link tokens and outside a prior auto-summary
  block is preserved verbatim under MANUAL NOTES.
"""
from __future__ import annotations
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Set, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.pipedrive import PD

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "state")
LOG_DIR   = os.path.join(ROOT, "run_log")
os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

DATE = datetime.now(timezone.utc).strftime("%Y%m%d")
TS   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
BACKUP_PATH = os.path.join(STATE_DIR, f"connected_properties_backup_{DATE}.json")
OUT_PATH    = os.path.join(STATE_DIR, f"cp_summaries_{DATE}.json")
RUN_LOG     = os.path.join(LOG_DIR,   f"cp_summaries_run_{TS}.json")

# --- field keys ---
F_CP        = "c2ad93c83b417a9b1c946a9bc45db29d888c6698"  # Connected Properties (target)
F_ADDRESS   = "9fbde71e3b4dfb3d4228638da194a11ce34178ac"  # Address (google address widget)
F_ASKING    = "e54507e6fc6f7eacc29a86825565441e2f7164ac"  # Asking Price (varchar)
F_VACANT    = "525e5146faa2331dca1205ba889bb2a60447b5a7"  # Vacant? (set, ids 148=Yes 149=No 162=N/A)
F_RENT      = "3a74f006a2df7102e2f539c8f873693a58bf4056"  # If occupied how much is the rent? (text)
F_OWNER_LLC = "3ae0f8127bc897fdc4e4243332e99ad7271145cf"  # Owner LLC (varchar)

VACANT_MAP = {"148": "yes", "149": "no", "162": "n/a"}

PHRASES = [
    "wants to sell",
    "interested in selling",
    "open to offer",
    "for sale",
    "would consider",
    "willing to sell",
    "ready to sell",
]

AUTO_HEADER_RE = re.compile(
    r"^=== CONNECTED PROPERTIES \(auto-updated \d{4}-\d{2}-\d{2}\) ===\s*$",
    re.MULTILINE,
)
DEAL_URL_RE = re.compile(r"https?://[^\s]*pipedrive\.com/deal/(\d+)", re.IGNORECASE)
HEADER_FMT = "=== CONNECTED PROPERTIES (auto-updated {date}) ==="
MANUAL_HEADER = "--- MANUAL NOTES (preserved) ---"


def linked_ids_from_text(text: str) -> List[int]:
    seen: Set[int] = set()
    out: List[int] = []
    for m in DEAL_URL_RE.finditer(text or ""):
        did = int(m.group(1))
        if did not in seen:
            seen.add(did)
            out.append(did)
    return out


def split_manual_notes(current: str) -> str:
    """Return the free-text remainder that should be preserved as manual notes.

    Strategy:
      - If current value contains a prior auto header, look for our prior
        MANUAL block (after MANUAL_HEADER) and re-preserve it as-is.
      - Otherwise, strip out every deal URL line. What remains is preserved
        manual context (e.g. "Main Deal . Lot 89 . Block 3318",
        "Connected to:", "Manually Linked:", labeled links like
        "2798 MORRIS AVENUE - https://....").
    """
    if not current:
        return ""

    # Case 1: prior auto-summary present
    if AUTO_HEADER_RE.search(current):
        # Anything explicitly under our MANUAL_HEADER should be re-preserved
        if MANUAL_HEADER in current:
            tail = current.split(MANUAL_HEADER, 1)[1].strip()
            return tail
        # No manual block before -> nothing to preserve
        return ""

    # Case 2: first run on this deal. Preserve EVERYTHING the human wrote,
    # but drop pure-URL lines (those are reflected in the new structured
    # block). Keep label-style lines like "X - https://.../deal/123"
    # because they carry context the human added.
    preserved_lines: List[str] = []
    for raw in current.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            preserved_lines.append(line)
            continue
        # If line consists ONLY of a deal URL (no other content), skip it
        m = DEAL_URL_RE.search(stripped)
        if m:
            without_url = DEAL_URL_RE.sub("", stripped).strip(" -|:.,")
            if not without_url:
                continue  # pure URL line, drop
        preserved_lines.append(line)
    # Trim trailing/leading blank-only lines
    while preserved_lines and not preserved_lines[0].strip():
        preserved_lines.pop(0)
    while preserved_lines and not preserved_lines[-1].strip():
        preserved_lines.pop()
    return "\n".join(preserved_lines).strip()


def fmt_asking(v) -> Tuple[str, bool]:
    """Return (display, has_value)."""
    if v is None:
        return "--", False
    s = str(v).strip()
    if not s:
        return "--", False
    return s, True


def fmt_vacant(v) -> str:
    if v is None:
        return "--"
    s = str(v).strip()
    if not s:
        return "--"
    # Pipedrive 'set' values come back as comma-separated option IDs
    out: List[str] = []
    for token in s.split(","):
        t = token.strip()
        out.append(VACANT_MAP.get(t, t))
    return ", ".join(out) if out else "--"


def fmt_text(v) -> str:
    if v is None:
        return "--"
    s = str(v).strip()
    return s if s else "--"


def derive_wants_to_sell(asking_has_value: bool, notes: List[dict]) -> str:
    if asking_has_value:
        return "likely (asking price set)"
    blob_lc = "\n".join(((n.get("content") or "") for n in (notes or []))).lower()
    if any(p in blob_lc for p in PHRASES):
        return "yes (per notes)"
    return "unknown"


def address_or_title(deal: dict) -> str:
    addr = (deal.get(F_ADDRESS) or "").strip()
    if addr:
        return addr
    # Fall back to "formatted_address" subfield if present
    fa = (deal.get(F_ADDRESS + "_formatted_address") or "").strip()
    if fa:
        return fa
    title = (deal.get("title") or "").strip()
    return title or "(no address)"


def render_block(idx: int, linked_deal: dict, notes: List[dict]) -> Tuple[str, str]:
    """Return (rendered_text, wants_to_sell_category)."""
    did = linked_deal.get("id")
    addr = address_or_title(linked_deal)
    asking_disp, asking_has = fmt_asking(linked_deal.get(F_ASKING))
    vacant_disp = fmt_vacant(linked_deal.get(F_VACANT))
    rent_disp = fmt_text(linked_deal.get(F_RENT))
    owner_disp = fmt_text(linked_deal.get(F_OWNER_LLC))
    wts = derive_wants_to_sell(asking_has, notes)
    block = (
        f"{idx}) {addr}\n"
        f"   https://bhinvestments.pipedrive.com/deal/{did}\n"
        f"   Asking: {asking_disp}  |  Vacant: {vacant_disp}  |  Rent if occupied: {rent_disp}\n"
        f"   Owner LLC: {owner_disp}\n"
        f"   Wants to sell: {wts}"
    )
    return block, wts


def fetch_linked_deal(pd: PD, deal_id: int, cache: Dict[int, dict]) -> dict | None:
    if deal_id in cache:
        return cache[deal_id]
    st, payload = pd.get(f"/deals/{deal_id}")
    if st != 200 or not payload.get("data"):
        cache[deal_id] = None
        return None
    cache[deal_id] = payload["data"]
    return payload["data"]


def fetch_notes(pd: PD, deal_id: int, cache: Dict[int, List[dict]]) -> List[dict]:
    if deal_id in cache:
        return cache[deal_id]
    # /deals/{id}/notes returns all notes
    out: List[dict] = []
    try:
        for n in pd.paged(f"/deals/{deal_id}/notes"):
            out.append(n)
    except Exception:
        # If the inner pagination throws, fall back to a single GET
        st, p = pd.get(f"/deals/{deal_id}/notes", params={"limit": 500})
        if st == 200:
            out = (p.get("data") or [])
    cache[deal_id] = out
    return out


def main():
    pd = PD()

    # ---- 1. Pull all deals, build backup of every non-empty CP field ----
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Pulling all deals...", flush=True)
    deals = list(pd.paged("/deals", {"status": "all_not_deleted"}))
    print(f"  total deals: {len(deals)}", flush=True)

    parent_deals = [d for d in deals if (d.get(F_CP) or "").strip()]
    print(f"  parent deals with non-empty Connected Properties: {len(parent_deals)}", flush=True)

    backup = {
        "ts": TS,
        "field_key": F_CP,
        "count": len(parent_deals),
        "deals": {str(d["id"]): d.get(F_CP) for d in parent_deals},
    }
    with open(BACKUP_PATH, "w", encoding="utf-8") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    backup_size = os.path.getsize(BACKUP_PATH)
    print(f"  backup written: {BACKUP_PATH} ({backup_size} bytes)", flush=True)

    # Index of every deal already in our fresh pull -- saves API roundtrips
    by_id: Dict[int, dict] = {int(d["id"]): d for d in deals}

    # ---- 2. Build each parent's new CP value ----
    notes_cache: Dict[int, List[dict]] = {}
    miss_cache: Dict[int, dict] = {}

    updated = 0
    skipped_unchanged = 0
    total_blocks = 0
    counts = {"likely (asking price set)": 0, "yes (per notes)": 0, "unknown": 0}
    errors: List[dict] = []
    preserved_example: dict | None = None
    sample_7870: dict | None = None
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for parent in parent_deals:
        pid = int(parent["id"])
        try:
            current = parent.get(F_CP) or ""
            linked_ids = linked_ids_from_text(current)
            manual_notes = split_manual_notes(current)

            blocks: List[str] = []
            for i, lid in enumerate(linked_ids, start=1):
                ld = by_id.get(lid)
                if ld is None:
                    ld = fetch_linked_deal(pd, lid, miss_cache)
                if ld is None:
                    blocks.append(
                        f"{i}) (linked deal {lid} not accessible)\n"
                        f"   https://bhinvestments.pipedrive.com/deal/{lid}\n"
                        f"   Asking: --  |  Vacant: --  |  Rent if occupied: --\n"
                        f"   Owner LLC: --\n"
                        f"   Wants to sell: unknown"
                    )
                    counts["unknown"] += 1
                    total_blocks += 1
                    continue
                # Only fetch notes if asking is empty (saves API calls)
                _, asking_has = fmt_asking(ld.get(F_ASKING))
                notes = [] if asking_has else fetch_notes(pd, lid, notes_cache)
                block, wts = render_block(i, ld, notes)
                blocks.append(block)
                counts[wts] = counts.get(wts, 0) + 1
                total_blocks += 1

            parts: List[str] = [HEADER_FMT.format(date=today_str), ""]
            if blocks:
                parts.append("\n\n".join(blocks))
            else:
                parts.append("(no parseable linked deals in source field)")
            if manual_notes:
                parts.append("")
                parts.append(MANUAL_HEADER)
                parts.append(manual_notes)
            new_value = "\n".join(parts).rstrip() + "\n"

            if new_value.strip() == (current or "").strip():
                skipped_unchanged += 1
                continue

            st, payload = pd.put(f"/deals/{pid}", {F_CP: new_value})
            if st == 200:
                updated += 1
                if pid == 7870:
                    sample_7870 = {"deal_id": pid, "before": current, "after": new_value}
                if manual_notes and preserved_example is None:
                    preserved_example = {"deal_id": pid, "before": current, "after": new_value}
            else:
                errors.append({"deal_id": pid, "phase": "PUT", "status": st, "payload": payload})
        except Exception as e:
            errors.append({"deal_id": pid, "phase": "EXC", "err": repr(e)})

    summary = {
        "ts": TS,
        "date": DATE,
        "parent_deals_processed": len(parent_deals),
        "updated": updated,
        "skipped_unchanged": skipped_unchanged,
        "total_blocks_rendered": total_blocks,
        "wants_to_sell_counts": counts,
        "errors": errors,
        "backup_path": BACKUP_PATH,
        "backup_size_bytes": backup_size,
        "sample_7870": sample_7870,
        "preserved_example": preserved_example,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    with open(RUN_LOG, "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in summary.items() if k not in ("sample_7870", "preserved_example")},
                  f, indent=2)
    print(json.dumps({
        "parent_deals_processed": summary["parent_deals_processed"],
        "updated": summary["updated"],
        "skipped_unchanged": summary["skipped_unchanged"],
        "total_blocks_rendered": summary["total_blocks_rendered"],
        "wants_to_sell_counts": summary["wants_to_sell_counts"],
        "errors_count": len(errors),
    }, indent=2))


if __name__ == "__main__":
    main()
