# Harvester Native Schedule and Mac Assist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans for the preserved Native method, or superpowers:subagent-driven-development only if the operator changes that method. Implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inactive-by-default, shared daily-slot admission and an optional Mac sender without duplicating collection, bypassing Stage3 handoffs, or changing financial analysis policy.

**Architecture:** GitHub remains the collector and native cron remains enabled. Native, optional Mac and explicit manual daily requests share a durable admission receipt on a separate GitHub branch. Stage3 remains a distinct collection path; a future server can replace the sender without replacing the collector or resetting receipts.

**Tech Stack:** Existing Python 3.11, standard library including zoneinfo/dataclasses/unittest, existing requests dependency, GitHub Actions/Contents API, and an inert macOS launchd plist example. No new package or timer service.

**Spec:** `docs/superpowers/specs/2026-10-30-harvester-hybrid-schedule-design.md`, approved for implementation planning on 2026-10-30. Original reviewed design: commit `6aac9eb38ac6e6e8d1370384936dbba11a8572de`.

## Authorization and restart checkpoint

- Planning-time authority: write/review this plan only.
- Implementation approval received: Native offline implementation, verification, independent review, PR/CI and merge. Resumed 2026-11-02. Installation, live GitHub receipt writes, dispatch, provider collection and activation still require separate operational approval.
- Baseline product code: `536205c29e2961622182172443730925a9967d26`.
- Workspace: `/Users/givet-bsm/US_Alpha_Seeker_Harvester/.worktrees/harvester-drive-failure-truth`; branch `codex/harvester-hybrid-schedule-design`.
- The reported network interruption occurred before this plan was saved. Verified restart state: clean worktree at the design commit; no implementation or dispatch attempt to repeat. The network's physical cause is unverified.
- A subsequent credit-limit restart found only this draft and its design-approval update uncommitted. Resume their self-review; do not restart any external work.
- Keep expected changes separate from any newly discovered user changes. Recheck status before implementing; stop on unexpected changes.

## Global Constraints

- First `13 22 * * 1-5`, second `13 3 * * 2-6`: Tuesday-Saturday 07:13/12:13 Asia/Seoul; do not move these slots.
- Start window is `[scheduledFor, scheduledFor + 3 hours)` (10800 seconds); retain existing 360-minute collector-job timeout.
- Mode defaults to `legacy`; new assist events are rejected in legacy. No install, automatic activation, source request or runtime proof in offline development.
- Hybrid mode accepts only main, attempt 1, explicit canonical first/second slots. No auto/all or manual bypass.
- Slot identity is repository + KST date + batch; host, event, run, head, deadline and version do not change identity.
- Receipt branch is `harvester-schedule-receipts`; missing control/branch or uncertain state blocks, never recreates or retries.
- One automatically admitted collector attempt per slot is the objective, NOT exactly-once successful external side effects or a punctuality SLA.
- Stage3 exact trigger-file/hash checks and priority remain. Assist is never Stage3 and never preempts it.
- SEC window remains actual America/New_York date immediately before its phase; never substitute the KST slot date or invent publication time.
- Only Harvester code changes. Preserve Stage0-7 policy, analysis repo, execution repo, Toss source/IP/clock restrictions and existing source budgets.
- No provider/broker/Telegram/Notion/Drive calls in tests; no secret or identifier output. No runtime artifact or state commit.
- No new dependency, trading ledger or general scheduler framework. The only new operational persistence proposed is the approved safe scheduling receipt.
- Safe execution defaults remain READ_ONLY=true, EXEC_ENABLED=false and MARKET_GUARD_MODE=observe; no order-related code is introduced.

## Review Focus

1. Raw GitHub action/ref/body disagreement: reject before provider authentication (Tasks 1 and 3).
2. A reservation exists but creation response/result is lost: never replay the collector (Task 2).
3. Queueing/dependency setup consumes the last valid start second: recheck before import-time Drive authentication, not only before scheduling the job (Task 3).
4. Sleep/login, local clock step, secret-store unavailability or another local sender process: no fabricated due slot, no second POST (Task 4).
5. Local summary/job success masks partial collection or Notion failure: store separate observations, never certify full source coverage from process success (Tasks 2 and 3).

