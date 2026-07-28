# Corporate-Action Source Procurement

Generated: 2026-07-28

## Decision

The current Harvester cannot truthfully certify five-year symbol-change,
delisting, suspension, and resumption no-event coverage.

| Event | Decision | Primary source | Fallback | Five-year no-event proof |
|---|---|---|---|---|
| Symbol change | `EXISTING_SOURCE_ENTITLEMENT_REQUIRED` | Finnhub Symbol Change Premium | FMP Symbol Changes, then exchange-specific products | Blocked until a premium key is wired to this repository and a natural run passes |
| Delisting | `EXISTING_SOURCE_ENTITLEMENT_REQUIRED` | FMP Delisted Companies | Nasdaq Daily List and NYSE Group Corporate Actions for their own listings | Blocked by the current FMP credential returning HTTP 402 |
| Suspension | `PAID_SOURCE_APPROVAL_REQUIRED` | NYSE Daily TAQ admin messages for consolidated U.S. coverage | Nasdaq halt history/RSS and SEC suspensions as positive-event supplements | Public sources do not publish a complete five-year no-event contract |
| Resumption | `PAID_SOURCE_APPROVAL_REQUIRED` | NYSE Daily TAQ admin messages for consolidated U.S. coverage | Nasdaq halt history/RSS as a positive-event supplement | Same blocker as suspension |

No source may be upgraded from `UNVERIFIED_*` based on this document alone.
Production verification still requires a successful source response with the
existing request, coverage, freshness, completeness, and response-hash
contract.

## Current Credential And Runtime Inventory

Only secret names and workflow wiring were inspected. Secret values were not
read or printed.

| Evidence | Result |
|---|---|
| Harvester `FMP_KEY` secret | Present and wired server-side |
| Harvester `FINNHUB_KEY` secret | Missing; workflow wiring already exists |
| `US_Alpha_Seeker` `FINNHUB_KEY` secret | Present, but repository-scoped and not readable or transferable by this audit |
| Nasdaq/NYSE paid product credential | Not present |
| FMP delisting runtime | `BLOCKED_EXTERNAL_SOURCE_CONTRACT`, `entitlement_or_auth_http_402` |
| Nasdaq halt runtime | `SUCCESS`, but historical coverage is one year versus the requested five years |
| Finnhub post-`b2232648` runtime | `pending_natural_runtime_proof` |
| Lineage structural coverage | 300/300, missing 0, duplicate 0 |
| Comparison-ready lineage | 0/300 |

The fixed baseline is natural Harvester run `30355306266`. It predates
`b2232648`, so it is valid evidence for the FMP and Nasdaq blockers but not a
runtime proof of the merged Finnhub producer.

## Official Feasibility Matrix

### Finnhub Symbol Change Premium

- Product/endpoint: `GET /ca/symbol-change`.
- Official contract: premium access, date-bounded requests, maximum 2,000
  events per response, and `fromDate`/`toDate` response echoes.
- Market scope: U.S.-listed and other documented markets.
- Five-year implementation: already implemented as deterministic one-year
  request segments with schema, date-echo, limit, and response-hash checks.
- Authentication: server-side API token.
- Public pricing: Finnhub lists Free at USD 0/month and All-In-One at USD
  3,500/month billed annually, but the public table does not unambiguously map
  Symbol Change Premium to a purchasable tier. Entitlement must be confirmed
  against the existing key or by Finnhub sales.
- Repository state: code ready; Harvester secret absent.
- Decision: `EXISTING_SOURCE_ENTITLEMENT_REQUIRED`.
- Next action: copy the already-owned key into the Harvester repository secret
  `FINNHUB_KEY` without exposing its value, then let one natural run prove
  entitlement and response completeness.

Official references:

- https://api.finnhub.io/docs/api/rate-limit
- https://finnhub.io/pricing

### FMP Symbol Changes And Delisted Companies

- Products/endpoints:
  - `GET /stable/symbol-change`
  - `GET /stable/delisted-companies?page=...&limit=100`
- Documented scope: symbol changes and companies delisted from U.S. exchanges.
- Pagination: the delisted producer already pages until a short/empty page and
  refuses page-cap saturation as complete coverage.
- Authentication: server-side API key.
- Runtime evidence: the current Harvester key receives HTTP 402 from Delisted
  Companies, so it cannot prove five-year delisting coverage.
