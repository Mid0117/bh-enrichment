"""Multi-property-owner sync — runs daily at 8 PM ET (00:00 UTC).

For every phone number on a Person record that's linked to 2+ deals
(i.e., the same owner shows up across multiple properties):

  1. Upsert a pinned 'ALSO OWNS' note on each of those deals showing the
     OTHER properties this contact owns, plus the latest Aircall recording
     for that phone.
  2. If the same phone is split across an LLC-named Person and a
     human-named Person, relink the LLC deals to the human Person and move
     the LLC name into the deal's 'Owner LL' custom field. No persons or
     deals are deleted.

Idempotent — the note is found by a distinctive marker and updated in
place, mirroring the AUTO-SUMMARY v2 pattern.

Credentials from env vars (GitHub secrets) with local .env fallback.
"""
import json, os, re, sys, time, base64, urllib.request, urllib.error, io
from urllib.parse import urlencode

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def env(k):
    v = os.environ.get(k)
    if v: return v
    for p in ("F:/Work/Joel/bh-enrichment/.env",
              os.path.join(os.path.dirname(__file__), "..", "..", ".env")):
        if os.path.exists(p):
            for line in open(p):
                if "=" in line:
                    a, b = line.strip().split("=", 1)
                    if a == k: return b
    raise KeyError(k)


TOKEN = env("PIPEDRIVE_TOKEN")
AC_AUTH = base64.b64encode(f'{env("AIRCALL_API_ID")}:{env("AIRCALL_API_TOKEN")}'.encode()).decode()

DRY = "--dry-run" in sys.argv

OWNER_LL_KEY = "3ae0f8127bc897fdc4e4243332e99ad7271145cf"
NOTE_MARKER = "🏢 ALSO OWNS"

LLC_PATTERNS = re.compile(
    r"\b(LLC|L\.L\.C\.?|INC\.?|INCORPORATED|CORP\.?|CORPORATION|LTD\.?|L\.P\.?|LP|"
    r"TRUST|HOLDINGS|PROPERTIES|REALTY|MANAGEMENT|ASSOCIATES|GROUP|PARTNERS|"
    r"ENTERPRISES|VENTURES|EQUITIES|CAPITAL|CHURCH|MINISTR|CONGREGATION|"
    r"PARISH|DIOCESE|TEMPLE|SYNAGOGUE|MOSQUE|SOCIETY|FOUNDATION|FUND)\b",
    re.I,
)


PLACEHOLDER_RE = re.compile(r"^\+?\d|aircall.*(new|unknown|person|caller)", re.I)


def is_llc(name):
    """True if a person's name looks like an entity, not a real human."""
    n = (name or "").strip()
    if not n: return False
    return bool(LLC_PATTERNS.search(n))


def is_placeholder(name):
    """Aircall auto-creates persons named like '+1347xxxxxxx Aircall new person'
    when a call comes in from a number not yet in the CRM. These are NOT
    valid canonical humans for the LLC-merge logic."""
    n = (name or "").strip()
    if not n: return True
    return bool(PLACEHOLDER_RE.match(n))


def norm_phone(s):
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else None


def pd(method, path, body=None, retries=5):
    url = f"https://api.pipedrive.com/v1{path}{'&' if '?' in path else '?'}api_token={TOKEN}"
    data = json.dumps(body).encode() if body else None
    h = {"User-Agent": "bh/multi-owner/1.0"}
    if data: h["Content-Type"] = "application/json"
    for a in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h, method=method)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()), r.getcode()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and a < retries - 1:
                time.sleep(2 ** a); continue
            try: return json.loads(e.read().decode()), e.code
            except: return {"error": str(e)}, e.code
        except Exception as e:
            if a < retries - 1: time.sleep(2 ** a); continue
            return {"error": str(e)}, 0