## File and dependency map

Create:
- `scripts/harvester_schedule_contract.py`: pure routing, typed slot/control/receipt validation, canonical hashes and time predicates.
- `scripts/harvester_schedule_receipts.py`: fixed-scope GitHub transport and bounded admission/result protocol; imports the pure contract only.
- `scripts/run_harvester_schedule_gate.py`: Actions CLI boundary for resolve/reserve/recheck/finish; no Harvester import.
- `scripts/dispatch_harvester_daily_slot.py`: Mac/server sender; defaults to validation-only and shares the contract/transport.
- `scripts/check_harvester_schedule_admission.py`: pure and receipt fake-transport regression tests.
- `scripts/check_harvester_schedule_integration.py`: event/workflow/AST-isolated collector and SEC compatibility tests.
- `scripts/check_harvester_mac_assist.py`: sender, local file custody, redaction and plist tests.
- `docs/ops/harvester-schedule-mac-assist.launchagent.plist.example`: inert template, not installed or activated.
- `docs/ops/harvester-hybrid-schedule-runbook.md`: activation/rollback/server-handover and separately bounded proof checklist.

Modify:
- `.github/workflows/main.yml`: event routing, inactive mode, admission/result jobs and collector guards.
- `harvester.py`: precise Stage3-versus-daily routing and pre-authentication hybrid guard, not collection formulas.
- `scripts/check_harvester_schedule_slot_contract.py`: keep cron assertions; replace old broad-dispatch assumptions with routing invariants.
- `.github/workflows/telegram-routing-ci.yml`: register the three new offline checks.

Reuse `scripts/official_shadow_runtime.py:canonical_sha256` without importing credential-loading Harvester. Existing analysis sender is a behavioral reference only; do not edit or invoke it. No changes to SEC producer algorithms or request counts.

## Shared interfaces and wire contract

Task 1 defines frozen dataclasses `Slot`, `Route`, `Control`, `AdmissionProof`, and `GateResult`. All timestamps are aware UTC datetime internally and canonical UTC Z strings on the wire. Counts cannot be bools; unknown fields in incoming control/request/receipts are rejected.

- `Slot`: repository, slot_date, batch, scheduled_for, start_before, slot_sha256, basis.
- `Route`: mode (`legacy|hybrid`), kind (`legacy_daily|stage3|hybrid_daily|blocked`), slot (optional), status, event_name, event_action and schedule_expression. Preserve these validated provenance fields so the legacy SEC predicate can distinguish native second cron from manual second batch. Blocked never means legacy fallback.
- `Control`: schemaVersion=`harvester-schedule-control.v1`, repository, receiptBranch, activationAt, timezone=`Asia/Seoul`, startWindowSeconds=10800, pathVersion=1. Exact byte SHA-256 must match trusted workflow/local configuration; reject old/pre-activation slots.
- `AdmissionProof`: schemaVersion=`harvester-schedule-proof.v1`, slot, controlSha256, runId, headSha, runAttempt=1, attemptSha256, attemptPath. This is an internal routing proof, not a source/analysis artifact.
- `GateResult`: status, may_collect, proof (optional), counts of GET/PUT/POST; serialized output is allowlisted. Only verified admission has may_collect=true.
- `harvester_daily_slot_requested` payload: schemaVersion=`harvester-daily-slot-request.v1`, scheduledFor, batch. Repository, branch, deadlines, URL and receipt paths are receiver-owned, not client overrides.
- Attempt body: schemaVersion=`harvester-schedule-attempt.v1`, status=`RESERVED_DO_NOT_RETRY`, exact Slot fields, controlSha256, runId, headSha, runAttempt, reservedAt. Immutable on creation.
- Result body: schemaVersion=`harvester-schedule-result.v1`, attemptSha256, runId, headSha, observedAt, collectorExit (`success|failure|skipped|unknown`), workflowJobResult, secPhase (`success|failure|skipped|unknown`), optional aggregateSummarySha256, sourceCoverageVerified=false. Never copy raw collector summary contents.
- Hash basis: slotSha256 is canonical SHA-256 of `{repository, slotDate, batch}` only. Control and receipt evidence hashes cover exact UTF-8 bytes. JSON byte serialization uses sorted keys/ASCII/stable separators; per-attempt timestamps do not enter slot identity. Do not mistake Git blob SHA for evidence SHA-256.

