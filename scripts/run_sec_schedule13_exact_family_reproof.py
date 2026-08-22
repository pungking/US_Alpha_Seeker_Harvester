from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from official_shadow_runtime import _atomic_write_json, canonical_sha256
from sec_finra_shadow_evidence import (
    SCHEDULE13_PASS_STATUSES,
    collect_sec_schedule13_exact_family_evidence,
)


APPROVAL = "AUTHORIZE SEC SCHEDULE 13 EXACT-FAMILY POST-FIX REPROOF"
PRESERVED_BASELINE_RUN_ID = 32580233030


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _rehash(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("evidenceSha256", None)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    result["evidenceSha256"] = canonical_sha256(result)
    return result


def _reserve_sentinel(path: Path, reserved_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schemaVersion": "sec-schedule13-exact-family-reproof-sentinel-v1",
                "status": "IN_PROGRESS",
                "preservedBaselineRunId": PRESERVED_BASELINE_RUN_ID,
                "reservedAt": reserved_at,
                "requestBudgets": {
                    "secSchedule13Discovery": 1,
                    "secSchedule13Submissions": 1,
                    "secSchedule13RawFiling": 1,
                },
                "rawResponseStored": False,
            },
            handle,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def _finish_sentinel(
    path: Path,
    *,
    status: str,
    request_counts: Mapping[str, int],
    result_sha256: str | None,
    safe_error_category: str | None = None,
) -> None:
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(existing, dict) or existing.get("status") != "IN_PROGRESS":
        raise ValueError("targeted_sentinel_not_in_progress")
    _atomic_write_json(
        path,
        {
            **existing,
            "status": status,
            "completedAt": _utc_now(),
            "requestCounts": {
                key: int(value) for key, value in sorted(request_counts.items())
            },
            "resultSha256": result_sha256,
            "artifactHashBasis": "FINAL_TARGETED_REPROOF_ARTIFACT_BYTES",
            "safeErrorCategory": safe_error_category,
        },
    )


def run_sec_schedule13_exact_family_reproof(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_path: Path,
    retrieved_at: str,
    approval: str,
) -> dict[str, Any]:
    if approval != APPROVAL:
        raise RuntimeError("sec_schedule13_exact_family_approval_required")
    if output_path.exists() or sentinel_path.exists():
        raise FileExistsError(output_path if output_path.exists() else sentinel_path)
    _reserve_sentinel(sentinel_path, retrieved_at)
    try:
        result = collect_sec_schedule13_exact_family_evidence(
            session=session,
            sec_user_agent=str(environment.get("SEC_USER_AGENT") or ""),
            retrieved_at=retrieved_at,
        )
        result.update(
            preservedBaselineRunId=PRESERVED_BASELINE_RUN_ID,
            approvalVerified=True,
            googleDrivePublished=False,
            telegramActualSend=False,
            recurringActivationAuthorized=False,
        )
        result = _rehash(result)
        _atomic_write_json(output_path, result)
        result_sha256 = hashlib.sha256(output_path.read_bytes()).hexdigest()
        _finish_sentinel(
            sentinel_path,
            status=(
                "COMPLETE"
                if result["status"] in SCHEDULE13_PASS_STATUSES
                else "FAILED"
            ),
            request_counts=result["requestCounts"],
            result_sha256=result_sha256,
        )
        return result
    except Exception as exc:
        _finish_sentinel(
            sentinel_path,
            status="FAILED",
            request_counts={},
            result_sha256=None,
            safe_error_category=type(exc).__name__,
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sentinel", type=Path, required=True)
    args = parser.parse_args()
    session = requests.Session()
    session.trust_env = False
    try:
        result = run_sec_schedule13_exact_family_reproof(
            session=session,
            environment=os.environ,
            output_path=args.output,
            sentinel_path=args.sentinel,
            retrieved_at=_utc_now(),
            approval=str(os.getenv("SEC_SCHEDULE13_REPROOF_APPROVAL") or ""),
        )
    finally:
        session.close()
    print(
        "[SEC_SCHEDULE13_EXACT_FAMILY_REPROOF] "
        f"status={result['status']} requests={result['externalRequestCount']} "
        "rawStored=false drivePublished=false"
    )
    return 0 if result["status"] in SCHEDULE13_PASS_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
