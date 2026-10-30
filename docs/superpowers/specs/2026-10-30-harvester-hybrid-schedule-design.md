# Harvester native schedule with optional Mac assist

Status: DESIGN_APPROVED_FOR_IMPLEMENTATION_PLANNING. No implementation or activation approval.
Date: 2026-10-30, Asia/Seoul.
Work item: HARVESTER_NATIVE_SCHEDULE_MAC_ASSIST_DESIGN_V1.
Baseline: 536205c29e2961622182172443730925a9967d26, locally verified main/origin/main.

## 1. Intent and selected approach

The operator selected option 1: retain GitHub native scheduling now, add an
optional Mac trigger, and transfer that optional trigger to an always-on server
later. Collection continues on GitHub-hosted runners in both cases. This does not
create a second collector on the Mac or migrate the execution engine.

The operator reports typical Mac availability of 08:00-17:00 and 19:00-06:00 KST,
not an uptime guarantee. Availability at 07:13 is unverified; 12:13 is usually
covered. Awake, logged in, network-connected and correctly configured are all
required; powered-on alone is insufficient. Weekend availability is not assumed.

Approaches considered:

1. Native GitHub cron plus optional Mac assist: SELECTED. No additional external
   timer service, but native delivery delay remains when the Mac is unavailable.
2. Independent server timer plus optional Mac: deferred until server deployment.
   Changes the trigger host, not source data, collector or admission semantics.
3. Mac-only scheduling: rejected because the first slot can occur while it is off.

This is resilience against a missing/late trigger, not a guarantee of punctual
runner start, analysis completion, or exactly-once successful collection.

## 2. Verified current behavior

- `.github/workflows/main.yml:10-12` has first `13 22 * * 1-5` and second
  `13 3 * * 2-6`: Tuesday-Saturday 07:13 and 12:13 KST.
- The workflow concurrency group serializes main collection, but does not stop
  the same batch running again after completion. Incoming repository_dispatch
  currently cancels an in-progress run, regardless of dispatch event type.
- `harvester.py:596` supports explicit first/second and manual all/auto. Its main
  path currently treats every repository_dispatch as a Stage3 OHLCV handoff.
  A new timer event must NOT inherit that path or preemption behavior.
- The recurring SEC producer is currently restricted to the native second cron.
  Its collection window is New York's actual date after daily collection, before
  SEC collection. Manual second-batch dispatch currently skips this producer.
- The analysis repo's `scripts/dispatch-auto-scheduler.mjs` demonstrates exclusive
  local attempt receipts and single-send behavior, but its endpoint and event are
  analysis-specific. Do not invoke, repoint or modify that analysis dispatcher.
- Preserved latency assessment:
  `~/.us-alpha-seeker/evidence/harvester-schedule-delivery-latency-20261030/result-safe.json`.
  It reports a pre-workflow-creation gap, not restored punctuality. Its nominal
  slot estimate is not authoritative evidence of GitHub's original scheduledFor.

## 3. Architecture and scope

Both native cron and the proposed `harvester_daily_slot_requested` dispatch enter
one Harvester-side admission path before provider/Drive collection. Stage3 events
retain their existing OHLCV path and exact trigger-file handshake.

The Mac emits a bounded wake/request signal; it never downloads source data,
holds broker credentials, rewrites Drive pointers, or invokes the collector.
Later, a server runs this same sender with the same slot identity and safety
rules. The old sender is disabled before the replacement is enabled.

Required production changes, only after design and implementation approval:

- Explicit event routing in main.yml and Harvester; only `stage3_completed`
  retains existing preemption. The assist event must not cancel daily or OHLCV.
- A small Harvester-specific sender/slot normalizer and a common admission helper.
- A durable scheduling receipt, described below; no trading ledger, broker
  abstraction, dependency, general scheduler framework or additional provider.
- Offline fixtures and an inert launchd example. No installed/loaded agent by
  default, no perpetual polling or new Codex automation.

No changes to US_Alpha_Seeker, alpha-exec-engine, Stage policy, source eligibility,
Toss clock rules, or execution flags. Mac-only Toss is not moved to GitHub.

## 4. Slot identity and late-start policy

Unchanged nominal slots are first/second on Tuesday-Saturday in Asia/Seoul.
Identity is repository + KST slot date + explicit canonical batch. Sender host,
event name, run ID, code version and adjustable deadline are NOT identity fields.
They cannot be changed to obtain a second attempt for the same scope.

Approved design choice, not operational activation: admit a collector start only in
the half-open interval `[scheduledFor, scheduledFor + 3 hours)`. This permits an
08:00 recovery of the 07:13 first slot without unlimited late backfill. It also
means a native second run first starting at 18:50 would be expired, not collected
silently. This is an explicit behavior change, not restoration of an existing
guarantee. Retain the existing 360-minute job runtime limit; no completion SLA or
promise of premarket Stage6 delivery follows from the proposed start window.

For Mac requests, carry explicit UTC scheduledFor and batch. The receiver derives
the slot date, weekday and deadline itself and rechecks against receiver time
immediately before admission and again before starting collection. Never trust a
client-supplied future deadline or local hour to choose a batch.