## Task 1: Pure slot and route contract

**Files:** Create contract and admission test modules; modify the old schedule-slot static test only when the workflow changes in Task 3.

**Interfaces:**
- `resolve_route(event_name: str, event: Mapping[str, Any], ref: str, run_attempt: int, created_at: datetime, now: datetime, mode: str, repository: str) -> Route`
- `due_slot(now: datetime, repository: str) -> Slot | None`
- `validate_control(raw: bytes, expected_sha256: str, repository: str) -> Control`
- `slot_is_open(slot: Slot, now: datetime) -> bool`
- `is_stage3_event(event_name: str, event: Mapping[str, Any]) -> bool`
- `validate_proof(raw: bytes, control: Control, run_id: str, head_sha: str, now: datetime) -> AdmissionProof`
- `sec_phase_allowed(route: Route, collector_succeeded: bool, enabled: bool) -> bool`

- [x] **Write failing tests** in `check_harvester_schedule_admission.py`: `test_first_second_utc_mapping`, `test_half_open_window`, `test_native_wake_basis`, `test_assist_main_attempt_and_payload`, `test_manual_no_bypass`, `test_legacy_defaults`, `test_slot_hash_host_invariance`, `test_control_and_proof_schema`.
  Assert first on 2026-10-30 KST is 2026-10-29T22:13:00Z; second is 2026-10-30T03:13:00Z. Assert 08:00 KST first is open, 10:13 is expired, 12:13 second is open and 15:13 is expired. Reject Sunday/Monday due slots, future times, non-main, attempt 2, auto/all in hybrid and unknown dispatch action.
- [x] **Run RED:** `python3 scripts/check_harvester_schedule_admission.py`; expect failure because the contract module/interface does not exist, not an unrelated dependency error.
- [x] **Implement pure functions/types.** Native schedule selection uses the most recent exact cron occurrence at trusted workflow created_at; current now still gates admission. Keep basis=`RECEIVER_SELECTED_DUE_SLOT`. Mac/manual must specify the exact canonical occurrence. Exclude naive/non-finite/overflow timestamps, path traversal, payload-declared repo/ref and unknown modes. Legacy native/manual behavior stays unchanged; new assist blocked even if its payload asks for hybrid.
- [x] **Add compatibility tests:** `test_stage3_does_not_claim_daily_slot` and `test_sec_gate_matrix`; Stage3 is only exact action `stage3_completed`, assists never inherit it, and SEC requires successful allowed second batch plus the existing enabled setting. Legacy predicate is unchanged.
- [x] **Run GREEN:** admission check, then current schedule-slot check. All cases must pass with a trap that rejects any real network access. No import of `harvester.py`.
- [x] **Commit task:** `feat: define canonical Harvester daily slots` (only the two new files).

## Task 2: Durable receipts and bounded GitHub transport

**Files:** Create receipts module; extend admission tests.

**Interfaces:**
- `GitHubTransport.request(method: str, path: str, body: Mapping[str, Any] | None = None) -> HttpResult`; `HttpResult` contains status, headers, body_bytes. Inject transport in every test.
- `read_run_metadata(transport: GitHubTransport, repository: str, run_id: str, head_sha: str) -> Mapping[str, Any]`
- `reserve_slot(slot: Slot, expected_control_sha256: str, run_id: str, head_sha: str, now: Callable[[], datetime], transport: GitHubTransport) -> GateResult`
- `recheck_admission(proof: AdmissionProof, now: Callable[[], datetime], transport: GitHubTransport) -> GateResult`
- `finish_slot(proof: AdmissionProof, outcome: Mapping[str, Any], now: Callable[[], datetime], transport: GitHubTransport) -> GateResult`

