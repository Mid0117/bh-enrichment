"""PLUTO helpers for the daily run.

The daily run does NOT re-resolve PLUTO from scratch (that's a one-time job in
the dry-run scripts). Instead, it relies on the per-deal block/lot/boro that
was already written into Pipedrive. This module just builds the canonical
URLs from a (boro, block, lot) tuple, mirroring enrich_v7.py exactly.
"""
from __future__ import annotations
import urllib.parse
from typing import Optional

BORO_NAME = {"MN": "MANHATTAN", "BK": "BROOKLYN", "BX": "BRONX", "QN": "QUEENS", "SI": "STATEN ISLAND"}
BORO_NUM  = {"MN": 1, "BX": 2, "BK": 3, "QN": 4, "SI": 5}


def acris_url(boro_code: str, block, lot) -> Optional[str]:
    bnum = BORO_NUM.get(boro_code)
    if not bnum:
        return None
    try:
        b = str(int(float(block))); l = str(int(float(lot)))
    except Exception:
        return None
    name_q = urllib.parse.quote(BORO_NAME.get(boro_code, ""))
    return (
        f"https://a836-acris.nyc.gov/DS/DocumentSearch/BBLResult"
        f"?hid_borough={bnum}&hid_block={b}&hid_lot={l}"
        f"&hid_borough_name={name_q}&hid_max_rows=50&hid_page=1&hid_SearchType=BBL"
    )


def parse_bbl(bbl: str):
    """Return (boro_code, block, lot) from a 10-digit BBL string."""
    s = "".join(ch for ch in (bbl or "") if ch.isdigit())
    if len(s) != 10:
        return None, None, None
    bnum = int(s[0])
    boro = {1: "MN", 2: "BX", 3: "BK", 4: "QN", 5: "SI"}.get(bnum)
    return boro, int(s[1:6]), int(s[6:])


def block_key(boro_code: str, block) -> Optional[str]:
    if not boro_code:
        return None
    try:
        b = int(float(block))
    except Exception:
        return None
    return f"{boro_code}-{b}"
