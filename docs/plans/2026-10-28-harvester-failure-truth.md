# Harvester upload failure truth and Toss clock diagnostics

Scope: Harvester only. No provider calls, workflow dispatch, actual Telegram
send, clock adjustment, production state writes, or PAPER policy changes.

1. RED/GREEN: make the existing tee pipeline propagate Python failure. Keep
   always-run failure artifacts; do not let failed collection start downstream.
2. RED/GREEN: preserve upload_json's signature/None return and JSON bytes.
   Resolve an exact unique parent/name before writing; fail on duplicates or
   incomplete listing. Retry only pre-write idempotent lookup. Submit one
   create/update request. After an uncertain write, reconnect, resolve the same
   identity and compare downloaded bytes by SHA-256 once. Never resubmit an
   uncertain write; mismatch/missing/ambiguous/read failure stays failed.
   This is conservative reconciliation, not resumable-session recovery.
   Report actual lookup/write attempts and safe cause without file IDs/raw text.
3. RED/GREEN: label Toss payload-vs-receipt and HTTP-Date-vs-receipt offsets
   separately, explicitly retain MAC_CLOCK_SYNC_UNVERIFIED. Do not alter
   timestamp eligibility, tolerance, fingerprints, receipt or no-op retention.
4. Run all existing offline contract checks, compile, diff check, independent
   whole-diff review, non-force PR/CI/merge. No runtime reproof in this task.

Caller impact: all upload_json users (daily/history, mapping, progress, SHADOW
reservation/receipt and Stage4 readiness) keep bytes and return compatibility;
uncertain writes now stop rather than risking duplicate writes. No source schema,
Stage6/OOS policy, schedule frequency or execution defaults change. Ordinary
successful writes retain their existing acknowledgement semantics. Read-back
proof is applied only after a transport/5xx ambiguity, not to authorize another
write. A single-writer expectation remains; this is not cross-host locking.

Done: shell failure regression, exact-byte create/update reconciliation, missing/
duplicate/hash/read-failure rejection with no second write, safe cause/count
alerts, strict Toss exclusion and existing fixtures pass; required CI passes.
Rollback: revert this merge. Do not repair or delete historical artifacts.

Review correction: SDK num_retries=0 does not disable httplib2's internal lost-
response replay. Upload-related requests therefore use the already-installed
Google AuthorizedSession with refresh replay disabled and redirects disabled,
adapting only the response interface for the existing generated Drive SDK.
HTTP errors are stripped of raw bodies before SDK logging. Ordinary reads outside
upload_json retain their existing implementation. writeAttempts counts logical
create/update invocations, not the resumable protocol's initialization/chunks.
Real-SDK fixtures cover successful upload, lost-response PUT count, 401/redirect
rejection and non-JSON/JSON 403 log redaction without network access.
