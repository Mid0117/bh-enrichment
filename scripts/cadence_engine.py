"""Path B daily cadence engine for Agent Follow-Up pipeline.

Runs daily. For each deal in Stage 2 (Active Outreach):
- Determine current cadence step from existing 'Touch N' activities
- Compute next touch's due date from stage-entry date + days delta
- Create the next activity if due (idempotent: skip if already created)
- After Touch 5 done + 1 day grace + no inbound: auto-move to Stage 6 (Unreachable)

Usage:
  python cadence_engine.py            # live run
  python cadence_engine.py --dry-run  # show what would happen, no writes
"""
import json, os, sys, time, csv
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode
import urllib.request, urllib.error

TOKEN = os.environ.get("PIPEDRIVE_TOKEN") or os.environ.get("PD_TOKEN")
if not TOKEN: print("ERROR: PIPEDRIVE_TOKEN env var required", file=sys.stderr); sys.exit(2)

PIPELINE_ID = 20  # Agent Follow-Up
STAGE_ACTIVE = 218  # Active Outreach
STAGE_UNREACHABLE = 222
STAGE_QUAL = 219  # Connected - Qualification (just for reference)

DRY_RUN = "--dry-run" in sys.argv
DAYS_BY_STEP = {1: 0, 2: 1, 3: 3, 4: 7, 5: 14}
TOTAL_STEPS = 5

BASE = "https://api.pipedrive.com/v1"
_calls = [0]
def api(method, path, params=None, body=None):
    params = dict(params or {}); params["api_token"] = TOKEN
    url = f"{BASE}{path}?{urlencode(params)}"
    data = json.dumps(body).encode("utf-8") if body else None
    headers = {"User-Agent":"bh-cadence/1.0"}
    if data: headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    _calls[0] += 1
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")), r.getcode()
    except urllib.error.HTTPError as e:
        try: payload = json.loads(e.read().decode("utf-8"))
        except: payload = {"error": "unknown"}
        return payload, e.code

# Load message templates from the v2 CSV (works locally and in GitHub Actions)
REPO_ROOT = Path(__file__).resolve().parent.parent
DRY = REPO_ROOT / "run_log"
DRY.mkdir(parents=True, exist_ok=True)
TPL_PATH = REPO_ROOT / "data" / "BH_followup_sequence_v2.csv"
if not TPL_PATH.exists():
    TPL_PATH = Path("F:/Work/Joel/bh-enrichment/reports/BH_followup_sequence_v2.csv")
