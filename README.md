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
- FMP Delisted Companies is also disabled by default in free-tier mode.
  `FMP_DELISTED_PREMIUM_ENABLED=false` prevents repeated paid-endpoint probes,
  preserves prior positive events, and leaves historical delisting no-event
  coverage unverified.
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

`prospective-corporate-action-surveillance-v1` is an additive free-source
contract stored under the existing mapping refresh audit. It records an
activation hash and one deterministic observation per completed market
session. Complete Nasdaq Trader listing snapshots provide prospective exact
symbol continuity for symbol-change/delisting no-event checks; the official
current halt feed provides prospective suspension evidence. A removed symbol,
source gap, partial response, stale response, or active suspension remains
unverified for that decision-to-horizon window. This contract never upgrades
historical five-year evidence and never turns a missing source into a verified
no-event result.

Runtime audit artifacts:

- `state/ticker-mapping-refresh-audit.json`
- `state/corporate-action-lineage-runtime-audit.json`
- `System_Identity_Maps/CORPORATE_ACTION_LINEAGE_RUNTIME_AUDIT.json`

## Toss read-only capability probe

The scheduled Harvester reserves `System_Identity_Maps/TOSS_READ_ONLY_CAPABILITY.json`
before making at most one OAuth request and one adjusted daily-candle request.
Later runs reuse that result with zero Toss requests. The probe never sends
`X-Tossinvest-Account`, never calls account/order endpoints, redacts the selected
symbol to a SHA-256, and does not replace the canonical Google Drive OHLCV source.

OAuth failures preserve only the HTTP status category and a bounded machine-readable
error code. An HTTP 403 is not treated as proof of an IP allow-list failure because
the official contract also uses 403 for permission failures. Existing sticky results
remain historical evidence and require the separately approved archive/reprobe
procedure in `TOSS_PHASE1_AUTH_REPROBE_PACKAGE.md` before another network request.

## Toss Phase2a SHADOW_ONLY market data

Phase2a reads `/api/v1/prices` and `/api/v1/market-calendar/US` only from a
server-side Mac runtime whose outbound IP is registered with Toss. The GitHub
hosted workflow locks `TOSS_SHADOW_PROVIDER_ENABLED=false`, so its Toss Phase2a
request count is always zero. A local run additionally requires
`TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED=true`.

The bounded run budget is one OAuth request, one US calendar request, and at
most two `/prices` requests with up to 200 dynamically selected symbols per
request. Current documented limits are 15 TPS for `MARKET_DATA` and 3 TPS for
`MARKET_INFO`; runtime `X-RateLimit-*` and `Retry-After` headers remain the
source of truth. No account header or account/order endpoint is allowed.

`state/toss-market-data-shadow.json` and
`System_Identity_Maps/TOSS_MARKET_DATA_SHADOW.json` are report-only evidence.
They never replace Google Drive/yfinance canonical data and never affect Stage6
policy. Any auth, rate-limit, transient, schema, stale/partial, or source-conflict
failure opens a run-level circuit breaker, excludes all Toss evidence for that
run, sends at most one aggregate alert through the existing alert route, and
lets canonical collection continue. Alert failures are recorded safely and do
not trigger recursive alerts.

The official `PriceResponse.timestamp` field is nullable. Phase2a therefore
does not combine every unusable row into a generic stale/future bucket: safe
aggregate diagnostics distinguish missing timestamps, timestamps after local
response receipt, dates outside the verified market-calendar window, and
symbols omitted from the response. Any of those conditions still excludes the
entire Toss contribution for the run; the diagnostics do not add clock-skew
tolerance or weaken evidence eligibility.

Timestamp-shape diagnostics also separate an absent key, documented `null`,
blank text, an unparseable value, and a valid offset-aware value. They persist
only counts, per-batch reconciliation, safe JSON type counts, and SHA-256 hashes
of sorted response key sets. They never persist timestamp values, symbols, or
raw responses. The strict all-row timestamp gate remains unchanged pending the
report-only policy decision in `TOSS_PRICE_TIMESTAMP_POLICY_REVIEW.md`.