- [x] **Write RED cases:** `test_existing_any_receipt_blocks`, `test_branch_control_missing_blocks`, `test_control_digest_mismatch`, `test_create_201_readback_required`, `test_create_timeout_no_replay`, `test_409_422_no_retry`, `test_404_only_after_control_verified`, `test_redirect_denied`, `test_different_run_cannot_adopt_reservation`, `test_result_is_not_source_pass`, `test_concurrent_requests_use_one_attempt`.
  Fake two admitted wake signals under a shared serial test lock: confirmed create/readback count=1, collector admission count<=1. Rejected/unknown create causes zero admissions. Pre-existing corrupt bytes block as well as valid JSON. Never update an existing attempt with a SHA.
- [x] **Run RED:** admission check; verify failures exercise the missing receipt behavior.
- [x] **Implement transport.** Use existing requests with redirects disabled, retry disabled, explicit 15-second timeout, fixed `https://api.github.com` and an endpoint allowlist. Bound JSON response body to 64 KiB and reject truncation/invalid content. Never log token, exception text, request body or response body. Run metadata has its own adequate bounded size (1 MiB), same no-log/no-pagination rule. No credential retrieval at module import.
- [x] **Implement receipt protocol.** Exact control GET, exact attempt GET, create-only PUT (require 201), exact attempt read-back and byte/run/head/hash parity. Missing branch/control is never repaired. All uncertain writes stop; read-back must not turn a lost create response into admission. Result create uses the same create-only convention, verifies original ownership and cannot overwrite or reopen an attempt.
- [x] **Pin request maxima in tests:** resolve run metadata GET<=1; reserve GET<=3/PUT<=1; pre-collector recheck GET<=2; finish GET<=4/PUT<=1. Whole receiver path <=12 GitHub requests, zero retries/pages. Failures consume the attempt where one exists. Provider requests are NOT included or newly authorized by these metadata budgets.
- [x] **Run GREEN** plus redaction tests containing fake bearer tokens, private filenames and arbitrary provider error text; none may reach stdout. Only allowlisted safe statuses/hashes/counts are output.
- [x] **Commit task:** `feat: add fail-closed Harvester slot receipts`.

## Task 3: Wire workflow routing and preserve Stage3/SEC behavior

**Files:** Create gate CLI and integration check; modify main.yml, harvester.py, the pure contract module and existing schedule-slot check.

**Interfaces:**
- Gate CLI subcommands `resolve`, `reserve`, `recheck`, `finish`; arguments name pinned event/input/proof files and expected hashes, never credentials. Token is step-scoped environment only. Expose safe scalar job outputs plus serialized proof through files/env, never shell interpolation of dispatch payload.
- `assert_hybrid_start(route: Route, proof: AdmissionProof | None, now: datetime) -> None` in the pure contract, called immediately before existing `drive_service = get_drive_service()` and again on entry to `run_harvester()` when hybrid daily. In legacy/Stage3 preserve current semantics; hybrid requires exact proof and open window.

- [x] **Write RED workflow/AST tests:** `test_assist_is_daily_not_stage3`, `test_assist_does_not_cancel`, `test_only_stage3_preempts`, `test_dedupe_before_import_auth`, `test_window_expires_during_install`, `test_job_dependency_skip_logic`, `test_job_failure_not_covered_by_tee`, `test_mode_cannot_come_from_event`, `test_secondary_jobs_cannot_replay`.
  AST-isolate guards/routing with fake Drive auth and fake collection; proof/window rejected before authentication must leave both counters zero. Also test expiration during authentication: preserve authAttempted=true but collectionCalls=0, never claim the earlier authentication did not happen. Every direct broad repository_dispatch daily/Stage3 branch is reviewed, not replaced blindly. Retain existing manual-only official SHADOW one-shot gate.
