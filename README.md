# bh-enrichment

Two unattended GitHub Actions jobs for BH Investments Pipedrive automation.

## Workflows

### 1. `cadence.yml` — Cadence Engine (morning, 11:00 UTC = ~7am ET)
Runs `scripts/cadence_engine.py` for the **Agent Follow-Up pipeline (id 20)**.
For each deal in Stage 2 (Active Outreach):
- Reads existing `Touch N` activities to determine current cadence step
- Computes next touch's due date from outreach start + days delta (0, 1, 3, 7, 14)
- Creates the next Pipedrive activity if due (call task + text fallback message)
- Auto-moves to Stage 6 (Unreachable) after Touch 5 completed

Agents wake up to fresh tasks. Pull templates from `data/BH_followup_sequence_v2.csv`.

### 2. `daily.yml` — Enrichment + EOD (evening, 23:00 UTC = ~7pm ET)

1. `pull_deals.py` - snapshot all non-deleted deals + recent notes to `state/`
2. `detect_neighbor_changes.py` - diff vs previous snapshot, flag new deals
   on a (borough, block) where existing BH deals already live
3. `update_connected_properties.py` - append the new-neighbor's Pipedrive URL
   to the Connected Properties field on each affected deal
4. `refresh_notes_summary.py` - post a fresh AUTO-SUMMARY note on every deal
   that had activity in the last 24h, including caller-extracted last-contact
   agent and Aircall/Calltools recording links from note content
5. `send_email_report.py` - HTML summary email to mikekent389@gmail.com

State (yesterday's deals snapshot, field-key cache) is committed back to the
repo so the next run has yesterday for comparison.

## Setup (one-time, manual)

1. Create an empty private repo on github.com (suggested name: `bh-enrichment`).
2. From this directory:
   ```powershell
   cd F:\Work\Joel\bh-enrichment\github_repo
   git init
   git add .
   git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<your-username>/bh-enrichment.git
   git push -u origin main
   ```
3. In the repo on github.com: Settings -> Secrets and variables -> Actions ->
   New repository secret. Add **two** secrets:
   - `PIPEDRIVE_TOKEN` - the BH Pipedrive API token
   - `GMAIL_APP_PASSWORD` - generate via Gmail account -> Security ->
     2-Step Verification -> App passwords -> "Mail / Other" -> 16-char
     password (paste with spaces removed)
4. Settings -> Actions -> General -> Workflow permissions -> select
   "Read and write permissions" so the workflow can commit the state
   snapshot (and check the box for "Allow GitHub Actions to create and
   approve pull requests" if needed).
5. Test it: Actions tab -> "BH Daily Enrichment" -> "Run workflow"
   (manual trigger). Watch the logs. Check the email arrives.
6. After the first manual run succeeds, the cron schedule takes over -
   23:00 UTC every day.

## Schedule note

`'0 23 * * *'` is 7pm ET during **EDT** (mid-Mar - early-Nov) and 6pm ET
during **EST** (early-Nov - mid-Mar). If 7pm is critical year-round, use
two cron lines or change to `'0 0 * * *'` (midnight UTC = 7pm EST in winter,
8pm EDT in summer).

## Local dev

```powershell
$env:PIPEDRIVE_TOKEN = "..."
$env:GMAIL_APP_PASSWORD = "..."
python -m pip install -r requirements.txt
python scripts/pull_deals.py
python scripts/detect_neighbor_changes.py
python scripts/update_connected_properties.py
python scripts/refresh_notes_summary.py
python scripts/send_email_report.py
```

## Tests

```powershell
python tests/test_caller_extractor.py
```

(no pytest needed; tests run inline)
