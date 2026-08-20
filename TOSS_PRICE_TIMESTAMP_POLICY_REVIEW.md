# Toss Price Timestamp Policy Review

Status: `DOCUMENTED_NULLABLE_TIMESTAMP_POLICY_REVIEW_REQUIRED`

This package is report-only. It does not authorize a Toss request, a source
promotion, a timestamp fallback, or a Stage6 policy change.

## Official contract

The canonical Toss OpenAPI document is:

- <https://openapi.tossinvest.com/openapi-docs/latest/openapi.json>
- captured SHA-256: `fccf49abd11f37f557bdd349138f4a03c42b829ebd8b5c14ab4907116fb84c7a`

For `PriceResponse`, the required fields are `symbol`, `lastPrice`, and
`currency`. `timestamp` is an optional `string | null` with `date-time` format.
The description states that it can be null when no trade has occurred.

## Runtime evidence and limit

Natural Harvester run `31691710767` returned all 300 requested rows. The current
parser accepted 192 timestamps and grouped 108 remaining rows under
`price_timestamp_missing`. Because it used `row.get("timestamp")`, the preserved
aggregate evidence cannot distinguish an absent key, null, blank text, or an
unparseable value. The current row-level cause is therefore
`SAFE_EVIDENCE_INSUFFICIENT`; this is not evidence of a parser field defect or a
provider contract violation.

The aggregate Telegram alert was delivered, while the published Drive artifact
still recorded `ALERT_PENDING_POST_PUBLISH`. That is separately classified as
`ALERT_DELIVERED_RECEIPT_NOT_PERSISTED`; it is not a timestamp root cause and is
not changed by this package.

## Current policy

- Keep the strict timestamp eligibility gate.
- Keep `eligible=false` and `tossEvidenceExcluded=true` when any row lacks a
  parseable payload timestamp.
- Do not substitute HTTP Date, local receipt, or `retrievedAt`.
- Do not add timestamp tolerance.
- Keep Google Drive/yfinance canonical and Toss `SHADOW_ONLY`.

The additive diagnostics classify timestamp shape using counts, safe type
names, per-batch reconciliation, and SHA-256 hashes of sorted response key sets.
They store no timestamp values, symbols, IP addresses, credentials, or raw
responses.

## Deferred policy decision

After one previously unseen natural Stage3 collector run captures complete
aggregate diagnostics, review whether documented null rows should remain a
run-level exclusion or become a separately excluded, non-comparable shadow
slice. That review must preserve point-in-time evidence and must not promote a
row without a provider payload timestamp. No policy migration is approved here.