- [x] **Run RED:** integration check; record the current broad-dispatch and missing pre-authentication gate failures without importing real Harvester.
- [x] **Implement minimal CLI/job graph.** Add workflow-local `HARVESTER_SCHEDULE_MODE: legacy` and an empty control-hash pin, not a repository execution variable. Add `scheduled_for` manual input (optional for legacy, required for hybrid). Accept new event only through routing; `cancel-in-progress` restricts to exact `stage3_completed`.
  Use `route` (contents/actions read), `reserve` (conditional hybrid daily, contents write), existing `build` collector (contents read), and `finish` (conditional admitted hybrid, contents write). Keep existing workflow-level concurrency across the entire graph. Existing collector job remains 360 minutes; new metadata jobs have bounded timeout (5 minutes each). Explicit `needs`/`if` clauses let legacy and Stage3 bypass a skipped reserve, but never bypass failed resolve/reserve. A canceled run need not finish; its reservation stays blocking.
  Route obtains exact current-run metadata once in hybrid mode and validates head, attempt, branch and event; no run list scan. Pass created_at as evidence, never fabricate it from job time. Metadata is unnecessary for unchanged legacy behavior. `GITHUB_EVENT_PATH` supplies action/inputs; no user-provided shell code.
- [x] **Wire collector start.** After dependency/tests, recheck control/attempt on the server, then create the local validated proof used by the pre-authentication guard. Expiration or changed control at that gate rejects before Harvester imports can authenticate. Do not begin provider/Drive/Notion/Telegram steps after rejection; if the second time check expires during an already-authorized authentication, preserve that observed call and block collection instead of reporting zero total calls. Existing collect/error notifications remain for legitimately admitted/legacy runs. Do not spoof GITHUB_EVENT_NAME to make assist look like schedule; record real event plus normalized route.
- [x] **Wire SEC and result semantics.** Legacy predicate unchanged. Hybrid native/assist/manual successful second batch uses existing enabled flag, authorization, budgets and sentinel. Preserve actual NY date immediately before SEC and freeze it. Finish reads safe job outputs, not raw logs: process success is not proof of zero source gaps; failures in SEC/Notion do not disappear into collector success. Store separate result observations and sourceCoverageVerified=false. No second result write retry.
- [x] **Add/run GREEN SEC tests:** `test_sec_native_assist_parity`, `test_first_stage3_rejection`, `test_ny_midnight_and_dst_window`, `test_sec_cross_midnight_no_second_request`, `test_stage0_window_contract_compatibility`. Reuse producer recurring_collection_key; read the pinned analysis consumer contract for compatibility without editing that repo or inventing a new current-window requirement. Test Oct29 03:40Z=>Oct28 and 11:40Z=>Oct29, Nov3 04:40Z=>Nov2 and 05:10Z=>Nov3. Keep publication times unchanged.
- [x] **Run all three relevant checks:** admission, integration, existing schedule-slot. Existing Drive upload test must pass; shell remains bash/pipefail. Legacy source/request/receipt side effects remain unchanged, hybrid state writes are all mocked.
- [x] **Commit task:** `feat: connect shared daily admission without activating Mac assist`.

## Task 4: Optional Mac sender and inert launchd configuration

**Files:** Create sender, Mac fixture and plist example; reuse Tasks 1-2, no other repo modification.

**Interfaces:**
- `dispatch_due_slot(config: Mapping[str, Any], send: bool, now: Callable[[], datetime], transport: GitHubTransport, receipt_dir: Path) -> GateResult`
- `reserve_local_attempt(receipt_dir: Path, slot: Slot, payload_sha256: str) -> bool`
- CLI `--config <owner-only-json> --receipt-dir <owner-only-dir>` is validation-only; `--send` is the explicit operational switch. No credentials or direct collector command in arguments/config/plist. Validated config contains repository, expected control hash and timezone; endpoint and receipt branch are fixed by code.

