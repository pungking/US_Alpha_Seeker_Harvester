# SEC/FINRA SHADOW_ONLY Capability Contract

## Decision

Overall status: `OFFICIAL_SOURCE_CONTRACT_INCOMPLETE`.

The existing `corporate-action-lineage-v1` and SHADOW_ONLY safety invariants are
sufficient for a static contract. No runtime provider, planner, ledger, or new
dependency is needed yet.

The remaining primary blocker is the exchange-listed Reg SHO source matrix.
The SEC assigns threshold-list dissemination to the security's primary-listing
SRO, while FINRA's `thresholdList` machine contract covers its OTC scope. An
approved primary-SRO source and schema matrix is therefore required before an
exchange-listed threshold collector can be called capability-ready.

FINRA datasets remain `LICENSE_OR_ACCESS_REVIEW_REQUIRED` until a server-side
Public credential and FINRA API terms acceptance are approved. This is separate
from source semantics and identifier lineage.

## Reused Contracts

- Effective-dated ticker rename, merger, delisting, and suspension lineage:
  `corporate-action-lineage-v1`.
- Report-only invariants: `mode=SHADOW_ONLY`,
  `policyImpact=NONE_REPORT_ONLY`, `canonicalSourceChanged=false`.
- Deterministic fixture/checker pattern under `fixtures/` and `scripts/`.
- Existing Google Drive/yfinance canonical data path; no fallback or source
  replacement is authorized.

## Source Semantics

| Source | Evidence meaning | Publication model | Prohibited inference | Static status |
|---|---|---|---|---|
| SEC Forms 3/4/5 | Insider/reporting-owner ownership transactions | Use SEC acceptance time; model each form's filing delay and amendments | Current buy/sell recommendation | `SHADOW_CAPABILITY_READY` |
| SEC Schedule 13D/13G | Beneficial ownership position disclosure | Use SEC acceptance time; deadlines vary by schedule and filer class | Contemporaneous trade or current sentiment | `SHADOW_CAPABILITY_READY` |
| SEC Form 13F | Institutional holdings snapshot at quarter end | Use SEC acceptance time; generally filed within 45 days | Real-time manager trading or complete current portfolio | `SHADOW_CAPABILITY_READY` |
| FINRA consolidated short interest | Twice-monthly aggregate short position snapshot | Settlement date plus official publication date | Daily short-sale volume | `LICENSE_OR_ACCESS_REVIEW_REQUIRED` |
| FINRA Reg SHO daily short volume | FINRA-published off-exchange short-sale activity volume | Trade date plus observed official publication | Short interest, end-of-day position, or consolidated exchange volume | `LICENSE_OR_ACCESS_REVIEW_REQUIRED` |
| Reg SHO threshold list | Regulatory threshold status from persistent aggregate fails criteria | Official SRO list date/publication, including late or amended lists | Issuer misconduct or direct short signal | `OFFICIAL_SOURCE_CONTRACT_INCOMPLETE` |

Every source uses `publishedAt<=decisionAt`. Event dates, quarter-end dates,
settlement dates, and trade dates are not substitutes for publication time.
Historical filings never become current sentiment evidence.

## Official Machine Contracts

- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
  provide submissions metadata keyed by ten-digit CIK and accession lineage.
- [SEC filing technical specifications](https://www.sec.gov/submit-filings/technical-specifications)
  define the current machine-readable ownership, Schedule 13D/G, and Form 13F
  XML contracts.
- [SEC Forms 3/4/5 guide](https://www.sec.gov/files/forms-3-4-5.pdf) defines
  their distinct reporting purposes and timing.
- [SEC 13D/G modernization release](https://www.sec.gov/newsroom/press-releases/2023-219)
  defines current filing deadlines and structured-data requirements.
- [SEC Form 13F FAQ](https://www.sec.gov/rules-regulations/staff-guidance/division-investment-management-frequently-asked-questions/frequently-asked-questions-about-form-13f)
  describes quarter-end holdings and the 45-day filing window.
- [FINRA short-interest catalog](https://www.finra.org/finra-data/browse-catalog/equity-short-interest)
  defines settlement-date snapshots, publication timing, and revisions.
- [FINRA short-sale-volume catalog](https://www.finra.org/finra-data/browse-catalog/short-sale-volume)
  limits the evidence to FINRA-published off-exchange reporting-facility data.
- [FINRA API documentation](https://developer.finra.org/docs) identifies the
  `consolidatedShortInterest`, `regShoDaily`, and `thresholdList` datasets and
  their credential requirements.
- [SEC Regulation SHO guide](https://www.sec.gov/investor/pubs/regsho.htm)
  defines threshold-security status and primary-listing SRO dissemination.
- [FINRA OTC threshold catalog](https://www.finra.org/finra-data/browse-catalog/otc-threshold)
  defines FINRA's OTC threshold-list scope and amendment timing.

## Identifier And Amendment Rules

1. Symbol-only joins are invalid. Stable identifiers and effective dates must
   precede the existing corporate-action alias chain.
2. Reporting-owner CIK is not subject-issuer CIK.
3. Form 13F filing-manager CIK is not held-issuer identity. A reported security
   identifier plus effective corporate-action lineage is required.
4. OTC and exchange-listed Reg SHO source scopes must not be merged.
5. Amendments retain their own accession/publication timestamp and response
   SHA-256. They never rewrite information into an earlier decision snapshot.
6. Rename, merger, and delisting lineage gaps classify the row as
   `IDENTIFIER_LINEAGE_INCOMPLETE`; they are not guessed from punctuation.

## Additive Static Schema

`fixtures/sec_finra_shadow_capability_contract.json` is the sole additive
contract fixture. It records source semantics, publication clocks, amendment
and identifier policies, official contracts, response-hash requirements, and
synthetic hashed-identifier cases. It contains no live data or actual symbol.

Capability status precedence is deterministic:

1. `OFFICIAL_SOURCE_CONTRACT_INCOMPLETE`
2. `IDENTIFIER_LINEAGE_INCOMPLETE`
3. `PUBLICATION_DELAY_NOT_MODELED`
4. `LICENSE_OR_ACCESS_REVIEW_REQUIRED`
5. `SHADOW_CAPABILITY_READY`

## Bounded Probe Scope

No probe is authorized by this document. Before a future probe, approve the
FINRA Public credential/terms contract and the SEC fair-access identity policy.

The maximum proposed one-shot scope is:

- SEC: at most three Submissions metadata requests and at most one filing
  document request for each of the Forms 3/4/5, Schedule 13D/13G, and Form 13F
  families.
- FINRA: OAuth at most once, metadata at most once, and one `limit=1` data
  request for each of `consolidatedShortInterest`, `regShoDaily`, and
  `thresholdList`.
- Exchange-listed SRO threshold data: zero requests until the primary-listing
  SRO source/schema matrix is approved.
- No pagination, retry, bulk download, recurring activation, raw response
  storage, canonical-source replacement, Stage6/OOS policy impact, or
  broker/sidecar/state mutation.
