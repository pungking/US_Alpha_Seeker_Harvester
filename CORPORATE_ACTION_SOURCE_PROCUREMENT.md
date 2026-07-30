# Corporate-Action Source Procurement

Generated: 2026-07-31

## Decision

The current Harvester cannot truthfully certify five-year symbol-change,
delisting, suspension, and resumption no-event coverage.

| Event | Decision | Primary source | Fallback | Five-year no-event proof |
|---|---|---|---|---|
| Symbol change | `DEFER_AND_KEEP_COMPARISON_BLOCKED` | Finnhub Symbol Change Premium | FMP Symbol Changes, then exchange-specific products | Existing key is wired, but runtime returns HTTP 403; public terms do not prove plan mapping, five-year completeness, or artifact rights |
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
| Harvester `FINNHUB_KEY` secret | Present and wired server-side |
| `US_Alpha_Seeker` `FINNHUB_KEY` secret | Present, but repository-scoped and not readable or transferable by this audit |
| Nasdaq/NYSE paid product credential | Not present |
| FMP delisting runtime | `BLOCKED_EXTERNAL_SOURCE_CONTRACT`, `entitlement_or_auth_http_402` |
| Nasdaq halt runtime | `SUCCESS`, but historical coverage is one year versus the requested five years |
| Finnhub local entitlement probe | `EXISTING_SOURCE_ENTITLEMENT_REQUIRED` |
| Finnhub natural run `30369628942` | Reused legacy dispatch coverage; producer refresh contract defect confirmed |
| Finnhub natural run `30497551790` | `FINNHUB_SYMBOL_CHANGE`, `BLOCKED_EXTERNAL_SOURCE_CONTRACT`, `entitlement_or_auth_http_403` |
| Lineage structural coverage | 2,924/2,924, missing 0, duplicate 0 |
| Comparison-ready lineage | 0/2,924 |

Natural run `30369628942` was the first eligible run after the Finnhub secret
wiring. Its dispatch path reused legacy `FMP_OR_FINNHUB_SYMBOL_CHANGE`
coverage instead of invoking the merged producer, so it is evidence of a
dispatch refresh defect rather than Finnhub runtime entitlement proof.

Run `30497551790` includes merge `53bd087d` and is the completed natural
runtime proof. It made one server-side request for the configured five-year
window and returned HTTP 403. The local and runtime entitlement decisions now
agree. No additional probe is authorized.

## Official Feasibility Matrix

### Finnhub Symbol Change Premium

Final procurement verdict: `DEFER_AND_KEEP_COMPARISON_BLOCKED`.

Technical entitlement status: `EXISTING_PLAN_UPGRADE_REQUIRED`. This is not a
purchase recommendation: the public contract is insufficient to name an exact
licensable plan and price for this repository.

| Contract item | Officially verified | Procurement result |
|---|---|---|
| Endpoint | `GET /api/v1/ca/symbol-change`; Premium access required | Existing key is not entitled |
| Documented market scope | US-listed, EU-listed, NSE, and ASX securities | "US-listed" is documented; complete all-U.S.-exchange coverage is not warranted |
| Query | Required `from` and `to` dates; response echoes `fromDate` and `toDate` | Exact bounded windows are supported |
| Response size | Maximum 2,000 events per response | The API documents no cursor, page, or offset parameter; date segmentation is not pagination |
| Effective date | `atDate` | Can be mapped to internal `eventEffectiveAt` without invention |
| Five-year history | No public historical start date or completeness SLA | Five-year positive-event and no-event proof is unverified |
| As-of lineage | Response includes query-window echoes, not vendor `sourceAsOf` or `retrievedAt` | Harvester may capture `retrievedAt`; `toDate` remains a coverage bound, not vendor as-of evidence |
| Public personal plan | All-In-One: USD 3,500/month, billed annually, personal use | USD 42,000 annual list commitment before tax; endpoint inclusion is not explicitly mapped |
| Public rate limit | All-In-One: 300 fundamental calls/minute; general cap 30 calls/second | No endpoint-specific rate is published |
| Enterprise plan | Flexible quote, commercial use, redistribution right, unlimited API calls | Only public option that expressly addresses redistribution; exact price is unavailable |
| Existing key upgrade | Public FAQ permits same-category plan changes through support | It does not state whether this endpoint is enabled on the existing key or requires a new key |
| Cancellation | No refunds; email cancellation requires 30 days' notice; API access is revoked on cancellation | Exact downgrade and annual-commitment treatment require written confirmation |
| Storage and redistribution | Personal terms prohibit sharing data or derived results without written approval and require data deletion when the subscription ends | Raw response storage/redistribution is not approved; public derived lineage and response hashes require written permission or private storage |

The repository is public. Therefore the public All-In-One personal license is
not sufficient evidence for publishing symbol-change event lineage or derived
results in repository artifacts. A response SHA-256 is technically possible,
but its retention/publication rights are not granted by the public terms.