- [x] **Write RED cases:** `test_default_zero_network`, `test_mac_due_at_0800`, `test_late_wake_no_backfill`, `test_sleep_and_login_converge`, `test_offline_no_post`, `test_two_processes_one_local_attempt`, `test_unknown_post_no_retry`, `test_timezone_clock_rejected`, `test_symlink_receipt_dir_rejected`, `test_missing_secret_before_network`, `test_plist_inert_no_keepalive`, `test_server_host_change_preserves_slot_key`.
  Fake 08:00 KST yields one first-slot request; 10:13 yields none; a repeated calendar/login callback cannot POST again. Clock/timezone/config failure yields no POST and native cron is untouched. Existing terminal remote receipt causes no local dispatch attempt. Fake body/token never appears in output.
- [x] **Run RED:** `python3 scripts/check_harvester_mac_assist.py`; expect missing sender behavior, no credential read.
- [x] **Implement validation and send.** Credential comes only from explicit runtime environment, missing before any request; no .env read, default process-secret dump or keychain access. Verify absolute real owner-only directory, reject symlinks and pre-existing attempt paths. Use exclusive create, restrictive permissions, file/directory fsync. Recheck window after reservation. One dispatch POST<=1, fixed event, no redirects/retry; timeout preserves an unknown attempt. Successful 204 means dispatch accepted, NOT collection proof.
  Sender reads pinned control and exact attempt path once each, GET<=2/POST<=1 per due invocation. Use authenticated HTTPS response Date only to check local dispatch clock, not as market-data timestamp: require date seconds fall between local request-start/end rounded to HTTP whole-second precision; reject missing/invalid/cache-aged Date and local wall clock reversal. This is a bounded comparison, NOT NTP synchronization proof or clock compensation. No time-sync service, clock mutation or source timestamp fallback is added.
  Resolve transport paths before sending; use the shared bounded transport. Its Mac endpoint allowlist is control/attempt GET plus the fixed repository_dispatch POST only. GitHub requires Contents-write permission for this dispatch API; do not falsely claim the credential has no wider technical capability. Limit repository scope and never use it for receipt PUT.
- [x] **Create inert plist example.** Explicit Tuesday-Saturday 07:13 and 12:13 entries, RunAtLoad=true, no KeepAlive/poll loop, no credentials, no shell command interpolation, no --send in the committed example. Absolute executable/config/receipt paths are placeholders resolved only in separately approved installation. Verify existing interpreter/dependencies before rollout; do not auto-install them. Do not overwrite Toss's LaunchAgent or adjust power settings.
- [x] **Run GREEN:** Mac check plus admission/integration checks. Validate plist with Python plistlib; real launchctl/bootstrap/send is forbidden. Fixture sender requests are recorded fake calls only.
- [x] **Commit task:** `feat: add inactive Mac assist sender and offline fixtures`.

## Task 5: CI, runbook and safe handoff

**Files:** Modify telegram-routing-ci.yml; create runbook; update test registration assertions where needed.

**Interfaces:** CI runs the three new `scripts/check_*` entrypoints. Runbook uses the existing CLI names, not a new installer or automation framework.

- [ ] **Write RED registration/runbook checks** in the integration test: all new offline checks included, no collecting workflow triggered by PR, CI retains contents:read, mode remains legacy, no launchd installation/--send activation, receipt branch never merges into main. Include pending-queue cancellation scenarios as availability failures, not duplicate permission.
- [ ] **Run RED** integration check; expect missing registration/runbook assertions.
- [ ] **Implement registration/runbook.** No new dependencies and no audit output from real providers. Document gate-only rollback, old-job drain, future KST activation boundary, control byte/hash pin, branch custody/no-delete/no-force rules, manual auto/all restriction, safe public fields, token installation as a separate approval, no source collection from before the boundary, and unexpected-existing-receipt response.
  Document Mac-off/awake checks and one future natural evaluation without automatic reruns. Distinguish proposed later host migration (disable/drain old sender, preserve both local and server attempts, validate new host) from today's implementation. Do not reset keys during migration. Receiver read/write budgets are operational metadata, not permission to increase provider budget.
