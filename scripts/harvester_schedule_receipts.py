"""Bounded create-only scheduling receipts; no broker/provider dependencies."""
from __future__ import annotations

import base64
import datetime as dt
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

try:
    from . import harvester_schedule_contract as c
except ImportError:
    import harvester_schedule_contract as c


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: Mapping[str, str]
    body_bytes: bytes


class GitHubTransport:
    """Fixed host/paths, no implicit credentials, proxy, retries or redirects."""
    def __init__(self, repository: str, token: str, phase: str) -> None:
        c.valid_repo(repository)
        c.require(isinstance(token, str) and bool(token.strip()), "CREDENTIAL_MISSING")
        limits = {"resolve": {"GET": 1}, "reserve": {"GET": 3, "PUT": 1},
                  "recheck": {"GET": 2}, "finish": {"GET": 4, "PUT": 1},
                  "sender": {"GET": 2, "POST": 1}}
        c.require(phase in limits, "INVALID_PHASE")
        self.repository, self.phase = repository, phase
        self._token, self._limits = token, limits[phase]
        self.counts = {"GET": 0, "PUT": 0, "POST": 0}

    def _validate(self, method: str, path: str, body: Mapping[str, Any] | None) -> None:
        prefix = f"/repos/{self.repository}/"
        c.require(isinstance(path, str) and path.startswith(prefix), "PATH_REJECTED")
        suffix = path[len(prefix):]
        content = r"contents/(control\.json|slots/\d{4}-\d{2}-\d{2}/(first|second)/(attempt|result)\.json)"
        valid = False
        if method == "GET":
            valid = bool(re.fullmatch(content + r"\?ref=" + c.RECEIPT_BRANCH, suffix))
            valid = valid or (self.phase == "resolve" and bool(re.fullmatch(r"actions/runs/[0-9]+", suffix)))
            c.require(body is None, "GET_BODY_REJECTED")
        elif method == "PUT" and self.phase in {"reserve", "finish"}:
            leaf = "attempt" if self.phase == "reserve" else "result"
            valid = bool(re.fullmatch(r"contents/slots/\d{4}-\d{2}-\d{2}/(first|second)/"+leaf+r"\.json", suffix))
            c.exact(body, {"message", "content", "branch"})
            c.require(body["branch"] == c.RECEIPT_BRANCH, "BRANCH_REJECTED")
        elif method == "POST" and self.phase == "sender":
            valid = suffix == "dispatches"
            c.exact(body, {"event_type", "client_payload"})
            c.require(body["event_type"] == c.ASSIST_ACTION, "EVENT_REJECTED")
            c.exact(body["client_payload"], {"schemaVersion", "scheduledFor", "batch"})
        c.require(valid, "PATH_REJECTED")
        c.require(self.counts.get(method, 0) < self._limits.get(method, 0), "REQUEST_BUDGET_EXCEEDED")

    def request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> HttpResult:
        self._validate(method, path, body)
        import requests

        self.counts[method] += 1
        limit = 1024*1024 if "/actions/runs/" in path else 64*1024
        try:
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, "https://api.github.com"+path, json=body,
                                     headers={"Authorization": "Bearer "+self._token, "Accept": "application/vnd.github+json",
                                              "X-GitHub-Api-Version": "2022-11-28"},
                                     timeout=15, allow_redirects=False, stream=True) as response:
                    chunks, size = [], 0
                    for chunk in response.iter_content(8192):
                        size += len(chunk)
                        c.require(size <= limit, "RESPONSE_TOO_LARGE")
                        chunks.append(chunk)
                    return HttpResult(response.status_code, {k.lower(): v for k, v in response.headers.items()}, b"".join(chunks))
        except requests.RequestException as exc:
            raise c.ContractError("TRANSPORT_UNCERTAIN") from exc


def content_path(repository: str, name: str, read: bool = True) -> str:
    return f"/repos/{repository}/contents/{name}" + (f"?ref={c.RECEIPT_BRANCH}" if read else "")


def content_bytes(response: HttpResult) -> bytes:
    c.require(response.status == 200, "RECEIPT_READ_FAILED")
    data = c.decode(response.body_bytes)
    c.require(data.get("type") == "file" and data.get("encoding") == "base64"
              and isinstance(data.get("content"), str) and type(data.get("size")) is int, "INVALID_CONTENT_ENVELOPE")
    try:
        raw = base64.b64decode(data["content"].replace("\n", ""), validate=True)
    except (ValueError, TypeError) as exc:
        raise c.ContractError("INVALID_CONTENT_BYTES") from exc
    c.require(len(raw) == data["size"] and len(raw) <= 32768, "CONTENT_SIZE_MISMATCH")
    return raw


def read_control(transport: GitHubTransport, expected_sha256: str) -> tuple[c.Control, bytes]:
    raw = content_bytes(transport.request("GET", content_path(transport.repository, "control.json")))
    return c.validate_control(raw, expected_sha256, transport.repository), raw


def read_run_metadata(transport: GitHubTransport, repository: str, run_id: str, head_sha: str) -> Mapping[str, Any]:
    c.require(isinstance(run_id, str) and re.fullmatch(r"[0-9]+", run_id) is not None, "INVALID_RUN")
    response = transport.request("GET", f"/repos/{repository}/actions/runs/{run_id}")
    c.require(response.status == 200, "RUN_METADATA_UNAVAILABLE")
    data = c.decode(response.body_bytes)
    c.require(str(data.get("id")) == run_id and data.get("head_sha") == head_sha and c.valid_hash(head_sha, 40)
              and data.get("head_branch") == "main" and type(data.get("run_attempt")) is int
              and data["run_attempt"] == 1 and data.get("repository", {}).get("full_name") == repository,
              "RUN_METADATA_MISMATCH")
    c.parse_time(data.get("created_at"))
    return data


