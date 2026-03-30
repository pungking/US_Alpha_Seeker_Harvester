# US_Alpha_Seeker_Harvester

## Symbol lifecycle state management

The harvester now persists per-symbol lifecycle state to:

- `System_Identity_Maps/HARVESTER_SYMBOL_STATE.json`

This is used to track symbols that are onboarding, partially covered, recovered, stale, retired, or excluded.

### Optional environment variables

- `HARVESTER_HISTORY_FULL_MIN_PERIODS` (default: `8`)
  - Minimum history periods required to classify a symbol as `FULL`.
- `HARVESTER_STALE_HISTORY_STREAK` (default: `3`)
  - Consecutive runs with missing history before state becomes `STALE`.
- `HARVESTER_STALE_QUOTE_STREAK` (default: `3`)
  - Consecutive runs with missing quote payload before state becomes `STALE`.
- `HARVESTER_RETIRE_DAYS` (default: `45`)
  - If a symbol is not seen for this many days, state is moved to `RETIRED`.

## Run summary artifact

The harvester now writes a run summary JSON file for downstream automation:

- `state/last-harvester-run.json`

Path override (optional):

- `HARVESTER_RUN_SUMMARY_PATH` (default: `state/last-harvester-run.json`)

## Optional Notion sync (GitHub Actions)

Workflow `main.yml` can upsert each run summary into Notion `Daily Snapshot` DB.

- `NOTION_TOKEN` (GitHub secret)
- `NOTION_DB_DAILY_SNAPSHOT` (GitHub variable)
- `NOTION_HARVESTER_SYNC_ENABLED` (variable, default `true`)
- `NOTION_HARVESTER_SYNC_REQUIRED` (variable, default `false`)

When `NOTION_HARVESTER_SYNC_REQUIRED=false`, Notion sync is warning-only.