- [ ] **Run GREEN and full offline validation** below with network denied in fake-transport tests. No passing flag may stand in for recorded failures or missing coverage.
- [ ] **Commit task:** `test: gate hybrid scheduling and document rollout boundaries`.
- [ ] **Whole-branch independent review (Native method).** Fresh reviewer checks actual final diff against spec/plan, especially import-time auth, concurrency/claim ordering, permission scope, partial failures, manual overlap and SEC midnight. Fix actual findings and rerun affected plus safety tests. A stopped reviewer is not a completed review.
- [ ] **Only after plan implementation approval and all tests/review pass:** non-force push, one Harvester PR, required CI PASS, merge; update local main by non-force fast-forward if clean. PR states docs/schema/source/permission effects, mode=legacy, activation=false, no execution flags/broker changes, no real state writes and rollback boundaries. No forced collection or workflow rerun to manufacture runtime proof.

## Full offline verification commands

Run from the isolated Harvester worktree, using an existing Python 3.11+ environment with current project dependencies. The three new checks install nothing and reject unexpected network access. Inspect existing checks' output paths first; any generated local reports stay uncommitted. Never load credential-bearing Harvester normally.

```sh
python3 scripts/check_harvester_schedule_admission.py
python3 scripts/check_harvester_schedule_integration.py
python3 scripts/check_harvester_mac_assist.py
python3 scripts/check_harvester_schedule_slot_contract.py
python3 scripts/check_drive_upload_contract.py
python3 scripts/check_stage0_financial_publication_lineage_producer.py
python3 scripts/check_financial_publication_lineage_bulk_probe.py
python3 scripts/check_telegram_routing_policy.py
python3 scripts/check_symbol_agnostic_runtime.py
python3 scripts/check_toss_same_stage3_handoff.py
python3 -m py_compile harvester.py scripts/*.py
git diff --check
```

Additionally run every other existing check registered in telegram-routing-ci.yml
(target/listing/corporate-action/earnings/SEC/FINRA/Macro/BLS/Toss), after verifying
it is fixture-only. Do not replace missing packages with real API calls. CI must
also pass, but success does not authorize mode change or live requests.

## Self-review and final completion gates

Spec coverage: sections 1-4 -> Task 1/3/4; 5 -> Task 2/3; 6 -> Task 4; 7 -> Task 3;
8 -> Task 3/5; 9 -> all tests; 10 -> authorization/rollout exclusions. All five
Review Focus inputs have named tests. Interface signatures and schema names are
defined here once and reused. The proposed job graph adds queue overhead that
must be measured later; it is not presented as a latency improvement already made.

- Implementation may finish as STATIC_READY_NOT_ACTIVATED only after tests,
  independent review, PR/CI and merge. None of those implementation steps are
  completed by the existence of this plan.
- Shared receipt branch creation/writes, credential provisioning, Mac loading,
  hybrid mode activation, real dispatch and natural proof are NOT in plan-only or
  offline implementation authority. Do not perform any of them without the
  separately reviewed operational approval package.
- Parent PAPER operational binding remains independently blocked on current-cutoff
  cash-flow completeness. This scheduling work neither supplies that evidence nor
  approves orders; no estimated completion date or fresh PAPER readiness is claimed.

## Platform references (planning only)

- https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run
- https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
- https://docs.github.com/en/rest/repos/repos#create-a-repository-dispatch-event
- https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#permissions
- Sleep/calendar behavior and schedule-delay references remain in the approved spec.

No authenticated API, broker/provider request, workflow dispatch, install or
operational-state mutation was performed to write this plan.
