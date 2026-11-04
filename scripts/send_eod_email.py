"""Daily EOD email - BH Investments agent call report.

Sends to mikekent389 + CC mayerbh26 via Gmail SMTP, Monday to Friday at 7:30 PM ET.

Where it runs (since 11/04/2026):
  * Railway service `bh-eod`, cron `30 23,0 * * *` (UTC) -> `--mode window`: sends only when the
    clock in New York is inside the 7:15-8:15 PM window on a weekday (the two UTC slots cover
    EST and EDT; the one that lands at 6:30 PM or 8:30 PM ET exits without sending).
  * GitHub Actions `eod.yml`, cron `45 1 * * *` (UTC) -> `--mode fallback`: later the same night,
    sends the report for the last weekday ONLY if the Gmail Sent folder has no copy of it yet
    (GitHub starts scheduled runs hours late, which is fine for a safety net).
  * `--mode now [--date YYYY-MM-DD] [--test] [--dry-run]` for manual runs.

Credentials from env vars (GitHub secrets / Railway variables) with local .env fallback:
  AIRCALL_API_ID, AIRCALL_API_TOKEN, PIPEDRIVE_TOKEN, GMAIL_APP_PASSWORD

Connected = outbound call with duration > 45 seconds.
The report window is the ET calendar day: midnight -> 7:30 PM when sent from the window run,
midnight -> midnight when sent by the fallback or for a past date.
"""
import argparse, base64, email, email.header, imaplib, json, os, re, smtplib, sys, time
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.error import HTTPError
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import urllib.request

ET = ZoneInfo("America/New_York")
SEND_AT = (19, 30)                       # 7:30 PM ET
WINDOW = (19 * 60 + 15, 20 * 60 + 15)    # minutes after midnight ET accepted by --mode window
GMAIL_USER = "mikekent389@gmail.com"
CC = ["mayerbh26@gmail.com"]
AGENTS = [
    ("Shyrine Asuncion", 1888950),
    ("Regina Magat",     1885656),
    ("Huney Blanes",     1926790),
    ("Jason Garcia",     1672542),
    ("Leo Davis",        None),    # auto-detected from Aircall users
    ("David James",      None),
]


def env(key):
    """Env var first (CI / Railway); .env file fallback (local runs)."""
    v = os.environ.get(key)
    if v:
        return v
    for path in ("F:/Work/Joel/bh-enrichment/.env",
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".env")):
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    if "=" in line:
                        k, val = line.strip().split("=", 1)
                        if k == key:
                            return val
    raise KeyError(f"missing credential: {key}")


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- scheduling
def decide(mode, now_et, forced=None):
    """Return (report_date, skip_reason). skip_reason None = send."""
    if forced:
        return forced, None
    d = now_et.date()
    if mode == "now":
        return d, None
    if mode == "window":
        if d.weekday() >= 5:
            return d, f"{d:%A} is a weekend in ET - nothing to send"
        mins = now_et.hour * 60 + now_et.minute
        if not (WINDOW[0] <= mins < WINDOW[1]):
            return d, f"{now_et:%H:%M} ET is outside the 7:15-8:15 PM send window"
        return d, None
    if mode == "fallback":
        if (now_et.hour, now_et.minute) < SEND_AT:
            d = d - timedelta(days=1)          # last day whose 7:30 PM has passed
        if d.weekday() >= 5:
            return d, f"report day {d} is a {d:%A} - nothing to send"
        return d, None
    raise SystemExit(f"unknown mode {mode}")


def window_for(d, now_et):
    start = datetime.combine(d, dtime(0, 0), tzinfo=ET)
    end = start + timedelta(days=1)
    partial = now_et < end
    if partial:
        end = now_et
    return int(start.timestamp()), int(end.timestamp()), partial


def subject_for(d):
    return f"BH Investments \u2014 Daily EOD \u2014 {d:%a} {d.month}/{d.day}/{d.year}"