- Pricing/licensing: endpoint-level plan access must be checked against the
  account or confirmed by FMP. FMP states that display or redistribution needs
  a separate licensing agreement; its commercial Enterprise price is
  quote-based.
- Decision: `EXISTING_SOURCE_ENTITLEMENT_REQUIRED`.
- Next action: obtain an endpoint-level entitlement response from FMP for both
  stable endpoints before changing code or treating empty results as no-event.

Official references:

- https://site.financialmodelingprep.com/developer/docs/stable/symbol-changes-list
- https://site.financialmodelingprep.com/developer/docs/delisted-companies-api
- https://site.financialmodelingprep.com/developer/docs/pricing?planType=commercial

### Nasdaq Daily List

- Product: Nasdaq Daily List, secured website or SFTP.
- Scope: Nasdaq-listed new listings, delistings, name/symbol changes, and other
  listed corporate actions.
- History: official product description states history from 1999.
- 2026 fee: USD 3,500 per organization per month.
- Agreements: Data Feed Request Form and Nasdaq Global Data Agreement; a System
  Application may also be required. CUSIP data requires separate CUSIP
  licensing.
- Limitation: Nasdaq listing scope is not a complete all-U.S.-exchange
  contract.
- Decision: `PAID_SOURCE_APPROVAL_REQUIRED` as an exchange-specific fallback,
  not as the sole source.

Official references:

- https://classic.nasdaqtrader.com/Trader.aspx?id=DailyListPD
- https://www.nasdaqtrader.com/content/ProductsServices/PriceList/Nasdaq_US_Equities_Price_List_2025_2026_2027.pdf
- https://nasdaqtrader.com/content/technicalSupport/specifications/dataproducts/dlcompletespec.pdf

### Nasdaq Halt History And RSS

- Public contracts:
  - current and date-specific historical RSS,
  - halt/resumption dates and times,
  - market and reason codes,
  - interactive search limited to a rolling year.
- Existing runtime: current feed plus one-year search succeeds and preserves
  positive events without claiming the requested five years.
- Feasibility probes: date-specific RSS returned valid HTTP 200 responses for
  sampled dates in 2021, 2022, 2023, 2024, and 2026.
- Limitation: official documentation does not provide a five-year
  completeness SLA, bulk pagination contract, or automation rate limit. A
  sampled old response is not proof that every date in the five-year interval
  is complete.
- Licensing: use is subject to the Nasdaq Trade Halt RSS terms; raw response
  redistribution is not approved by this audit.
- Decision: `OFFICIAL_PARTIAL_SOURCE_ONLY`.
- Next action: continue using it for verified positive events and the
  documented one-year no-event interval. Do not launch a multi-year scraper or
  upgrade five-year no-event status without written coverage and usage terms.

Official references:

- https://www.nasdaqtrader.com/snippets/tradehaltaccordion.html
- https://beta.nasdaqtrader.com/Trader.aspx?id=TradingHaltSearch
- https://nasdaqtrader.com/Trader.aspx?id=tradehaltcodes
- https://www.nasdaqtrader.com/content/administrationsupport/agreementstrading/THRSSFeedTermsCond.pdf

### SEC Trading Suspensions

- Source: official SEC suspension orders and historical listings.
- History: sufficient for a five-year positive-event lookup.
- Limitation: SEC suspensions are not equivalent to all exchange trading halts
  or resumptions, so the source cannot prove a general no-event result.
- Decision: `OFFICIAL_PARTIAL_SOURCE_ONLY`.
- Next action: use only as a positive regulatory-suspension supplement if a
  captured response contract is later added.

Official reference:

- https://www.sec.gov/enforcement-litigation/trading-suspensions

### NYSE Group Corporate Actions And Daily TAQ

- NYSE Group Corporate Actions covers NYSE, NYSE American, NYSE Arca, and NYSE
  Texas listings and includes symbol changes, suspensions, and delistings.
- Market Event Feed is programmatic, but its public factsheet describes only
  six months of historical retrieval and therefore does not satisfy the
  five-year requirement by itself.
- Daily TAQ covers all U.S. equities through CTA/UTP and includes admin
  messages, with history from 1993. It is the strongest documented candidate
  for five-year consolidated halt/resumption evidence.
- Authentication, exact files, price, storage rights, and redistribution terms
  require an ICE/NYSE quote and agreement.
- Decision: `PAID_SOURCE_APPROVAL_REQUIRED`.
- Next action: request a quote for five years of Daily TAQ CTA/UTP admin
  messages and confirm that trading-action messages can be retained as
  internal lineage evidence.