Native cron is a wake signal: because its original scheduledFor is not exposed,
derive the most recent nominal occurrence of its EXACT cron at workflow creation
time. Mark this `RECEIVER_SELECTED_DUE_SLOT`, not verified original delivery time.
Admission still checks the current time. A very old wake signal may service only
the currently due slot; it cannot request a prior day's backfill. Unknown cron,
future time, expired window, rerun, non-main ref or invalid routing fails closed.

Once hybrid mode is activated, ordinary workflow_dispatch all/auto is rejected
before collection. Explicit manual first/second must provide an exact slot and
use the same admission path; no manual bypass. Ad-hoc historical recovery needs
a separately reviewed scope. Stage3-triggered OHLCV is not a daily slot and must
not consume its receipt.

## 5. Durable admission, not just concurrency

Approved design choice: safe scheduling receipts on a dedicated GitHub branch
`harvester-schedule-receipts`, using repository Contents API and the existing
main-workflow concurrency group. This is NEW orchestration metadata, not existing
functionality. Creating that branch or writing receipts is operational activation
and is NOT authorized by the present design work.

The branch contains a pinned control document with schema, activation boundary,
approved start-window policy and receipt-path contract. Each slot has an immutable
`slots/<date>/<first|second>/attempt.json` and optional immutable `result.json`.
Only status, timestamps, run/head and content hashes are public. No raw response,
source row, symbol, IP, private file ID, portfolio data or credential is stored.
The branch is never merged into main and contains no workflow files.

Protocol, serialized before any source request:

1. Verify the control document and branch exist and match approved configuration.
   Missing branch/control, permission errors or uncertain reads block; never
   silently recreate state. A cache miss is not proof of no previous attempt.
2. Read the exact slot attempt path. Any existing record, including corrupt,
   reserved, failed or outcome-unknown, blocks another automated collection.
3. For an absent path, create the attempt file without an update SHA. Require a
   confirmed create response and read-back of exact bytes/hash and own run ID
   before collection. Conflict, timeout or ambiguous creation means no collection
   and no write retry. Never overwrite another reservation.
4. Recheck the start deadline. Expiration after reservation consumes the attempt
   but causes no source call; preserve it for explicit review.
5. Run the existing collector once. Preserve underlying authorized source request
   and bounded read-recovery contracts; this is not an authorization to widen them.
6. Write one terminal result linked to the attempt hash, if the process survives.
   A missing result after cancellation/crash remains unresolved, not retryable.

The guarantee is at most one automatically admitted collector attempt per slot
under the controlled writer protocol, NOT exactly-once successful external side
effects. GitHub serialization is not used as durable proof. Contents API failures
always stop the runner; its response codes are not interpreted as retry permission.
An ambiguous write may leave a permanent reserved attempt and sacrifice liveness.

Scope permissions to the admission/result writer jobs as narrowly as practical;
the GitHub Contents write permission is repository-wide, not a path-level grant.
The Mac's dispatch credential never writes the receipt branch. Credential choice,
branch access rules and no-delete/no-force policy must be verified before rollout;
do not claim protection against an administrator deleting/resetting history.
No receipt cleanup, branch recreation, cache-based fallback or automatic repair.

## 6. Optional Mac sender

Use a new user LaunchAgent after separate installation approval. Its calendar
entries match the existing slots; RunAtLoad performs one bounded due-slot check
on login/load. A sleeping machine may receive a deferred calendar wake; shutdown
does not replay a missed calendar job, so the load check matters. Do not modify
power settings, demand administrator privileges, or repurpose the Toss agent.

The sender recalculates due slots on every invocation, rather than replaying
queued payloads. A host timezone mismatch, invalid clock/config, unavailable
credential or offline state cannot fabricate a slot or advance collection.
Failure to prove the required clock/config checks disables assist for that
invocation; native scheduling remains available. Clock proof specifics and
secret retrieval reuse are implementation details to test, not permission to
read or expose .env/keychain values during design.

Per due slot and invocation: read the pinned control and exact attempt path once
each; an existing attempt suppresses dispatch. A missing/unavailable control or
an uncertain receipt read blocks assist. Before dispatch, use exclusive owner-only
local attempt creation and fsync, following the existing analysis sender pattern.
One POST at most per slot from this host, no automatic retry, no pagination, no
raw error/response logging. A timeout is outcome-unknown, not proof of non-delivery.
The server still arbitrates races between native and Mac signals.

Network changes alone do not trigger an infinite retry loop. If the Mac misses
its opportunity, native GitHub remains the fallback. To retry a failed local
dispatch attempt requires explicit recovery review, not deleting its receipt.
Do not silently promise delivery merely because the Mac was usually available.

## 7. SEC second batch and current-window truth

After an admitted daily second batch succeeds, the existing recurring-enabled
setting may permit the SEC producer regardless of native versus assist trigger.
This broadened trigger predicate needs approval as part of cutover; it must not
enable a disabled provider. First, Stage3, rejected or failed daily runs cannot
invoke it. Retain existing authorization, budget, sentinel and publication guards.

