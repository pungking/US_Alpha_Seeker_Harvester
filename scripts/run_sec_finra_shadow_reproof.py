from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import requests

from sec_finra_shadow_evidence import (
    PASS_STATUS,
    REQUEST_BUDGETS,
    build_sec_finra_shadow_not_run_result,
    collect_sec_finra_shadow_evidence,
    sec_finra_shadow_runtime_decision,
)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _reserve_sentinel(path: Path, reserved_at: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schemaVersion": "sec-finra-shadow-reproof-sentinel-v1",
                "status": "IN_PROGRESS",
                "reservedAt": reserved_at,
                "requestBudgets": REQUEST_BUDGETS,
            },
            handle,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        handle.write("\n")


def run_sec_finra_shadow_reproof(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_path: Path,
    retrieved_at: str,
) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(output_path)
    _reserve_sentinel(sentinel_path, retrieved_at)

    enabled, reason = sec_finra_shadow_runtime_decision(
        {**environment, "SEC_FINRA_SHADOW_PROVIDER_ENABLED": "true"}
    )
    if not enabled:
        result = build_sec_finra_shadow_not_run_result(reason)
        result.update(
            status="SEC_FINRA_SHADOW_REPROOF_CONFIG_BLOCKED",
            reproofMode="BOUNDED_MANUAL_READ_ONLY",
        )
    else:
        try:
            result = collect_sec_finra_shadow_evidence(
                session=session,
                sec_user_agent=str(environment.get("SEC_USER_AGENT") or ""),
                finra_client_id=str(environment.get("FINRA_CLIENT_ID") or ""),
                finra_client_secret=str(environment.get("FINRA_CLIENT_SECRET") or ""),
                retrieved_at=retrieved_at,
            )
        except Exception as exc:
            _atomic_write_json(
                sentinel_path,
                {
                    "schemaVersion": "sec-finra-shadow-reproof-sentinel-v1",
                    "status": "FAILED",
                    "reservedAt": retrieved_at,
                    "completedAt": _utc_now(),
                    "safeErrorCategory": type(exc).__name__,
                },
            )
            raise
        result["reproofMode"] = "BOUNDED_MANUAL_READ_ONLY"

    result["credentialValuesPersisted"] = False
    result["googleDrivePublished"] = False
    result["recurringProviderEnabled"] = False
    _atomic_write_json(output_path, result)
    _atomic_write_json(
        sentinel_path,
        {
            "schemaVersion": "sec-finra-shadow-reproof-sentinel-v1",
            "status": "COMPLETE",
            "reservedAt": retrieved_at,
            "completedAt": _utc_now(),
            "requestCounts": result["requestCounts"],
            "resultSha256": _sha256(output_path),
        },
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sentinel", type=Path, required=True)
    args = parser.parse_args()
    result = run_sec_finra_shadow_reproof(
        session=requests.Session(),
        environment=os.environ,
        output_path=args.output,
        sentinel_path=args.sentinel,
        retrieved_at=_utc_now(),
    )
    print(
        "[SEC_FINRA_SHADOW_REPROOF] "
        f"status={result['status']} requests={result['externalRequestCount']} "
        f"unknown={result['unknownOrUnclassifiedRows']} rawStored=false"
    )
    return 0 if result["status"] == PASS_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
