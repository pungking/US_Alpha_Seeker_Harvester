"""Pure daily admission contract; importing this module never authenticates."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

try:
    from .official_shadow_runtime import canonical_sha256
except ImportError:
    from official_shadow_runtime import canonical_sha256

UTC = dt.timezone.utc
KST = ZoneInfo("Asia/Seoul")
CRONS = {"first": "13 22 * * 1-5", "second": "13 3 * * 2-6"}
HOURS = {"first": 7, "second": 12}
RECEIPT_BRANCH = "harvester-schedule-receipts"
ASSIST_ACTION = "harvester_daily_slot_requested"
REQUEST_SCHEMA = "harvester-daily-slot-request.v1"
WINDOW = 10800


class ContractError(ValueError):
    """Only constant, non-sensitive reason codes may cross the CLI boundary."""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ContractError(code)


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            require(key not in out, "DUPLICATE_JSON_KEY")
            out[key] = value
        return out
    try:
        value = json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ContractError("INVALID_JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ContractError("INVALID_JSON") from exc
    require(isinstance(value, dict), "INVALID_JSON_OBJECT")
    return value


def exact(value: Mapping[str, Any], keys: set[str]) -> None:
    require(isinstance(value, dict) and set(value) == keys, "INVALID_SCHEMA")


def parse_time(value: Any) -> dt.datetime:
    require(isinstance(value, str) and len(value) <= 40, "INVALID_TIME")
    try:
        result = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.utcoffset() is not None, "TIMEZONE_REQUIRED")
        return result.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ContractError("INVALID_TIME") from exc


def stamp(value: dt.datetime) -> str:
    require(isinstance(value, dt.datetime) and value.utcoffset() is not None, "TIMEZONE_REQUIRED")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def valid_hash(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def valid_repo(repository: str) -> None:
    require(isinstance(repository, str) and re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository) is not None
            and repository.split("/")[-1] not in {".", ".."}, "INVALID_REPOSITORY")


@dataclass(frozen=True)
class Slot:
    repository: str
    slot_date: str
    batch: str
    scheduled_for: dt.datetime
    start_before: dt.datetime
    slot_sha256: str
    basis: str

    @property
    def attempt_path(self) -> str:
        return f"slots/{self.slot_date}/{self.batch}/attempt.json"

    def wire(self) -> dict[str, Any]:
        return {"repository": self.repository, "slotDate": self.slot_date, "batch": self.batch,
                "scheduledFor": stamp(self.scheduled_for), "startBefore": stamp(self.start_before),
                "slotSha256": self.slot_sha256, "basis": self.basis}


def make_slot(repository: str, date: dt.date, batch: str, basis: str) -> Slot:
    valid_repo(repository)
    require(batch in HOURS and date.weekday() in range(1, 6), "INVALID_SLOT")
    require(basis in {"EXPLICIT_CANONICAL_SLOT", "RECEIVER_SELECTED_DUE_SLOT"}, "INVALID_SLOT_BASIS")
    start = dt.datetime.combine(date, dt.time(HOURS[batch], 13), KST).astimezone(UTC)
    key = {"repository": repository, "slotDate": date.isoformat(), "batch": batch}
    return Slot(repository, date.isoformat(), batch, start, start+dt.timedelta(seconds=WINDOW), canonical_sha256(key), basis)


def slot_is_open(slot: Slot, now: dt.datetime) -> bool:
    stamp(now)
    return slot.scheduled_for <= now < slot.start_before


def due_slot(now: dt.datetime, repository: str) -> Slot | None:
    stamp(now)
    date = now.astimezone(KST).date()
    if date.weekday() not in range(1, 6):
        return None
    for batch in HOURS:
        slot = make_slot(repository, date, batch, "EXPLICIT_CANONICAL_SLOT")
        if slot_is_open(slot, now):
            return slot
    return None


@dataclass(frozen=True)
class Route:
    mode: str
    kind: str
    slot: Slot | None
    status: str
    event_name: str
    event_action: str
    schedule_expression: str


def is_stage3_event(event_name: str, event: Mapping[str, Any]) -> bool:
    return event_name == "repository_dispatch" and event.get("action") == "stage3_completed"


def resolve_route(event_name: str, event: Mapping[str, Any], ref: str, run_attempt: int,
                  created_at: dt.datetime, now: dt.datetime, mode: str, repository: str) -> Route:
    require(mode in {"legacy", "hybrid"} and isinstance(event, dict), "INVALID_ROUTE")
    action = event.get("action", "")
    expression = event.get("schedule", "")
    require(isinstance(action, str) and isinstance(expression, str), "INVALID_ROUTE")
    if is_stage3_event(event_name, event):
        return Route(mode, "stage3", None, "STAGE3_HANDOFF", event_name, action, expression)
    require(event_name in {"schedule", "workflow_dispatch", "repository_dispatch"}, "INVALID_EVENT")
    if event_name == "repository_dispatch":
        require(action == ASSIST_ACTION and mode == "hybrid", "ASSIST_NOT_ENABLED")
    if mode == "legacy":
        return Route(mode, "legacy_daily", None, "LEGACY_UNCHANGED", event_name, action, expression)
    require(ref == "refs/heads/main" and type(run_attempt) is int and run_attempt == 1, "MAIN_FIRST_ATTEMPT_REQUIRED")
    stamp(created_at); stamp(now)
    require(created_at <= now, "FUTURE_RUN")
    if event_name == "schedule":
        require(expression in CRONS.values(), "INVALID_CRON")
        batch = next(key for key, value in CRONS.items() if value == expression)
        date = created_at.astimezone(KST).date()
        candidates = [make_slot(repository, date-dt.timedelta(days=i), batch, "RECEIVER_SELECTED_DUE_SLOT")
                      for i in range(8) if (date-dt.timedelta(days=i)).weekday() in range(1, 6)]
        slot = next((s for s in candidates if s.scheduled_for <= created_at), None)
        require(slot is not None, "NO_DUE_SLOT")
    else:
        if event_name == "repository_dispatch":
            payload = event.get("client_payload", {})
            exact(payload, {"schemaVersion", "scheduledFor", "batch"})
            require(payload["schemaVersion"] == REQUEST_SCHEMA, "INVALID_SCHEMA")
            timestamp, batch = payload["scheduledFor"], payload["batch"]
        else:
            payload = event.get("inputs", {})
            exact(payload, {"batch_mode", "scheduled_for"})
            timestamp, batch = payload["scheduled_for"], payload["batch_mode"]
        start = parse_time(timestamp)
        slot = make_slot(repository, start.astimezone(KST).date(), batch, "EXPLICIT_CANONICAL_SLOT")
        require(stamp(start) == timestamp and start == slot.scheduled_for, "NONCANONICAL_SLOT")
    require(slot_is_open(slot, now), "SLOT_NOT_OPEN")
    return Route(mode, "hybrid_daily", slot, "ADMISSION_REQUIRED", event_name, action, expression)


@dataclass(frozen=True)
class Control:
    repository: str
    activation_at: dt.datetime
    sha256: str


def validate_control(raw: bytes, expected_sha256: str, repository: str) -> Control:
    require(valid_hash(expected_sha256) and digest(raw) == expected_sha256, "CONTROL_HASH_MISMATCH")
    value = decode(raw)
    exact(value, {"schemaVersion", "repository", "receiptBranch", "activationAt", "timezone", "startWindowSeconds", "pathVersion"})
    valid_repo(repository)
    require(value["schemaVersion"] == "harvester-schedule-control.v1" and value["repository"] == repository
            and value["receiptBranch"] == RECEIPT_BRANCH and value["timezone"] == "Asia/Seoul"
            and type(value["startWindowSeconds"]) is int and value["startWindowSeconds"] == WINDOW
            and type(value["pathVersion"]) is int and value["pathVersion"] == 1, "INVALID_CONTROL")
    return Control(repository, parse_time(value["activationAt"]), expected_sha256)


def slot_from_wire(value: dict[str, Any], repository: str) -> Slot:
    exact(value, {"repository", "slotDate", "batch", "scheduledFor", "startBefore", "slotSha256", "basis"})
    start = parse_time(value["scheduledFor"])
    slot = make_slot(repository, start.astimezone(KST).date(), value["batch"], value["basis"])
    require(slot.wire() == value, "SLOT_PARITY_MISMATCH")
    return slot


@dataclass(frozen=True)
class AdmissionProof:
    slot: Slot
    control_sha256: str
    run_id: str
    head_sha: str
    attempt_sha256: str

    def wire(self) -> dict[str, Any]:
        return {"schemaVersion": "harvester-schedule-proof.v1", "slot": self.slot.wire(),
                "controlSha256": self.control_sha256, "runId": self.run_id, "headSha": self.head_sha,
                "runAttempt": 1, "attemptSha256": self.attempt_sha256, "attemptPath": self.slot.attempt_path}


def validate_proof(raw: bytes, control: Control, run_id: str, head_sha: str, now: dt.datetime,
                   *, allow_expired: bool = False) -> AdmissionProof:
    value = decode(raw)
    exact(value, {"schemaVersion", "slot", "controlSha256", "runId", "headSha", "runAttempt", "attemptSha256", "attemptPath"})
    slot = slot_from_wire(value["slot"], control.repository)
    require(value["schemaVersion"] == "harvester-schedule-proof.v1" and value["controlSha256"] == control.sha256
            and value["runId"] == run_id and isinstance(run_id, str) and re.fullmatch(r"[0-9]+", run_id) is not None
            and value["headSha"] == head_sha and valid_hash(head_sha, 40)
            and type(value["runAttempt"]) is int and value["runAttempt"] == 1
            and valid_hash(value["attemptSha256"]) and value["attemptPath"] == slot.attempt_path,
            "PROOF_IDENTITY_MISMATCH")
    require(slot.scheduled_for >= control.activation_at and
            (slot.scheduled_for <= now if allow_expired else slot_is_open(slot, now)), "PROOF_WINDOW_INVALID")
    return AdmissionProof(slot, control.sha256, run_id, head_sha, value["attemptSha256"])


def sec_phase_allowed(route: Route, collector_succeeded: bool, enabled: bool) -> bool:
    if not enabled or not collector_succeeded:
        return False
    if route.kind == "hybrid_daily":
        return route.slot is not None and route.slot.batch == "second"
    return route.kind == "legacy_daily" and route.event_name == "schedule" and route.schedule_expression == CRONS["second"]


@dataclass(frozen=True)
class GateResult:
    status: str
    may_collect: bool = False
    proof: AdmissionProof | None = None

    def wire(self) -> dict[str, Any]:
        return {"status": self.status, "mayCollect": self.may_collect,
                "proof": self.proof.wire() if self.proof else None}


def read_json_file(path: str, *, private: bool = False) -> dict[str, Any]:
    require(isinstance(path, str) and bool(path), "INPUT_FILE_REQUIRED")
    try:
        target = Path(path)
        info = target.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 1024*1024, "INPUT_FILE_INVALID")
        if private:
            require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, "PRIVATE_INPUT_REQUIRED")
        return decode(target.read_bytes())
    except OSError as exc:
        raise ContractError("INPUT_FILE_UNAVAILABLE") from exc


def environment_event(env: Mapping[str, str]) -> dict[str, Any]:
    if env.get("GITHUB_EVENT_PATH"):
        return read_json_file(env["GITHUB_EVENT_PATH"])
    require(env.get("GITHUB_EVENT_NAME", "") != "repository_dispatch", "DISPATCH_EVENT_REQUIRED")
    return {}


def assert_environment_start(env: Mapping[str, str], now: dt.datetime) -> str:
    """Called before Drive auth and again before collection, without network."""
    mode = env.get("HARVESTER_SCHEDULE_MODE", "legacy")
    require(mode in {"legacy", "hybrid"}, "INVALID_MODE")
    event = environment_event(env)
    event_name = env.get("GITHUB_EVENT_NAME", "")
    if is_stage3_event(event_name, event):
        return "stage3"
    if mode == "legacy":
        require(event_name != "repository_dispatch", "ASSIST_NOT_ENABLED")
        return "legacy_daily"
    bundle = read_json_file(env.get("HARVESTER_SCHEDULE_PROOF_PATH", ""), private=True)
    exact(bundle, {"proof", "control"})
    require(isinstance(bundle["control"], str), "INVALID_CONTROL")
    control = validate_control(bundle["control"].encode(), env.get("HARVESTER_SCHEDULE_CONTROL_SHA256", ""),
                               env.get("GITHUB_REPOSITORY", ""))
    proof = validate_proof(encode(bundle["proof"]), control, env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_SHA", ""), now)
    require(env.get("GITHUB_RUN_ATTEMPT") == "1", "MAIN_FIRST_ATTEMPT_REQUIRED")
    route = resolve_route(event_name, event, env.get("GITHUB_REF", ""), 1, now, now, mode, control.repository)
    require(route.slot == proof.slot and env.get("DAILY_BATCH_MODE") == proof.slot.batch, "ROUTE_PROOF_MISMATCH")
    return "hybrid_daily"
