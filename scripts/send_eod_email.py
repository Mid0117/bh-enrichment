"""Daily EOD email — BH Investments agent call report.

Sends to mikekent389 + CC mayerbh26 via Gmail SMTP.
Reports on the ET calendar day that just ended (run early-UTC-morning by cron).

Credentials from env vars (GitHub Actions secrets) with local .env fallback:
  AIRCALL_API_ID, AIRCALL_API_TOKEN, PIPEDRIVE_TOKEN, GMAIL_APP_PASSWORD

Connected = outbound call with duration > 45 seconds.
"""
import json, os, base64, re, smtplib, sys
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urlencode
import urllib.request


def env(key):
    """Env var first (GitHub Actions); .env file fallback (local runs)."""
    v = os.environ.get(key)
    if v:
        return v
    for path in ("F:/Work/Joel/bh-enrichment/.env",
                 os.path.join(os.path.dirname(__file__), "..", "..", ".env")):
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if "=" in line:
                        k, val = line.strip().split("=", 1)
                        if k == key:
                            return val
    raise KeyError(f"missing credential: {key}")


AC_ID = env("AIRCALL_API_ID")
AC_TOKEN = env("AIRCALL_API_TOKEN")
PD_TOKEN = env("PIPEDRIVE_TOKEN")
GMAIL_USER = "mikekent389@gmail.com"
GMAIL_PASS = env("GMAIL_APP_PASSWORD")
auth_ac = base64.b64encode(f"{AC_ID}:{AC_TOKEN}".encode()).decode()

# --- Dynamic date: the ET calendar day that just ended ---
# ET day boundary = 04:00 UTC. Cron fires ~05:00 UTC, so yesterday-UTC == the
# ET day that closed an hour ago.
_now = datetime.now(timezone.utc)
target_date = (_now - timedelta(days=1)).date()
ts_from = int(datetime(target_date.year, target_date.month, target_date.day, 4, 0, 0, tzinfo=timezone.utc).timestamp())
ts_to = ts_from + 86400
TARGET = target_date.isoformat()

AGENTS = [
    ("Shyrine Asuncion", 1888950),
    ("Regina Magat",     1885656),
    ("Huney Blanes",     1926790),
    ("Jason Garcia",     1672542),
    ("Leo Davis",        None),    # auto-detected from Aircall users
    ("David James",      None),
]


def ac(path, params=None):
    qs = "?" + urlencode(params or {}) if params else ""
    url = f"https://api.aircall.io/v1{path}{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth_ac}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def pd(path, params=None):
    params = dict(params or {}); params["api_token"] = PD_TOKEN
    url = f"https://api.pipedrive.com/v1{path}?{urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode())


# Auto-detect Leo + David user IDs
users_resp = ac("/users", {"per_page": 50})
for u in users_resp.get("users", []):
    name = u.get("name", "")
    uid = u.get("id")
    for i, (an, aid) in enumerate(AGENTS):
        if aid is None and an.lower() in name.lower():
            AGENTS[i] = (an, uid)

# Pull all calls in window
all_calls = []; page = 1
while True:
    r = ac("/calls", {"from": ts_from, "to": ts_to, "per_page": 50, "page": page})
    batch = r.get("calls", [])
    all_calls.extend(batch)
    if len(batch) < 50:
        break
    page += 1
    if page > 50:
        break


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else None


# phone -> person -> deal index
persons = []; start = 0
while True:
    r = pd("/persons", {"start": start, "limit": 500})
    persons.extend(r.get("data") or [])
    more = (r.get("additional_data", {}) or {}).get("pagination", {}) or {}
    if not more.get("more_items_in_collection"):
        break
    start = more.get("next_start", start + 500)
phone_to_persons = {}
for p in persons:
    for ph in p.get("phone") or []:
        v = ph.get("value") if isinstance(ph, dict) else ph
        n = norm_phone(v)
        if n:
            phone_to_persons.setdefault(n, []).append(p["id"])

person_deal_cache = {}
def deal_title_for_phone(phone_n):
    for pid in phone_to_persons.get(phone_n, []):
        if pid in person_deal_cache:
            return person_deal_cache[pid]
        r = pd(f"/persons/{pid}/deals", {"limit": 3})
        deals = r.get("data") or []
        if deals:
            title = (deals[0].get("title", "") or "").split(",")[0].split("/")[0].strip()
            person_deal_cache[pid] = title
            return title
        person_deal_cache[pid] = None
    return None


