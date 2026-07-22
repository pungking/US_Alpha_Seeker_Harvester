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
- `HARVESTER_SKIP_RETIRED_SYMBOLS` (default: `true`)
  - Skip data collection for symbols already classified as `RETIRED`.
- `HARVESTER_SKIP_EXCLUDED_SYMBOLS` (default: `true`)
  - Skip data collection for symbols already classified as `EXCLUDED` because the instrument type is not analysis-eligible.

## Mapping freshness audit

The harvester owns the raw collection universe. Before daily collection it refreshes
`System_Identity_Maps/Ticker_ID_Mapping_Final.json` from authoritative active
listing directories, then uses lifecycle state as a second safety layer.

To keep collection fresh without hiding bad symbols, daily runs now:

- add newly listed symbols from authoritative listing sources,
- remove symbols absent from authoritative active listing sources from the mapping,
- prune symbols absent from the refreshed mapping out of `Financial_Data_Daily` group files so Stage0 cannot keep loading stale records,
- default the collection mapping to common-stock eligible listings; set `HARVESTER_TICKER_MAPPING_INCLUDE_NON_COMMON=true` only if monitoring ETFs/units/rights/warrants is intentionally required,
- classify stale/retired/excluded symbols from `HARVESTER_SYMBOL_STATE.json`,
- skip only confirmed `RETIRED` and `EXCLUDED` symbols,
- let the refreshed authoritative mapping override stale `EXCLUDED`/`RETIRED` state so falsely excluded active common stocks can recover,
- keep stale common-stock symbols visible until the retire policy or an upstream mapping refresh resolves them,
- write an audit artifact with mapping-review and skip candidates.

Artifacts:

- `state/harvester-mapping-freshness-audit.json`
- `state/harvester-mapping-freshness-audit.md`
- `state/ticker-mapping-refresh-audit.json`

Drive mirror:

- `System_Identity_Maps/HARVESTER_MAPPING_FRESHNESS_AUDIT.json`
- `System_Identity_Maps/TICKER_MAPPING_REFRESH_AUDIT.json`
- `System_Identity_Maps/Ticker_ID_Mapping_Final.json`

Authoritative listing sources:

- `https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt`
- `https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt`

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

## OHLCV corporate-action lineage

Stage3 dispatch OHLCV files use the backward-compatible `ohlcv-lineage-v1`
envelope:

```json
{
  "schemaVersion": "ohlcv-lineage-v1",
  "data": [],
  "lineage": {}
}
```

Legacy array-only files remain readable. The first dispatch that encounters a
legacy file performs a full-history refresh before adding lineage; it does not
claim corporate-action coverage from the old array alone.
An incremental response that contains a split/dividend event, changes an
overlapping adjusted OHLC value, or no longer overlaps the stored history also
forces a full-history refresh so two adjustment bases are never merged.

The lineage records yfinance/Yahoo retrieval time, source-as-of session,
`auto_adjust=true`, split/dividend action columns, event effective dates,
return basis, listing evidence, and source request lineage. Symbol-change,
delisting, or suspension status remains `UNVERIFIED_*` unless a source record
contains an explicit verified status, source, and source-as-of timestamp.
A successfully returned yfinance action column can prove no split/dividend in
the requested window; a missing or failed external event response is never
converted into verified symbol-change, delisting, or suspension no-event
evidence.

The active listing directory does not by itself prove that no historical
symbol change, delisting, or suspension occurred. Those statuses therefore
remain `UNVERIFIED_*` until an explicit source-backed evidence record is
available; such rows are intentionally ineligible for OOS cohort comparison.

Runtime audit artifacts:

- `state/corporate-action-lineage-runtime-audit.json`
- `System_Identity_Maps/CORPORATE_ACTION_LINEAGE_RUNTIME_AUDIT.json`