def create_file(transport: GitHubTransport, name: str, raw: bytes) -> None:
    response = transport.request("PUT", content_path(transport.repository, name, False),
                                 {"message": "Record immutable Harvester scheduling receipt", "branch": c.RECEIPT_BRANCH,
                                  "content": base64.b64encode(raw).decode("ascii")})
    c.require(response.status == 201, "CREATE_NOT_CONFIRMED_NO_RETRY")
    saved = content_bytes(transport.request("GET", content_path(transport.repository, name)))
    c.require(saved == raw, "WRITE_READBACK_MISMATCH")


def reserve_slot(slot: c.Slot, expected_control_sha256: str, run_id: str, head_sha: str,
                 now: Callable[[], dt.datetime], transport: GitHubTransport) -> c.GateResult:
    control, _ = read_control(transport, expected_control_sha256)
    candidate = c.AdmissionProof(slot, control.sha256, run_id, head_sha, "0"*64)
    c.validate_proof(c.encode(candidate.wire()), control, run_id, head_sha, now())
    existing = transport.request("GET", content_path(slot.repository, slot.attempt_path))
    c.require(existing.status == 404, "EXISTING_OR_UNCERTAIN_ATTEMPT")
    instant = now()
    c.require(c.slot_is_open(slot, instant), "SLOT_EXPIRED")
    raw = c.encode({"schemaVersion": "harvester-schedule-attempt.v1", "status": "RESERVED_DO_NOT_RETRY",
                    "slot": slot.wire(), "controlSha256": control.sha256, "runId": run_id, "headSha": head_sha,
                    "runAttempt": 1, "reservedAt": c.stamp(instant)})
    create_file(transport, slot.attempt_path, raw)
    proof = c.AdmissionProof(slot, control.sha256, run_id, head_sha, c.digest(raw))
    c.validate_proof(c.encode(proof.wire()), control, run_id, head_sha, now())
    return c.GateResult("ADMITTED", True, proof)


def verify_attempt(proof: c.AdmissionProof, transport: GitHubTransport, instant: dt.datetime,
                   allow_expired: bool = False) -> bytes:
    control, raw_control = read_control(transport, proof.control_sha256)
    c.validate_proof(c.encode(proof.wire()), control, proof.run_id, proof.head_sha, instant, allow_expired=allow_expired)
    raw = content_bytes(transport.request("GET", content_path(proof.slot.repository, proof.slot.attempt_path)))
    c.require(c.digest(raw) == proof.attempt_sha256, "ATTEMPT_HASH_MISMATCH")
    value = c.decode(raw)
    exact_keys = {"schemaVersion", "status", "slot", "controlSha256", "runId", "headSha", "runAttempt", "reservedAt"}
    c.exact(value, exact_keys)
    reserved = c.parse_time(value["reservedAt"])
    c.require(value["schemaVersion"] == "harvester-schedule-attempt.v1" and value["status"] == "RESERVED_DO_NOT_RETRY"
              and value["slot"] == proof.slot.wire() and value["controlSha256"] == proof.control_sha256
              and value["runId"] == proof.run_id and value["headSha"] == proof.head_sha
              and type(value["runAttempt"]) is int and value["runAttempt"] == 1
              and c.slot_is_open(proof.slot, reserved) and reserved <= instant, "ATTEMPT_IDENTITY_MISMATCH")
    return raw_control


def recheck_admission(proof: c.AdmissionProof, now: Callable[[], dt.datetime], transport: GitHubTransport) -> c.GateResult:
    verify_attempt(proof, transport, now())
    c.require(c.slot_is_open(proof.slot, now()), "SLOT_EXPIRED")
    return c.GateResult("ADMISSION_RECHECKED", True, proof)


def finish_slot(proof: c.AdmissionProof, outcome: Mapping[str, Any], now: Callable[[], dt.datetime],
                transport: GitHubTransport) -> c.GateResult:
    required = {"collectorExit", "workflowJobResult", "secPhase"}
    c.require(isinstance(outcome, dict) and required <= set(outcome) <= required|{"aggregateSummarySha256"}, "INVALID_OUTCOME")
    allowed = {"success", "failure", "skipped", "unknown", "cancelled"}
    c.require(all(outcome[k] in allowed for k in required), "INVALID_OUTCOME")
    if "aggregateSummarySha256" in outcome:
        c.require(c.valid_hash(outcome["aggregateSummarySha256"]), "INVALID_OUTCOME_HASH")
    verify_attempt(proof, transport, now(), allow_expired=True)
    name = proof.slot.attempt_path.replace("/attempt.json", "/result.json")
    existing = transport.request("GET", content_path(proof.slot.repository, name))
    c.require(existing.status == 404, "EXISTING_OR_UNCERTAIN_RESULT")
    raw = c.encode({"schemaVersion": "harvester-schedule-result.v1", "attemptSha256": proof.attempt_sha256,
                    "runId": proof.run_id, "headSha": proof.head_sha, "observedAt": c.stamp(now()),
                    "sourceCoverageVerified": False, **outcome})
    create_file(transport, name, raw)
    return c.GateResult("RESULT_PERSISTED")