# Per-agent stats
agent_stats = []
for name, uid in AGENTS:
    if not uid:
        agent_stats.append({"name": name, "dialed": 0, "connected": 0, "addrs": [], "non_crm": 0, "first_day": True})
        continue
    calls = [c for c in all_calls if (c.get("user") or {}).get("id") == uid]
    out = [c for c in calls if c.get("direction") == "outbound"]
    connected = [c for c in out if (c.get("duration") or 0) > 45]
    addrs = []; non_crm = 0; seen = set()
    for c in connected:
        ph = norm_phone(c.get("raw_digits") or c.get("number", ""))
        if not ph:
            non_crm += 1; continue
        title = deal_title_for_phone(ph)
        if title and title not in seen:
            seen.add(title); addrs.append(title)
        elif not title:
            non_crm += 1
    agent_stats.append({"name": name, "dialed": len(out), "connected": len(connected),
                        "addrs": addrs, "non_crm": non_crm, "first_day": False})

total_dialed = sum(s["dialed"] for s in agent_stats)
total_connected = sum(s["connected"] for s in agent_stats)

# HTML
date_h = target_date.strftime("%A, %B %d, %Y")
html = f"""<html><body style="font-family:Calibri,Arial,sans-serif;font-size:14px;color:#222;">
<h2 style="color:#2D5F8B;margin-bottom:0">BH Investments — Daily EOD</h2>
<div style="color:#666;margin-bottom:18px">{date_h}</div>

<table style="border-collapse:collapse;border:1px solid #ccc">
<thead><tr style="background:#2D5F8B;color:#fff">
  <th style="padding:8px 12px;text-align:left">Agent</th>
  <th style="padding:8px 12px;text-align:right">Dialed</th>
  <th style="padding:8px 12px;text-align:right">Connected</th>
  <th style="padding:8px 12px;text-align:left">Interested</th>
</tr></thead><tbody>"""
for s in agent_stats:
    html += f"""<tr>
  <td style="padding:6px 12px;border-bottom:1px solid #eee"><b>{s['name']}</b></td>
  <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right">{s['dialed']}</td>
  <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:right"><b>{s['connected']}</b></td>
  <td style="padding:6px 12px;border-bottom:1px solid #eee;color:#888">(manual)</td>
</tr>"""
html += f"""<tr style="background:#f5f5f5;font-weight:bold">
  <td style="padding:8px 12px">TEAM TOTAL</td>
  <td style="padding:8px 12px;text-align:right">{total_dialed}</td>
  <td style="padding:8px 12px;text-align:right">{total_connected}</td>
  <td></td>
</tr></tbody></table>

<p style="color:#666;font-size:12px;margin-top:6px;font-style:italic">
Connected = outbound calls with real conversation (duration &gt; 45 seconds).
</p>

<h3 style="color:#2D5F8B;margin-top:24px">Today's connected addresses</h3>"""
for s in agent_stats:
    if s["first_day"]:
        html += f"<p><b>{s['name']}:</b> <i>first day on Aircall — no deal mappings yet</i></p>"
        continue
    if not s["addrs"]:
        html += f"<p><b>{s['name']} ({s['connected']}):</b> <i>{s['non_crm']} non-CRM contacts</i></p>"
        continue
    addrs_str = " · ".join(s["addrs"])
    extra = f" <i>(+ {s['non_crm']} non-CRM contacts)</i>" if s["non_crm"] else ""
    html += f"<p><b>{s['name']} ({s['connected']}):</b> {addrs_str}{extra}</p>"

html += """<hr style="margin-top:30px;border:none;border-top:1px solid #ddd">
<p style="color:#888;font-size:11px;font-style:italic">
Auto-generated from Aircall + Pipedrive · sent daily after end of day.<br>
Reply with each agent's "Interested" addresses to add to tomorrow's report.
</p>
</body></html>"""

# SMTP send
subject_date = target_date.strftime("%a %-m/%-d/%Y") if os.name != "nt" else target_date.strftime("%a %#m/%#d/%Y")
msg = MIMEMultipart("alternative")
msg["Subject"] = f"BH Investments — Daily EOD — {subject_date}"
msg["From"] = GMAIL_USER
msg["To"] = GMAIL_USER
msg["Cc"] = "mayerbh26@gmail.com"
msg.attach(MIMEText(html, "html"))

print(f"Sending {TARGET} EOD to {GMAIL_USER} + CC mayerbh26@gmail.com ...")
with smtplib.SMTP("smtp.gmail.com", 587) as srv:
    srv.starttls()
    srv.login(GMAIL_USER, GMAIL_PASS)
    srv.send_message(msg, from_addr=GMAIL_USER, to_addrs=[GMAIL_USER, "mayerbh26@gmail.com"])
print(f"SENT {TARGET}  total_dialed={total_dialed}  total_connected={total_connected}")