def ac_search(phone):
    qs = urlencode({"phone_number": f"+1{phone}", "per_page": 50})
    req = urllib.request.Request(f"https://api.aircall.io/v1/calls/search?{qs}",
                                 headers={"Authorization": f"Basic {AC_AUTH}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode()).get("calls") or []
    except Exception:
        return []


def paginate(path):
    out = []; start = 0
    while True:
        j, _ = pd("GET", f"{path}{'&' if '?' in path else '?'}start={start}&limit=500")
        batch = j.get("data") or []
        out.extend(batch)
        more = (j.get("additional_data", {}) or {}).get("pagination", {}) or {}
        if not more.get("more_items_in_collection"): break
        start = more.get("next_start", start + 500)
    return out


# 1. PERSONS
print("[1/5] Pulling all persons...", file=sys.stderr)
persons = paginate("/persons?")
print(f"  {len(persons)} persons", file=sys.stderr)

# phone10 -> [(person_id, name, is_llc, update_time)]
phone_index = {}
for p in persons:
    pid = p.get("id"); name = (p.get("name") or "").strip()
    if not pid: continue
    for ph in p.get("phone") or []:
        v = ph.get("value") if isinstance(ph, dict) else ph
        n = norm_phone(v)
        if not n: continue
        phone_index.setdefault(n, []).append({
            "person_id": pid, "name": name, "is_llc": is_llc(name),
            "update_time": p.get("update_time") or "",
        })

# 2. DEALS
print("[2/5] Pulling all open deals...", file=sys.stderr)
deals_raw = paginate("/deals?status=open")
# index by person_id
deals_by_person = {}
deal_by_id = {}
for d in deals_raw:
    deal_by_id[d["id"]] = d
    pid = d.get("person_id")
    if isinstance(pid, dict): pid = pid.get("value")
    if pid:
        deals_by_person.setdefault(pid, []).append(d)
print(f"  {len(deals_raw)} open deals, {len(deals_by_person)} distinct linked persons", file=sys.stderr)

# 3. ALL NOTES (for finding existing ALSO OWNS notes)
print("[3/5] Pulling all notes (for upsert lookup)...", file=sys.stderr)
all_notes = paginate("/notes?")
deal_to_also_owns_note = {}
for n in all_notes:
    if NOTE_MARKER in (n.get("content") or ""):
        deal_to_also_owns_note[n.get("deal_id")] = n["id"]
print(f"  {len(all_notes)} notes total, {len(deal_to_also_owns_note)} existing ALSO OWNS notes", file=sys.stderr)


# 4. ANALYZE — find multi-property phones + LLC merge candidates
print("[4/5] Analyzing phone groups...", file=sys.stderr)

multi_owner_groups = []  # list of dicts: {phone, persons, deals, llc_to_human_pairs}
for phone, persons_for_phone in phone_index.items():
    # Collect all deals reachable via any person on this phone
    deal_ids = []
    for p in persons_for_phone:
        for d in deals_by_person.get(p["person_id"], []):
            deal_ids.append((d["id"], d, p))
    # Dedupe deals (one deal per id, keep first occurrence)
    seen = set(); deals_uniq = []
    for did, d, p in deal_ids:
        if did in seen: continue
        seen.add(did); deals_uniq.append({"deal_id": did, "deal": d, "person": p})
    if len(deals_uniq) < 2:
        continue
    # LLC -> human merge candidate: any LLC-typed person and any human-typed person share this phone
    # Exclude Aircall placeholders (auto-created persons named like '+13479228656 Aircall new person')
    humans = [p for p in persons_for_phone
              if not p["is_llc"] and p["name"] and not is_placeholder(p["name"])]
    llcs = [p for p in persons_for_phone if p["is_llc"]]
    canonical_human = None
    if humans and llcs:
        # Pick most recently updated human as canonical
        canonical_human = sorted(humans, key=lambda x: x["update_time"], reverse=True)[0]
    multi_owner_groups.append({
        "phone": phone,
        "persons": persons_for_phone,
        "deals": deals_uniq,
        "canonical_human": canonical_human,
        "llc_persons": llcs,
    })
print(f"  multi-property phone groups: {len(multi_owner_groups)}", file=sys.stderr)
print(f"  groups with LLC->human merge opportunity: {sum(1 for g in multi_owner_groups if g['canonical_human'])}", file=sys.stderr)
print(f"  total deals that will get an ALSO OWNS note: {sum(len(g['deals']) for g in multi_owner_groups)}", file=sys.stderr)

if DRY:
    # Sample 5 groups to show what would happen
    print("\nSample of multi-property groups (DRY-RUN):", file=sys.stderr)
    for grp in multi_owner_groups[:5]:
        print(f"\n  phone +1 {grp['phone'][:3]}-{grp['phone'][3:6]}-{grp['phone'][6:]}  ({len(grp['deals'])} deals)", file=sys.stderr)
        for p in grp["persons"]:
            tag = " [LLC]" if p["is_llc"] else " [human]"
            print(f"     person #{p['person_id']:>6} {p['name']!r}{tag}", file=sys.stderr)
        if grp["canonical_human"]:
            llc_count = sum(1 for e in grp["deals"] if e["person"]["is_llc"])
            print(f"     -> WOULD MERGE: {llc_count} LLC-linked deals relink to person #{grp['canonical_human']['person_id']} ({grp['canonical_human']['name']!r})", file=sys.stderr)
        for e in grp["deals"][:8]:
            print(f"     deal #{e['deal_id']} {e['deal'].get('title','')[:55]}", file=sys.stderr)
    print(f"\nDRY-RUN — no writes performed. Run without --dry-run to apply.", file=sys.stderr)
    sys.exit(0)


# 5. APPLY — LLC merges first, then ALSO OWNS notes
print("[5/5] Applying merges + notes...", file=sys.stderr)
merges_done = 0; notes_done = 0; errors = []
ts = time.strftime("%Y%m%dT%H%M%S")
# Log lives in run_log/ inside the repo so it works on both the local Windows
# box and the GHA Linux runner. (Was hard-coded F:/Work/... — that broke GHA.)
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run_log")
os.makedirs(LOG_DIR, exist_ok=True)
log = open(os.path.join(LOG_DIR, f"multi_owner_log_{ts}.jsonl"), "w", encoding="utf-8")

for grp in multi_owner_groups:
    phone = grp["phone"]
    deals = grp["deals"]
    canonical_human = grp["canonical_human"]

    # 5a. LLC -> human merge: any deal whose person is LLC-typed gets relinked
    if canonical_human:
        for entry in deals:
            person = entry["person"]
            if not person["is_llc"]: continue
            did = entry["deal_id"]
            d = entry["deal"]
            cur_ll = d.get(OWNER_LL_KEY) or person["name"]
            body = {"person_id": canonical_human["person_id"], OWNER_LL_KEY: cur_ll}
            r, c = pd("PUT", f"/deals/{did}", body=body)
            if c in (200, 201):
                merges_done += 1
                log.write(json.dumps({"action": "merge", "deal_id": did, "phone": phone,
                                       "from_person": person["person_id"], "from_name": person["name"],
                                       "to_person": canonical_human["person_id"],
                                       "to_name": canonical_human["name"],
                                       "owner_ll_set": cur_ll}) + "\n")
            else:
                errors.append({"deal_id": did, "action": "merge", "code": c})
            time.sleep(0.1)

    # 5b. Latest call for this phone (Aircall search)
    calls = ac_search(phone)
    latest = sorted(calls, key=lambda c: c.get("started_at") or 0, reverse=True)[:1]
    latest_line = ""
    if latest:
        c = latest[0]
        dt = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(c.get("started_at") or 0))
        agent = (c.get("user") or {}).get("name", "?")
        dur = c.get("duration") or 0
        rec = c.get("recording") or ""
        rec_link = f' · <a href="{rec}">recording</a>' if rec else ""
        latest_line = f"<br><b>📞 Latest call:</b> {dt} · {agent} · {dur}s{rec_link}"

    # 5c. Per deal: build "Also Owns" note listing the OTHER deals in this group
    for entry in deals:
        did = entry["deal_id"]
        others = [e for e in deals if e["deal_id"] != did]
        lines = [f"<strong>{NOTE_MARKER}</strong> (multi-property owner)",
                 f"<em>Same phone +1 {phone[:3]}-{phone[3:6]}-{phone[6:]} appears on {len(deals)} deals</em>",
                 "<br><b>Also owns:</b>"]
        for o in sorted(others, key=lambda x: x["deal"].get("title", "")):
            title = (o["deal"].get("title") or f"#{o['deal_id']}")[:80]
            lines.append(f'• <a href="https://bhinvestments.pipedrive.com/deal/{o["deal_id"]}">{title}</a>')
        content = "<br>".join(lines) + latest_line

        existing_nid = deal_to_also_owns_note.get(did)
        if existing_nid:
            r, c = pd("PUT", f"/notes/{existing_nid}",
                      body={"content": content, "pinned_to_deal_flag": 1})
            if c in (200, 201):
                notes_done += 1
                log.write(json.dumps({"action": "update_note", "deal_id": did, "note_id": existing_nid,
                                       "phone": phone, "others_count": len(others)}) + "\n")
            else:
                errors.append({"deal_id": did, "action": "update_note", "code": c})
        else:
            r, c = pd("POST", "/notes",
                      body={"deal_id": did, "content": content, "pinned_to_deal_flag": 1})
            if c in (200, 201):
                notes_done += 1
                nid = r.get("data", {}).get("id") if isinstance(r, dict) else None
                log.write(json.dumps({"action": "create_note", "deal_id": did, "note_id": nid,
                                       "phone": phone, "others_count": len(others)}) + "\n")
            else:
                errors.append({"deal_id": did, "action": "create_note", "code": c})
        time.sleep(0.1)

log.close()
print(f"\nDONE  merges={merges_done}  notes={notes_done}  errors={len(errors)}", file=sys.stderr)
if errors:
    print(f"  sample errors: {errors[:5]}", file=sys.stderr)
