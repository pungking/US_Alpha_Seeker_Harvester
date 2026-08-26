# Toss Price Timestamp Policy Review

Status: `TOSS_NULLABLE_VALID_SLICE_STATIC_READY`

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

The preserved natural aggregate returned all 300 requested rows: 257 had a
parseable payload timestamp and 43 carried documented `null`; absent, blank,
unparseable, and unknown rows were zero. Six of the parseable rows were after
the local receipt but no later than the HTTP `Date`, so they remain excluded as
`LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE`. The resulting report-only valid
slice is 251 rows. HTTP `Date`, local receipt, and `retrievedAt` remain diagnostic
references only.

The aggregate alert receipt is persisted in both local and Drive evidence. A
later no-op collector poll must preserve that terminal local artifact rather
than replace it with `NOT_RUN`; this durability rule does not change the
timestamp classification.

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

## Runtime proof

The additive slice and terminal-retention contracts are statically verified.
Their post-merge runtime proof remains limited to the first previously unseen
natural Stage3 chain. Until then, strict run-level exclusion remains in force;
no row without a provider payload timestamp is promoted.
No policy migration is approved here.
