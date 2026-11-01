# Hybrid scheduling: offline implementation and separate rollout

Implementation target: STATIC_READY_NOT_ACTIVATED. Merging this code is not
permission to create operational receipts, install a sender, dispatch, or enable
hybrid mode. CI fixtures are not proof of punctual collection or source coverage.
Native implementation, offline review and PR/CI/merge were approved 2026-11-01;
installation and activation were explicitly excluded. No new dependencies.

## Unchanged defaults and boundaries

- `HARVESTER_SCHEDULE_MODE=legacy`; existing native cron remains active.
- First/second slots are Tuesday-Saturday 07:13/12:13 Asia/Seoul. Hybrid allows
  start only within [slot, slot + 3 hours). It does not limit completion to 3 hours.
- Only exact `stage3_completed` preempts collection and enters the OHLCV path.
  `harvester_daily_slot_requested` is a daily wake request, never Stage3.
- Collector, source policies, existing SEC request budgets and execution flags
  are unchanged. SEC uses the actual New York date at its phase start; it does
  not adopt a KST slot date as the financial collection window.
- Hybrid manual auto/all is rejected: an explicit first/second canonical slot
  is required, with the same reservation as automatic runs. Legacy manual
  behavior is unchanged until activation.
- Analysis and execution repositories, Phil and TradingAgents are out of scope.
  PAPER current-cutoff cash-flow completeness remains a separate blocker.

## Before any separately authorized activation

1. Pin the merged code and CI result. Review job-level write permissions and a
   future KST activation boundary. Complete an old-job drain: no queued/running
   collector with the legacy path may overlap hybrid activation. Do not cancel
   or rerun active jobs as an implicit part of this runbook.
2. Separately authorize creation/custody of branch `harvester-schedule-receipts`.
   Never merge it into main; no-delete, no-force, no history rewrite and no
   fallback branches. The Contents API token can technically write more than
   the two receipt files: limit repository scope and custody accordingly.
3. Pin exact UTF-8 `control.json` bytes and SHA-256. Schema
   `harvester-schedule-control.v1` contains repository, receiptBranch,
   activationAt, timezone=Asia/Seoul, startWindowSeconds=10800, pathVersion=1.
   Its hash is configured as `HARVESTER_SCHEDULE_CONTROL_SHA256`. This document
   supplies no operational hash, credentials or pretend activation timestamp.
4. Separately approve changing mode to hybrid. Source slots before activation
   remain forbidden. Any absent branch/control/hash must block, never create a
   substitute or quietly fall back to legacy.
5. Mac installation is optional and separately approved. Validate the existing
   Python/dependencies, system timezone and owner-only real config/receipt
   directories. Config has only repository, expectedControlSha256, timezone.
   Never store tokens in config or plist. Provision a repository-scoped token
   only through an approved environment mechanism. No automatic keychain/.env
   reads, power-setting changes, or changes to the existing Toss LaunchAgent.
6. The committed plist example is inert: placeholders and no `--send`. It must
   not be installed as-is. Default sender invocation validates with zero network
   calls. An explicitly approved operational invocation adds `--send` to
   `scripts/dispatch_harvester_daily_slot.py --config <private-config>
   --receipt-dir <private-directory>`; approval is not granted by this example.

## Reservation and failure semantics

The receiver uses `scripts/run_harvester_schedule_gate.py` phases resolve,
reserve, recheck, finish. Attempts are immutable
`slots/YYYY-MM-DD/{first|second}/attempt.json`; terminal outcomes use `result.json`.
Run/head/control/slot hashes must match. A create must return 201 followed by
exact byte readback. Existing, invalid, failed, ambiguous or cancelled attempts
consume the slot. A missing result is UNKNOWN, not permission to collect again.
Lost create response or readback failure blocks; no retry or artifact deletion.

The pre-import guard precedes Drive authentication and is rechecked at collector
entry. A runner that waited past the start deadline does not collect. Stage3
preemption after reservation may leave an unresolved attempt; preserve it.
GitHub's pending queue may replace a waiting run even with cancellation disabled.
That is an availability failure, never permission for a duplicate attempt. There
is no fairness, punctuality, exact original native trigger time, or successful
exactly-once guarantee. Native due slots are receiver-selected, not measured
GitHub original scheduledFor values.

Receiver metadata limits per full run: GET<=12, PUT<=2, split across four phases.
The Mac sender allows GET<=2 and POST<=1 per due invocation, no pagination,
redirect or retry. It verifies HTTPS Date against the local request interval;
this is not NTP proof or market-data timestamp substitution. An exclusive,
fsynced owner-only local attempt precedes POST. A 204 means only dispatch accepted.
An uncertain POST remains consumed locally. Never delete attempts to resend.
Provider request budgets do not increase with these metadata budgets.

Public records contain only schema/status, slot/run/head identifiers, timestamps,
safe outcome enums and hashes. No credentials, symbols, provider responses,
positions, account identifiers or exception response bodies. A successful
collector process is not proof of complete source coverage. Result records retain
separate collector, SEC, Notion and job outcomes.

## Bounded future verification and rollback

After separate rollout approval, select one first natural eligible slot and
inspect existing metadata/receipts once. Record scheduled slot, workflow creation,
start, collection start/end, disposition and outcome; do not dispatch or rerun to
manufacture natural proof. Verify Mac-awake and Mac-off cases separately when
they naturally occur: no claim about an unobserved case. At 08:00 a valid first
slot can still be requested; at 10:13 it expires. Mac-off leaves native cron as
the only trigger; it does not wake the laptop or guarantee an analysis deadline.
No perpetual monitoring or new automation is authorized here.

For gate-only rollback, stop the optional sender and drain jobs before any
separately approved mode change. Preserve all remote/local receipts and control
history. Legacy mode no longer enforces hybrid per-slot deduplication; never
toggle it to replay a consumed slot. Do not revert source policies or delete
receipt history. Unexpected existing receipts require an evidence review, not
forced replacement.

Later host migration: disable/drain the old sender, preserve both local and
server attempts, validate the new host and token separately, then explicitly
authorize activation. Repository/date/batch identity stays unchanged. Changing
hostname, run ID, code version, keys or local directory is not a reset mechanism.

## Offline completion evidence

Run admission, integration, Mac-assist checks and all checks registered in
telegram-routing-ci.yml; compile Python; check whitespace and independent review.
Only merge after all related CI passes. Runtime evidence, receipts and secrets
must never be included in the implementation commit. Runtime latency remains
unverified until the separately approved natural observation.