def already_sent(subject, gmail_pass, days=4):
    """True if a message with exactly this subject sits in the Gmail Sent folder (last `days` days)."""
    try:
        m = imaplib.IMAP4_SSL("imap.gmail.com")
        m.login(GMAIL_USER, gmail_pass)
        typ, _ = m.select('"[Gmail]/Sent Mail"', readonly=True)
        if typ != "OK":
            log("already-sent check: cannot open Sent Mail, treating as not sent")
            return False
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = m.search(None, "SINCE", since)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        found = False
        for i in ids[-300:]:
            typ, parts = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
            raw = b"".join(p[1] for p in parts if isinstance(p, tuple))
            subj = email.message_from_bytes(raw).get("Subject", "")
            subj = str(email.header.make_header(email.header.decode_header(subj))).strip()
            if subj == subject:
                found = True
                break
        m.logout()
        return found
    except Exception as e:
        log(f"already-sent check failed ({type(e).__name__}: {e}) - treating as not sent")
        return False


# ---------------------------------------------------------------- data
def http_json(url, headers=None, tries=4):
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            if e.code == 429 and n < tries - 1:
                wait = 5 * (2 ** n)
                log(f"HTTP 429 from {url.split('?')[0]} - waiting {wait}s")
                time.sleep(wait)
                continue
            raise


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else None