The additive `clockDomainEvidence` envelope records request start, local response
receipt, parsed HTTP `Date`, request duration, and aggregate payload timestamp
offsets for each response. HTTP Date is diagnostic-only: it is not rewritten as
`sourceAsOf`, does not adjust the local clock, and does not make a failed row
eligible. No timestamp tolerance is authorized; a payload after the strict local
receipt remains excluded until a separately reviewed provider clock contract is
proven. Raw headers, symbols, IP addresses, credentials, and response bodies are
not included in public clock diagnostics.

This is an additive, non-breaking extension of `toss-market-data-shadow-v1`.
Existing consumers may ignore `clockDomainEvidence`; eligibility and canonical
source semantics are unchanged. The root-cause counts distinguish local receipt
behind a valid server reference, payload ahead of both references, missing or
invalid HTTP `Date`, nullable payload timestamps, and partial symbol responses.

The additive `requestLineage` envelope binds each Toss request to the selected
Stage3 file, canonical artifact hash, source time, deterministic request-scope
hash, and per-batch requested/returned scope hashes. Missing provider rows are
stored only as individual SHA-256 values; raw symbols remain absent from public
artifacts. A partial response still excludes the entire Toss contribution. When
Stage3 has no embedded generation timestamp, `generatedAtSource` explicitly
identifies the Google Drive creation time rather than presenting it as an
artifact field. The existing top-level `requestScopeSha256` keeps its legacy
symbols-plus-calendar semantics; `requestLineage.requestScopeSha256` is the
symbols-only scope hash used to reconcile Stage3 and provider batches.

Toss symbol adaptation is provider-only and preserves the canonical Stage3
universe. A US class-share symbol ending in a single hyphenated class letter is
requested from Toss with the equivalent dot separator, then mapped back to the
canonical symbol before the SHADOW artifact is built. The request lineage keeps
separate canonical and provider scope hashes, stores provider symbols only as
SHA-256 evidence, and blocks before network access if two canonical symbols
would collide after adaptation. This additive mapping does not authorize Toss
eligibility, canonical-source replacement, or any Stage6 policy impact.

Before another registered-Mac one-shot, operators should confirm the macOS
`timed` service, timezone, and network-time configuration without changing the
clock. A running daemon alone does not prove offset accuracy. If the exact
network-time state or offset cannot be verified without privileged or external
checks, report `MAC_CLOCK_SYNC_UNVERIFIED`; do not infer synchronization and do
not compensate timestamps in code.

### Same-Stage3 Mac handoff

The optional Mac collector consumes the exact Stage3 file/hash/scope published
in `COLLECTION_PROGRESS.json`; it does not independently choose a newer Stage3
after locking that handoff. The dispatch path also records the same canonical
JSON hash and request-scope hash in `LATEST_STAGE4_READY.json`. A missing or
mismatched exact file is blocked before any Toss request and never falls back to
another Stage3 artifact.

`python harvester.py --toss-shadow-collector` is the prepared one-shot entrypoint.
It remains disabled unless both existing Mac-only provider gates are enabled.
Collection is allowed only while the exact Stage3 progress handshake is still
`PROCESSING`; a completed Stage4-ready window causes zero Toss requests.
For each verified Stage3 source it derives one idempotency key, atomically
reserves a private local sentinel, reuses an already matched successful shadow
with zero Toss requests, and preserves failed/in-progress sentinels instead of
automatically retrying. Drive publication archives the previous and current
shadow before replacing `TOSS_MARKET_DATA_SHADOW.json`; publication failure
leaves canonical Google Drive/yfinance analysis fail-open and excludes Toss.

No recurring `launchd` job is installed by this repository change. Activation
requires a separate approval, server-side secret loading, the registered Mac
egress, and an explicit rollback that unloads the job without deleting sentinel
or Drive archive evidence. GitHub-hosted runners keep both Toss probes disabled
and make zero Toss requests.