templates = {}  # step -> {touch_name, message}
with open(TPL_PATH, "r", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["Track"] != "Owner":
            continue
        step = int(row["Step"])
        if step in (1, 2, 3, 4):  # Day 0, 1, 2, 3 (consecutive)
            pass  # Original Owner steps 1=Day0, 2=Day1, 3=Day2, 4=Day3 -- but new cadence is Day 0,1,3,7,14
# New 5-touch cadence: Day 0, Day 1, Day 3, Day 7, Day 14 (call-first, text-backup)
# Use Owner track step 1 (Day 0) and step 2 (Day 1) templates, then for Day 3/7/14 use Owner step 4/5/6 (now Week 2/3/etc)
# Actually the new playbook says 5 touches over 2 weeks. The CSV has Owner Day0,1,2,3 + Week 2,3,5,7 + Month 3,4,5
# Repurpose for 5-touch cadence (call first, text if no answer):
NEW_TPLS = {
    1: ("First Contact - Day 0", "Hey, is this the owner of [PROPERTY ADDRESS]?"),
    2: ("First Follow-Up - Day 1", "Hi, this is BH Investments. I know you've probably had a lot of people reaching out asking if you want to sell your property. I'm actually reaching out regarding [PROPERTY ADDRESS]. There may be a potential opportunity involving the property and I wanted to see if it's something you'd be open to discussing. Let me know if you're available to chat."),
    3: ("Second Follow-Up - Day 3", "Just checking in. We're currently working on something involving a property near [PROPERTY ADDRESS], and your property could potentially be part of that opportunity. Happy to explain what we're looking to do and see if it might make sense for you. Feel free to call or text back whenever you have a moment."),
    4: ("Third Follow-Up - Day 7", "Hope you're doing well. Just an update — we recently moved forward on a property near [PROPERTY ADDRESS] and your property still fits what we're putting together. Open to a quick chat? Even 5 minutes would help. – [AGENT NAME]"),
    5: ("Final Follow-Up - Day 14", "Hey, last quick one for now. If anything has changed about [PROPERTY ADDRESS], reach out anytime. – [AGENT NAME], BH Investments"),
}

def touch_step_from_subject(subject):
    """Parse 'Touch 3: ...' to extract step number."""
    import re
    if not subject: return None
    m = re.match(r"Touch\s+(\d+)\b", subject, re.I)
    return int(m.group(1)) if m else None

today = datetime.now().date()
print(f"[{today}] Cadence engine run {'(DRY-RUN)' if DRY_RUN else '(LIVE)'}", file=sys.stderr)

# 1. Pull all deals in Stage 2 (Active Outreach)
print(f"Pulling deals in stage {STAGE_ACTIVE}...", file=sys.stderr)
deals = []
start = 0
while True:
    res, _ = api("GET", "/deals", {"stage_id": STAGE_ACTIVE, "start": start, "limit": 500, "status": "all_not_deleted"})
    batch = res.get("data") or []
    deals.extend(batch)
    more = (res.get("additional_data",{}) or {}).get("pagination",{}) or {}
    if not more.get("more_items_in_collection"): break
    start = more.get("next_start", start+500)
print(f"  {len(deals)} deals in Active Outreach", file=sys.stderr)

if not deals:
    print("No deals in Active Outreach. Engine exits clean.", file=sys.stderr)
    sys.exit(0)

actions_taken = []

for d in deals:
    deal_id = d["id"]
    title = d.get("title","")[:60]

    # Pull activities for this deal
    a_res, _ = api("GET", "/activities", {"deal_id": deal_id, "limit": 100})
    activities = a_res.get("data") or []

    # Find existing 'Touch N' activities
    touch_acts = []
    for a in activities:
        step = touch_step_from_subject(a.get("subject",""))
        if step:
            touch_acts.append({
                "id": a.get("id"),
                "step": step,
                "done": a.get("done", False),
                "due_date": a.get("due_date",""),
                "subject": a.get("subject",""),
            })
    touch_acts.sort(key=lambda x: x["step"])

    # Determine current state
    completed_steps = sorted(set(a["step"] for a in touch_acts if a["done"]))
    open_steps = sorted(set(a["step"] for a in touch_acts if not a["done"]))
    last_completed = completed_steps[-1] if completed_steps else 0
    has_open_touch = len(open_steps) > 0

    # Outreach start date = earliest Touch 1 due_date OR deal's add_time if no touch exists yet
    if touch_acts:
        start_date_str = min(a["due_date"] for a in touch_acts if a["due_date"])
        try:
            start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        except:
            start_date = today
    else:
        # First-ever touch: outreach starts today
        start_date = today

    # Determine next step
    next_step = last_completed + 1

    if next_step > TOTAL_STEPS:
        # Cadence exhausted - check if we should auto-move to Stage 6 Unreachable
        # Wait 1 day after step 5 completion before moving
        day14_done = next(((a for a in touch_acts if a["step"] == TOTAL_STEPS and a["done"]), None), None)
        if day14_done:
            # TODO: check for inbound replies since step 5; for now just move after step 5 done
            action = f"MOVE→Unreachable  cadence done"
            actions_taken.append((deal_id, title, action))
            if not DRY_RUN:
                api("PUT", f"/deals/{deal_id}", body={"stage_id": STAGE_UNREACHABLE})
        continue

    if has_open_touch:
        # Already has open activity for next step (or current); skip
        action = f"SKIP  open Touch {open_steps[0]} pending"
        actions_taken.append((deal_id, title, action))
        continue

    # Compute target due date for next step
    days_delta = DAYS_BY_STEP[next_step]
    target_due = start_date + timedelta(days=days_delta)

    if target_due > today:
        # Not yet time
        action = f"WAIT  Touch {next_step} due {target_due} (in {(target_due - today).days}d)"
        actions_taken.append((deal_id, title, action))
        continue

    # Time to create the next activity
    touch_name, message = NEW_TPLS[next_step]
    subject = f"Touch {next_step}: {touch_name}"

    # CALL_ONLY users: no text fallback in note (e.g., Leo Davis, David James)
    CALL_ONLY_USER_IDS = {25208018, 25208007}  # Leo Davis, David James — call-only, no SMS
    assigned_user_id = d.get("user_id")
    if isinstance(assigned_user_id, dict):
        assigned_user_id = assigned_user_id.get("id")

    if assigned_user_id in CALL_ONLY_USER_IDS:
        note_html = (
            f"<strong>Touch {next_step}/5 — Active Outreach Cadence</strong><br><br>"
            f"<b>CALL ONLY — do not text.</b><br>"
            f"Dial via Aircall. If no answer, leave a brief voicemail and move on.<br>"
            f"Tomorrow's cadence picks up the next touch automatically."
        )
    else:
        msg_personalized = message.replace("[PROPERTY ADDRESS]", title).replace("[AGENT NAME]", "BH Agent")
        note_html = (
            f"<strong>Touch {next_step}/5 — Active Outreach Cadence</strong><br><br>"
            f"Call first via Aircall. If no answer, send this text:<br><br>"
            f"<em>{msg_personalized}</em><br><br>"
            f"<a href='https://docs.google.com/spreadsheets/d/1OizTUtcfJsuVv8h91W9LkwPZxD5NhHwInfVu7WU8BPs/edit?gid=68079243'>Full template sheet</a>"
        )

    body = {
        "subject": subject,
        "type": "call",
        "deal_id": deal_id,
        "due_date": target_due.isoformat(),
        "user_id": assigned_user_id,
        "note": note_html,
    }
    action = f"CREATE Touch {next_step} due {target_due}"
    actions_taken.append((deal_id, title, action))
    if not DRY_RUN:
        api("POST", "/activities", body=body)

# Summary
print(f"\n=== {'DRY-RUN' if DRY_RUN else 'LIVE'} Cadence engine: {len(actions_taken)} deals processed ===", file=sys.stderr)
actions_by_type = {}
for did, t, a in actions_taken:
    key = a.split()[0]
    actions_by_type[key] = actions_by_type.get(key, 0) + 1
for k, v in actions_by_type.items():
    print(f"  {k}: {v}", file=sys.stderr)

print(f"\nAPI calls: {_calls[0]}", file=sys.stderr)
# Detailed log
utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
LOG = DRY / f"cadence_engine_log_{utc}.jsonl"
with open(LOG, "w", encoding="utf-8") as f:
    for did, t, a in actions_taken:
        f.write(json.dumps({"deal_id": did, "title": t, "action": a, "dry_run": DRY_RUN}) + "\n")
print(f"Log: {LOG}", file=sys.stderr)