def build_report(d, ts_from, ts_to, partial, creds):
    ac_auth = base64.b64encode(f"{creds['AIRCALL_API_ID']}:{creds['AIRCALL_API_TOKEN']}".encode()).decode()
    pd_token = creds["PIPEDRIVE_TOKEN"]

    def ac(path, params=None):
        qs = "?" + urlencode(params or {}) if params else ""
        return http_json(f"https://api.aircall.io/v1{path}{qs}", {"Authorization": f"Basic {ac_auth}"})

    def pd(path, params=None):
        params = dict(params or {}); params["api_token"] = pd_token
        return http_json(f"https://api.pipedrive.com/v1{path}?{urlencode(params)}")

    agents = list(AGENTS)
    for u in ac("/users", {"per_page": 50}).get("users", []):
        name = u.get("name", ""); uid = u.get("id")
        for i, (an, aid) in enumerate(agents):
            if aid is None and an.lower() in name.lower():
                agents[i] = (an, uid)

    all_calls = []; page = 1
    while True:
        r = ac("/calls", {"from": ts_from, "to": ts_to, "per_page": 50, "page": page})
        batch = r.get("calls", [])
        all_calls.extend(batch)
        if len(batch) < 50 or page >= 50:
            break
        page += 1

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

    cache = {}

    def deal_title_for_phone(phone_n):
        for pid in phone_to_persons.get(phone_n, []):
            if pid in cache:
                return cache[pid]
            deals = pd(f"/persons/{pid}/deals", {"limit": 3}).get("data") or []
            if deals:
                title = (deals[0].get("title", "") or "").split(",")[0].split("/")[0].strip()
                cache[pid] = title
                return title
            cache[pid] = None
        return None

    stats = []
    for name, uid in agents:
        if not uid:
            stats.append({"name": name, "dialed": 0, "connected": 0, "addrs": [], "non_crm": 0, "first_day": True})
            continue
        out = [c for c in all_calls if (c.get("user") or {}).get("id") == uid and c.get("direction") == "outbound"]
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
        stats.append({"name": name, "dialed": len(out), "connected": len(connected),
                      "addrs": addrs, "non_crm": non_crm, "first_day": False})

    total_dialed = sum(s["dialed"] for s in stats)
    total_connected = sum(s["connected"] for s in stats)
    date_h = f"{d:%A}, {d:%B} {d.day}, {d.year}"
    if partial:
        date_h += f" (calls through {datetime.fromtimestamp(ts_to, ET):%#I:%M %p} ET)" if os.name == "nt" \
            else f" (calls through {datetime.fromtimestamp(ts_to, ET):%-I:%M %p} ET)"

    html = f"""<html><body style="font-family:Calibri,Arial,sans-serif;font-size:14px;color:#222;">
<h2 style="color:#2D5F8B;margin-bottom:0">BH Investments &mdash; Daily EOD</h2>
<div style="color:#666;margin-bottom:18px">{date_h}</div>

<table style="border-collapse:collapse;border:1px solid #ccc">
<thead><tr style="background:#2D5F8B;color:#fff">
  <th style="padding:8px 12px;text-align:left">Agent</th>
  <th style="padding:8px 12px;text-align:right">Dialed</th>
  <th style="padding:8px 12px;text-align:right">Connected</th>
  <th style="padding:8px 12px;text-align:left">Interested</th>
</tr></thead><tbody>"""
    for s in stats:
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
    for s in stats:
        if s["first_day"]:
            html += f"<p><b>{s['name']}:</b> <i>first day on Aircall &mdash; no deal mappings yet</i></p>"
            continue
        if not s["addrs"]:
            html += f"<p><b>{s['name']} ({s['connected']}):</b> <i>{s['non_crm']} non-CRM contacts</i></p>"
            continue
        addrs_str = " &middot; ".join(s["addrs"])
        extra = f" <i>(+ {s['non_crm']} non-CRM contacts)</i>" if s["non_crm"] else ""
        html += f"<p><b>{s['name']} ({s['connected']}):</b> {addrs_str}{extra}</p>"

    html += """<hr style="margin-top:30px;border:none;border-top:1px solid #ddd">
<p style="color:#888;font-size:11px;font-style:italic">
Auto-generated from Aircall + Pipedrive &middot; sent Monday to Friday at 7:30 PM ET.<br>
Reply with each agent's "Interested" addresses to add to tomorrow's report.
</p>
</body></html>"""
    return html, total_dialed, total_connected


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["window", "fallback", "now"], default="now")
    ap.add_argument("--date", help="YYYY-MM-DD: full-day report for that ET date (implies --mode now)")
    ap.add_argument("--test", action="store_true", help="send only to mikekent389 with a [TEST] subject")
    ap.add_argument("--dry-run", action="store_true", help="build the report, send nothing")
    a = ap.parse_args()

    now_et = datetime.now(ET)
    forced = date.fromisoformat(a.date) if a.date else None
    d, skip = decide("now" if forced else a.mode, now_et, forced)
    log(f"now {now_et:%Y-%m-%d %H:%M %Z} | mode {a.mode} | report day {d} ({d:%A})")
    if skip:
        log("SKIP:", skip)
        return 0

    creds = {k: env(k) for k in ("AIRCALL_API_ID", "AIRCALL_API_TOKEN", "PIPEDRIVE_TOKEN", "GMAIL_APP_PASSWORD")}
    subject = subject_for(d)
    if a.test:
        subject = "[TEST] " + subject
    elif not a.dry_run and already_sent(subject, creds["GMAIL_APP_PASSWORD"]):
        log(f"SKIP: '{subject}' is already in the Sent folder")
        return 0

    ts_from, ts_to, partial = window_for(d, now_et)
    log(f"window {datetime.fromtimestamp(ts_from, ET):%m/%d %H:%M} -> {datetime.fromtimestamp(ts_to, ET):%m/%d %H:%M} ET"
        + (" (partial day)" if partial else ""))
    html, dialed, connected = build_report(d, ts_from, ts_to, partial, creds)
    log(f"report built: total_dialed={dialed} total_connected={connected}")
    if a.dry_run:
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"eod_preview_{d}.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(html)
        log("DRY RUN - preview written to", out)
        return 0

    to = [GMAIL_USER]; cc = [] if a.test else list(CC)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg.attach(MIMEText(html, "html"))
    log(f"Sending '{subject}' to {', '.join(to + cc)} ...")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as srv:
        srv.login(GMAIL_USER, creds["GMAIL_APP_PASSWORD"])
        srv.send_message(msg, from_addr=GMAIL_USER, to_addrs=to + cc)
    log(f"SENT {d}  total_dialed={dialed}  total_connected={connected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