Preserve the existing definition: calculate SEC collectionWindow from actual
America/New_York wall date immediately before the SEC phase, then freeze it for
that attempt. Store it separately from the intended KST daily slot. Do not set it
to the KST date, backdate publication time, or send a second SEC request simply
because execution crosses midnight. Publication/retrieval timestamps remain real.
A faster run can legitimately yield a different window from an old delayed run.

Test this interpretation against the existing Stage0 consumer and current-window
readiness contract before implementation acceptance. If those consumers require
a different window, fail the compatibility test and amend the design; do not
silently change another repository or promote stale evidence. The schedule work
does not strengthen existing SEC cache durability into a new exactly-once claim.

## 8. Transition and future server migration

1. Approve this document, then prepare the implementation plan.
2. Implement and review offline fixtures; merge inert support, retaining existing
   scheduling behavior and with the assist event unable to collect by default.
3. Separately approve activation: explicit future KST date boundary, control
   document, receipt branch custody, GitHub permissions, token delivery and Mac
   LaunchAgent installation. Preflight must show no overlapping old daily job.
4. At that boundary enable common admission for ALL daily paths together, then
   install/enable the Mac sender. If no valid control exists, enabled admission
   blocks collection rather than falling back to an unguarded path.
5. One separately authorized natural runtime evaluation must distinguish admitted,
   skipped, expired and failed runs. No forced analysis, extra provider proof or
   recurring Codex monitor is created by this design.

Implementation detail: a single workflow-local mode, default `legacy`, selects
the old native/manual behavior versus `hybrid` admission. Activation of `hybrid`
is a separate reviewed change, not an execution flag. New assist events are
rejected in legacy mode. Do not mix modes within a run or use sender data to pick
the mode. CI must prove both defaults and the transition.

Routine rollback disables the Mac sender only; native runs keep the common gate
and all receipts. If the shared gate is defective, stop daily admissions and
diagnose; do not automatically revert to an unguarded collector in the same slot.
Full rollback requires a new future boundary and preservation of attempts.

Later server migration disables and drains the Mac sender, preserves its local
attempts and central receipts, verifies the new host's timezone/clock/credentials,
and changes only the sender host. Keep the same slot key; host migration must not
reset reservations. Native cron remains unless separately approved for retirement.
Agent_Javis hosting and broker automation are not part of this work item.

## 9. Acceptance and evidence

Offline fixtures must cover:

- Both cron mappings, KST weekdays, UTC rollover, and US DST for SEC dates.
- Mac awake, asleep then waking, off then login at 08:00, off all day, offline,
  invalid timezone/clock, and expired late wake; no invented recovery success.
- Simultaneous native/Mac requests, native delayed until after Mac completion,
  same-slot manual attempt, auto/all rejection, rerun rejection and non-main.
- Missing/corrupt control, unavailable receipt store, create conflict, lost create
  response, incomplete reservation, cancellation before/after source access,
  unknown result and failure with provider effects; no automatic replay.
- Stage3 retains its exact handshake and priority; assist is not Stage3 and
  does not cancel daily or OHLCV. Interrupted daily attempt stays consumed.
- Native and assist second batch use the same SEC gate/window rule, first batch
  makes no extra SEC calls, midnight does not fabricate timestamps or retry.
- Sender redaction, exclusive attempts, no actual requests in fixtures, no broker
  calls or execution policy changes, same inputs give identical slot/hash basis.
- Legacy default unchanged; mode transition, rollback, server host handover.

Static PASS means fixtures/review/CI pass, not runtime delivery. Runtime reporting
keeps nominal slot, derived-slot basis, actual workflow creation, job start,
provider start/end and artifact publication separate. Measure improvement; never
report original GitHub scheduledFor or a punctuality SLA that was not observed.

## 10. Boundaries and remaining approval

The operator approved the document and implementation planning, including the
3-hour admission window, safe scheduling receipt branch and automatic/manual
shared-gate behavior. This approval does not authorize implementation or rollout.
Installation, repository permissions/variables, real dispatch, receipt writes,
provider requests, workflow activation and runtime proof remain separately gated.

The PAPER operational binding goal remains separate. Harvester timing does not
provide complete cash-flow evidence through current equity observation, authorize
orders, or complete historical evidence gaps. No completion date is inferred.

## Official references

- GitHub schedule limitations and dispatch routing:
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- GitHub concurrency is run scheduling, not durable deduplication:
  https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency
- GitHub Contents API create/update SHA and failure responses:
  https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
- Apple archived launchd scheduling guidance, including sleep versus shutdown:
  https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html

These sources describe platform behavior, not verified entitlement, branch
protection configuration, on-host launchd installation, or a successful run.

## Design review record

- Independent builder check: no material planning questions remained.
- Existing schedule-slot static contract: PASS against unchanged product code.
- Proposed window arithmetic: 16 boundary cases PASS; four SEC New York date
  examples PASS. These are design checks, not new production fixture coverage.
- No product code, runtime configuration, provider request or activation changed.
- Operator reply on 2026-10-30: design approved; prepare the implementation plan.
- Preserve the original reviewed design in commit 6aac9eb38ac6e6e8d1370384936dbba11a8572de.
