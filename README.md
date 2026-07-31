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

External evidence is additive to `corporate-action-lineage-v1`; existing field
names are unchanged. A verified external event/no-event record now also
requires a successful request, exact requested/matched symbol, explicit
coverage interval, non-partial response, retrieval timestamp, and response
SHA-256. Empty, timed-out, stale, partial, or entitlement-blocked responses
remain unverified.

Current source chain:

- Finnhub Symbol Change Premium is disabled by default because the current
  account is free-tier. `FINNHUB_SYMBOL_CHANGE_PREMIUM_ENABLED=false` makes no
  Premium request, preserves prior positive evidence, and keeps no-event
  coverage unverified. A reviewed paid entitlement must explicitly enable the
  source; successful responses must still pass the existing window, row-limit,
  schema, and hash checks.
- FMP Delisted Companies for delisting events and coverage.
- Nasdaq Trader official current-halt RSS plus the documented one-year halt
  search for H4/H9/H10/H11/M1/T6/T12 regulatory, listing, corporate-action, or
  extended halt evidence. The current feed catches unresolved halts older than
  the searchable history window; the artifact records the one-year historical
  coverage limit instead of claiming the configured five-year window. RSS
  publication time is validated independently from retrieval time; missing,
  future, or older-than-policy feed timestamps make the suspension source
  unverified. The default freshness ceiling is 15 minutes during the weekday
  regular-session window and 120 hours outside that window to tolerate
  weekends and exchange holidays without treating an intraday-stale feed as
  current. Positive halt events outside the one-year query window are
  retained with explicit preservation provenance until the configured global
  retention cutoff, but current suspension status is determined by the fresh
  complete current feed rather than by a preserved historical row.
If the Finnhub symbol-change source is disabled, unavailable, or not entitled,
the audit reports `BLOCKED_EXTERNAL_SOURCE_CONTRACT` and does not manufacture
`VERIFIED_NO_SYMBOL_CHANGE_AS_OF_SOURCE`. The existing
`TICKER_MAPPING_REFRESH_AUDIT.json` carries source summaries and compact event
rows, so removed/delisted symbols are not lost when the active ticker mapping
is replaced.

Batch-source no-event evidence records the evaluated symbol separately from
the response match. `matchedSymbol` is empty for a proven exact absence and is
populated only when an event row actually matches; `symbolMatchStatus` and the
complete response hash make that distinction auditable.

Compatibility/migration note: this is an additive extension of
`corporate-action-lineage-v1`. Existing status fields retain their semantics;
older rows without the request-proof envelope remain valid historical
artifacts but are not eligible for OOS comparison. Consumers must not backfill
the new proof fields or upgrade legacy rows by inference.

Runtime audit artifacts:

- `state/ticker-mapping-refresh-audit.json`
- `state/corporate-action-lineage-runtime-audit.json`
- `System_Identity_Maps/CORPORATE_ACTION_LINEAGE_RUNTIME_AUDIT.json`
