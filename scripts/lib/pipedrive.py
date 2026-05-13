"""Thin Pipedrive v1 client. No SDK -- stdlib + requests for retries."""
import json
import os
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

API_BASE = "https://api.pipedrive.com/v1"
COMPANY_SUBDOMAIN = "bhinvestments"

DEFAULT_PACE_S = 0.18  # ~5.5 req/s, well under 100/10s


def _token() -> str:
    tok = os.environ.get("PIPEDRIVE_TOKEN") or os.environ.get("PD_TOKEN")
    if not tok:
        raise SystemExit("PIPEDRIVE_TOKEN env var required")
    return tok


class PD:
    def __init__(self, pace_s: float = DEFAULT_PACE_S):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": "bh-daily/1.0"})
        self.pace_s = pace_s
        self._tok = _token()

    def _req(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
             json_body: Optional[Dict[str, Any]] = None,
             retries: int = 5, base: float = 0.5) -> Tuple[int, Dict[str, Any]]:
        qp = dict(params or {})
        qp["api_token"] = self._tok
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(qp)}"
        last = None
        for i in range(retries):
            try:
                r = self.s.request(method, url, json=json_body, timeout=30)
                if r.status_code in (429, 502, 503, 504):
                    time.sleep(base * (2 ** i)); last = r; continue
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"raw": r.text}
            except Exception as e:
                last = e; time.sleep(base * (2 ** i))
        if isinstance(last, requests.Response):
            return last.status_code, {"raw": last.text}
        return 0, {"err": str(last)}

    def get(self, path, params=None):
        time.sleep(self.pace_s)
        return self._req("GET", path, params=params)

    def put(self, path, body, params=None):
        time.sleep(self.pace_s)
        return self._req("PUT", path, params=params, json_body=body)

    def post(self, path, body, params=None):
        time.sleep(self.pace_s)
        return self._req("POST", path, params=params, json_body=body)

    def paged(self, path: str, params: Optional[Dict[str, Any]] = None,
              limit: int = 500) -> Iterable[Dict[str, Any]]:
        start = 0
        params = dict(params or {})
        while True:
            params["limit"] = limit
            params["start"] = start
            st, payload = self._req("GET", path, params=params)
            if st != 200:
                raise RuntimeError(f"GET {path} status={st} payload={payload}")
            for row in (payload.get("data") or []):
                yield row
            pi = (payload.get("additional_data") or {}).get("pagination") or {}
            if not pi.get("more_items_in_collection"):
                return
            start = pi.get("next_start", start + limit)
            time.sleep(self.pace_s)


# ---- Custom-field key constants used across the daily run ----
F_ADDRESS              = "9fbde71e3b4dfb3d4228638da194a11ce34178ac"
F_ACRIS_LINKS          = "35bfce2c2e0c487c5d69aa67a697797b668fab56"
F_ZONING               = "935a15b4f617c44200a35d5cfeaa31421054282d"
F_ZOLA_LINK            = "900f3c0c32bb51c1f2030dc5caffe0d2bb50f199"
F_PROPERTYSHARK_LINK   = "7e82150a9c74390ac3056ec3ff7cdee3a396291c"
F_GOOGLE_MAPS          = "ac0ea1391fc807573bbc6417f53c0bce0552e40a"
F_LOT_AREA             = "db10cac1fc32278e4838f3747be3f4ca4e31a19e"
F_BUILDING_AREA        = "359e1cfb5f759c69d53d6ba96dc9baf40bb051f5"
F_LOT_FRONTAGE         = "8f5a93d65b10fc0e5878de35d210474f76c48e09"
F_LOT_DEPTH            = "4becf08e11d3ed836a43b91476ce0fb6b6c252b6"
# Lead source / connected properties / resid_far -- resolved at runtime via /dealFields
# (their UUIDs live in `state/field_keys.json` once detect_neighbor_changes.py runs).