The current producer already performs deterministic bounded requests and
rejects schema/date-echo/limit failures. No code change is required before a
written entitlement and license are obtained.

Required written answers from Finnhub:

1. Does a named plan include `/api/v1/ca/symbol-change`?
2. Does it provide complete US-listed history for a rolling five-year window,
   including a complete empty response that can prove no event?
3. Which U.S. exchanges and security types are included?
4. Is the 2,000-event response limit handled only by date segmentation, and
   what rate-limit bucket applies?
5. Can the existing account/key be upgraded without key rotation?
6. May parsed old/new symbol, effective date, lineage counts, and response
   SHA-256 be retained in private artifacts? May any derived metadata appear
   in public GitHub Actions artifacts?
7. What data must be deleted after cancellation?
8. What are the exact total annual price, taxes, commitment, renewal,
   cancellation, and downgrade terms?

Official references:

- https://finnhub.io/docs/api/symbol-change
- https://finnhub.io/pricing
- https://finnhub.io/pricing-startups-and-enterprise
- https://finnhub.io/faq
- https://finnhub.io/terms-of-service
- https://finnhub.io/docs/api/rate-limit

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

### Package A: Finnhub Symbol-Change Contract Decision

Scope:

1. Keep the current server-side `FINNHUB_KEY`; do not rotate it before Finnhub
   states whether an entitlement change can reuse the key.
2. Request a written quote for the exact endpoint, rolling five-year
   completeness, all-U.S. scope, retention, and redistribution terms.
3. Do not purchase All-In-One solely from its name. The public table does not
   map Symbol Change Premium to that plan.
4. Prefer an Enterprise written contract if derived lineage remains in public
   repository artifacts. Otherwise move licensed evidence to private storage
   before considering a personal-use plan.
5. Keep `comparisonCoverageStatus=blocked_external_source_contract` until one
   natural runtime proves the purchased contract.

Safe no-cost inquiry authorization:

```text
AUTHORIZE FINNHUB SYMBOL CHANGE CONTRACT INQUIRY — no purchase, no key change,
no API probe; request written endpoint entitlement, rolling five-year all-US
coverage, retention and redistribution rights, existing-key upgrade path,
exact annual price, and cancellation terms only
```

Current safe defer decision:

```text
DEFER FINNHUB SYMBOL CHANGE PREMIUM — keep OOS comparison blocked, no purchase,
no secret change, no additional probe
```

Purchase approval is intentionally not valid until Finnhub provides a written
plan and exact total commitment:

```text
APPROVE FINNHUB SYMBOL CHANGE PREMIUM — <written plan and quote id>,
up to USD <exact total annual commitment>, server-side Harvester only,
5-year symbol-change lineage, no VITE exposure,
no raw response redistribution
```

Minimal post-approval change:

1. Apply the entitlement to the existing account/key if Finnhub confirms that
   path; otherwise replace only the server-side GitHub `FINNHUB_KEY`.
2. Do not add a client key, dependency, endpoint, or Stage7 backfill.
3. Run no manual probe. Verify the first natural Harvester run once.

Rollback/downgrade:

1. Ask Finnhub support to downgrade/cancel under the written quote terms.
2. Expect no refund, at least 30 days' email notice, and immediate API access
   revocation on cancellation under the public terms.
3. Remove or rotate only the server-side secret if required.
4. Delete licensed data when the subscription ends unless the written contract
   grants retention; keep OOS comparison blocked rather than fabricating
   continuity.

### Symbol-Change Alternative Priority

| Rank | Source | Why it ranks here | Blocking evidence |
|---:|---|---|---|
| 1 | FMP Symbol Changes | Existing vendor and dedicated stable endpoint; materially lower public plan prices | Exact endpoint tier, five-year completeness, and display/redistribution rights require confirmation |
| 2 | Nasdaq Daily List + NYSE Group Corporate Actions | Highest-authority exchange evidence and long history | Fragmented exchange scope, multiple contracts, and no single all-U.S. no-event contract |
| 3 | Existing Massive/Polygon Ticker Events | Existing credential and ticker-change event support | Experimental/partial contract; no verified five-year completeness |

No alternative may be promoted from this ranking alone.

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

- Run `30497551790` completed the entitlement one-shot and returned HTTP 403.
- No additional probe, `workflow_dispatch`, forced run, rerun, or repeated
  artifact download is authorized before entitlement changes.
- Existing unverified Stage7 rows remain comparison-excluded.
- OOS readiness remains:
  - `EXECUTABLE_COHORT`: 0/30
  - `ACTIONABLE_BLOCKED_COHORT`: 0/30
  - comparable regimes: 0/2
  - `policyChangeAuthorized=false`
- A future row becomes comparison-eligible only after every required source
  supplies complete, fresh, source-backed evidence under the existing
  consumer contract.
- After an approved entitlement change, verify only the first natural
  `schedule` or `repository_dispatch` artifact. Do not backfill historical
  decisions or change Stage6 policy.
