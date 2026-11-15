"""Additive, report-only Stage4 collection evidence. Does not authorize Stage4 or orders."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Mapping

_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN = re.compile(r"^[0-9]+$")


def _utc(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def build_stage4_collection_evidence(
    *,
    target: int,
    success: int,
    skipped: int,
    errors: int,
    started_at: str,
    completed_at: str,
    github_env: Mapping[str, Any],
) -> dict[str, Any]:
    """Distinguish completed collection work from market-data freshness.

    SKIPPED is included in success, per the unchanged Harvester counter contract.
    No status from this function is a downstream permission or eligibility verdict.
    """
    counts_valid = (
        all(type(x) is int and x >= 0 for x in (target, success, skipped, errors))
        and skipped <= success
        and success + errors == target
        and target > 0
    )
    started, completed = _utc(started_at), _utc(completed_at)
    time_valid = started is not None and completed is not None and started <= completed

    run_id = str(github_env.get("GITHUB_RUN_ID") or "")
    attempt = str(github_env.get("GITHUB_RUN_ATTEMPT") or "")
    sha = str(github_env.get("GITHUB_SHA") or "").lower()
    identity_valid = (
        bool(_RUN.fullmatch(run_id)) and bool(_RUN.fullmatch(attempt))
        and int(attempt) >= 1 and bool(_SHA.fullmatch(sha))
    )
    if not counts_valid or not time_valid:
        collection_status = "UNVERIFIED"
    elif errors:
        collection_status = "PARTIAL"
    else:
        collection_status = "COMPLETE"
    return {
        "schemaVersion": "stage4-collection-evidence.v1",
        "collectionStatus": collection_status,
        "countsVerified": counts_valid,
        "timeOrderVerified": time_valid,
        "targetSymbols": target,
        "successCount": success,
        "skippedWithinSuccessCount": skipped,
        "errorCount": errors,
        "startedAt": started_at,
        "completedAt": completed_at,
        "producerRunId": run_id if identity_valid else None,
        "producerRunAttempt": int(attempt) if identity_valid else None,
        "producerHeadSha": sha if identity_valid else None,
        "producerIdentityStatus": "RECORDED_GITHUB_ENV" if identity_valid else "UNVERIFIED",
        # A collection timestamp does not prove the market-data source as-of.
        "marketDataAsOf": None,
        "marketDataAsOfStatus": "UNVERIFIED_AT_COLLECTION_SUMMARY",
        "downstreamAdmissionAuthorized": False,
    }