Official references:

- https://www.nyse.com/market-data/corporate-actions
- https://www.nyse.com/market-data/corporate-actions/market-event-feed
- https://www.nyse.com/data-products/catalog/daily-taq

### Existing Polygon/Massive Credential

- A `POLYGON_API_KEY` secret exists in `US_Alpha_Seeker`, not in Harvester.
- Official Ticker Events is experimental and currently documents
  `ticker_change` as the only event type.
- All Tickers exposes active/delisted state and `delisted_utc`, but the public
  contract does not prove a complete five-year symbol-event or
  suspension/resumption no-event history.
- Decision: `OFFICIAL_PARTIAL_SOURCE_ONLY`.
- Next action: do not add a new Harvester integration for this goal. Reconsider
  only if Massive supplies a non-experimental completeness and licensing
  contract.

Official references:

- https://massive.com/docs/rest/stocks/corporate-actions/ticker-events
- https://massive.com/docs/rest/stocks/tickers/all-tickers

## Procurement Packages

### Package A: Lowest-Cost Symbol-Change Runtime Proof

Scope:

1. Add the already-owned Finnhub token to the Harvester GitHub Actions secret
   named `FINNHUB_KEY`.
2. Do not copy the value into source, logs, variables, artifacts, or any
   `VITE_*` key.
3. Use it only for the existing server-side symbol-change producer.
4. Accept no new vendor cost until the natural run proves whether the existing
   account has Premium entitlement.

Approval phrase:

```text
APPROVE HARVESTER FINNHUB SECRET WIRING — copy the existing server-side
FINNHUB_KEY into pungking/US_Alpha_Seeker_Harvester for symbol-change lineage
only, no value disclosure, no VITE exposure, no forced run
```

Rollback: delete the Harvester `FINNHUB_KEY` secret. No code or historical
lineage row is rewritten.

### Package B: Delisting Entitlement

Scope:

1. Ask FMP to identify the minimum plan/entitlement that returns the complete
   paginated `stable/delisted-companies` dataset for at least five years.
2. Confirm internal automated use and storage of derived event evidence and
   response hashes in a public GitHub repository's Actions artifacts.
3. Confirm request limits and whether a complete empty response may support
   no-event evidence.
4. Upgrade the existing `FMP_KEY`; no new client-side secret is allowed.

Approval phrase after FMP supplies the plan and written terms:

```text
APPROVE FMP DELISTING ENTITLEMENT — <exact plan>, up to <exact cost>,
Harvester server-side FMP_KEY only, five-year internal lineage use, no raw
response redistribution, no VITE exposure
```

Rollback: revert the account plan or rotate/remove `FMP_KEY`; producer remains
safe as `BLOCKED_EXTERNAL_SOURCE_CONTRACT`.

### Package C: Consolidated Suspension/Resumption History

Scope:

1. Obtain an ICE/NYSE quote for Daily TAQ CTA/UTP admin messages covering the
   required rolling five years.
2. Confirm event coverage for all U.S. equities, halt and resumption message
   semantics, file completeness, corrections, and late data.
3. Confirm internal storage of parsed lineage plus response hashes. Do not
   store or publish raw licensed files in the public repository or public CI
   artifacts.
4. Keep Nasdaq/SEC sources as positive-event fallbacks only.

Approval phrase after a written quote and license are available:

```text
APPROVE NYSE DAILY TAQ PROCUREMENT — <exact product/years>, up to <exact cost>,
server-side Harvester access only, parsed internal halt/resumption lineage,
no raw file redistribution, no VITE exposure
```

Rollback: disable the source credential and retain existing unverified status;
do not delete previously captured positive-event evidence.

## Runtime And OOS Gate

- The first natural `schedule` or `repository_dispatch` run after `b2232648`
  remains the only allowed Finnhub runtime proof.
- No `workflow_dispatch`, forced run, rerun, or repeated artifact download is
  authorized by this package.
- Existing unverified Stage7 rows remain comparison-excluded.
- OOS readiness remains:
  - `EXECUTABLE_COHORT`: 0/30
  - `ACTIONABLE_BLOCKED_COHORT`: 0/30
  - comparable regimes: 0/2
  - `policyChangeAuthorized=false`
- A future row becomes comparison-eligible only after every required source
  supplies complete, fresh, source-backed evidence under the existing
  consumer contract.
