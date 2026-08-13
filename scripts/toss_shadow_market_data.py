from __future__ import annotations

import datetime
import email.utils
import hashlib
import json
import os
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "toss-market-data-shadow-v1"
TOKEN_ENDPOINT = "https://openapi.tossinvest.com/oauth2/token"
PRICES_ENDPOINT = "https://openapi.tossinvest.com/api/v1/prices"
US_CALENDAR_ENDPOINT = "https://openapi.tossinvest.com/api/v1/market-calendar/US"
MAX_SYMBOLS_PER_PRICE_REQUEST = 200
MAX_PRICE_REQUESTS_PER_RUN = 2
PASS_STATUS = "TOSS_SHADOW_PASS"
CLOCK_ROOT_CAUSES = (
    "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE",
    "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES",
    "HTTP_DATE_REFERENCE_MISSING_OR_INVALID",
    "PAYLOAD_TIMESTAMP_MISSING",
    "PARTIAL_SYMBOL_RESPONSE",
    "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT",
)
FAILURE_STATUSES = {
    "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED",
    "TOSS_SHADOW_RATE_LIMITED",
    "TOSS_SHADOW_TRANSIENT_FAILURE",
    "TOSS_SHADOW_SCHEMA_INVALID",
    "TOSS_SHADOW_STALE_OR_PARTIAL",
    "TOSS_SHADOW_SOURCE_CONFLICT",
}


class _Response(Protocol):
    status_code: int
    content: bytes
    headers: Any

    def json(self) -> Any: ...


class _Session(Protocol):
    def post(self, url: str, **kwargs: Any) -> _Response: ...

    def get(self, url: str, **kwargs: Any) -> _Response: ...


def _bool_env(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def toss_shadow_runtime_decision(environment: Mapping[str, Any]) -> tuple[bool, str]:
    if not _bool_env(environment.get("TOSS_SHADOW_PROVIDER_ENABLED")):
        return False, "shadow_provider_disabled"
    if _bool_env(environment.get("GITHUB_ACTIONS")):
        return False, "github_hosted_runner_disabled"
    if not _bool_env(environment.get("TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED")):
        return False, "registered_egress_not_confirmed"
    return True, "registered_server_runtime_enabled"


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.utcoffset() is not None else None


def _parse_date(value: Any) -> datetime.date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _status_category(status_code: int) -> str:
    return f"{status_code // 100}xx" if 100 <= status_code <= 599 else "invalid"


def _bounded_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if re.fullmatch(r"[a-z0-9_-]{1,64}", normalized) else None


def _safe_error_code(response: _Response, *, oauth: bool) -> str | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if oauth:
        return _bounded_error_code(
            error.get("code") if isinstance(error, dict) else error
        )
    return _bounded_error_code(error.get("code") if isinstance(error, dict) else error)


def _response_sha256(response: _Response) -> str:
    return hashlib.sha256(bytes(getattr(response, "content", b""))).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _symbol_scope_sha256(symbols: list[str]) -> str:
    encoded = "\n".join(sorted(set(symbols))).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sanitize_request_source_artifact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    file_name = value.get("file")
    sha256 = value.get("sha256")
    hash_basis = value.get("hashBasis")
    generated_at = value.get("generatedAt")
    generated_at_source = value.get("generatedAtSource")
    request_scope_sha256 = value.get("requestScopeSha256")
    if (
        not isinstance(file_name, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,255}", file_name)
        or not file_name.startswith("STAGE3_FUNDAMENTAL_FULL_")
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or hash_basis not in {"CANONICAL_JSON", "RAW_BYTES"}
        or (generated_at is not None and _parse_timestamp(generated_at) is None)
        or generated_at_source not in {
            None,
            "ARTIFACT_FIELD",
            "GOOGLE_DRIVE_CREATED_TIME",
        }
        or not isinstance(request_scope_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", request_scope_sha256)
    ):
        return None
    safe = {
        "file": file_name,
        "sha256": sha256,
        "hashBasis": hash_basis,
        "generatedAt": generated_at,
        "requestScopeSha256": request_scope_sha256,
    }
    if generated_at_source is not None:
        safe["generatedAtSource"] = generated_at_source
    return safe


def build_stage3_request_scope(
    *,
    file_name: str,
    payload: Any,
    expected_sha256: str | None = None,
    drive_created_at: str | None = None,
) -> dict[str, Any]:
    base = {
        "status": "REQUEST_SOURCE_ARTIFACT_INVALID",
        "symbols": [],
        "sourceArtifact": None,
        "normalizationCollisionRows": 0,
        "invalidSymbolRows": 0,
    }
    if (
        not isinstance(file_name, str)
        or not re.fullmatch(r"STAGE3_FUNDAMENTAL_FULL_[A-Za-z0-9_.-]{1,220}", file_name)
        or not isinstance(payload, (dict, list))
    ):
        return base

    actual_sha256 = _canonical_sha256(payload)
    if expected_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_sha256)):
            return {**base, "status": "REQUEST_SOURCE_HASH_INVALID"}
        if actual_sha256 != expected_sha256:
            return {**base, "status": "REQUEST_SOURCE_HASH_MISMATCH"}

    rows = (
        payload.get("fundamental_universe") or payload.get("stocks") or []
        if isinstance(payload, dict)
        else payload
    )
    if not isinstance(rows, list) or not rows:
        return base

    normalized_rows: list[str] = []
    invalid_symbol_rows = 0
    for row in rows:
        symbol = row.get("symbol") if isinstance(row, dict) else None
        normalized = str(symbol or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9.-]+", normalized):
            invalid_symbol_rows += 1
            continue
        normalized_rows.append(normalized)
    symbols = sorted(set(normalized_rows))
    collision_rows = len(normalized_rows) - len(symbols)
    if invalid_symbol_rows:
        return {
            **base,
            "status": "REQUEST_SOURCE_SYMBOL_INVALID",
            "invalidSymbolRows": invalid_symbol_rows,
        }
    if collision_rows:
        return {
            **base,
            "status": "NORMALIZATION_COLLISION",
            "normalizationCollisionRows": collision_rows,
        }

    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    artifact_timestamp = (
        payload.get("generated_at") or payload.get("generatedAt")
        if isinstance(payload, dict)
        else None
    )
    if artifact_timestamp is None and isinstance(manifest, dict):
        artifact_timestamp = (
            manifest.get("timestamp")
            or manifest.get("generated_at")
            or manifest.get("generatedAt")
        )
    if _parse_timestamp(artifact_timestamp) is not None:
        generated_at = str(artifact_timestamp)
        generated_at_source = "ARTIFACT_FIELD"
    elif _parse_timestamp(drive_created_at) is not None:
        generated_at = str(drive_created_at)
        generated_at_source = "GOOGLE_DRIVE_CREATED_TIME"
    else:
        return {**base, "status": "REQUEST_SOURCE_TIMESTAMP_INVALID"}

    request_scope_sha256 = _symbol_scope_sha256(symbols)
    source_artifact = {
        "file": file_name,
        "sha256": actual_sha256,
        "hashBasis": "CANONICAL_JSON",
        "generatedAt": generated_at,
        "generatedAtSource": generated_at_source,
        "requestScopeSha256": request_scope_sha256,
    }
    return {
        "status": "VERIFIED_STAGE3_REQUEST_SCOPE",
        "symbols": symbols,
        "sourceArtifact": source_artifact,
        "normalizationCollisionRows": 0,
        "invalidSymbolRows": 0,
        "requestScopeSha256": request_scope_sha256,
    }


def same_stage3_idempotency_key(request_source_artifact: Mapping[str, Any]) -> str:
    source = _sanitize_request_source_artifact(request_source_artifact)
    if source is None:
        raise ValueError("invalid_request_source_artifact")
    return _canonical_sha256(
        {
            "schemaVersion": SCHEMA_VERSION,
            "file": source["file"],
            "sha256": source["sha256"],
            "hashBasis": source["hashBasis"],
            "requestScopeSha256": source["requestScopeSha256"],
        }
    )


def toss_shadow_matches_stage3(
    shadow: Any,
    request_source_artifact: Mapping[str, Any],
) -> bool:
    expected = _sanitize_request_source_artifact(request_source_artifact)
    lineage = shadow.get("requestLineage") if isinstance(shadow, Mapping) else None
    actual = _sanitize_request_source_artifact(
        lineage.get("requestSourceArtifact") if isinstance(lineage, Mapping) else None
    )
    summary = shadow.get("summary") if isinstance(shadow, Mapping) else None
    prices = shadow.get("prices") if isinstance(shadow, Mapping) else None
    request_counts = (
        shadow.get("requestCounts") if isinstance(shadow, Mapping) else None
    )
    response_sha256 = (
        shadow.get("responseSha256") if isinstance(shadow, Mapping) else None
    )
    price_request_count = (
        request_counts.get("prices") if isinstance(request_counts, Mapping) else None
    )
    return bool(
        expected
        and actual
        and shadow.get("schemaVersion") == SCHEMA_VERSION
        and shadow.get("status") == PASS_STATUS
        and shadow.get("mode") == "SHADOW_ONLY"
        and shadow.get("provider") == "TOSS_OPEN_API"
        and shadow.get("eligible") is True
        and shadow.get("tossEvidenceExcluded") is False
        and shadow.get("canonicalSourceChanged") is False
        and shadow.get("policyImpact") == "NONE_REPORT_ONLY"
        and shadow.get("accountHeaderUsed") is False
        and shadow.get("orderEndpointUsed") is False
        and shadow.get("rateLimitHeadersComplete") is True
        and isinstance(summary, Mapping)
        and isinstance(prices, list)
        and isinstance(summary.get("requestedRows"), int)
        and summary.get("requestedRows") > 0
        and summary.get("requestedRows") == summary.get("matchedRows")
        and summary.get("matchedRows") == len(prices)
        and summary.get("missingRows") == 0
        and summary.get("invalidRows") == 0
        and summary.get("duplicateRows") == 0
        and isinstance(request_counts, Mapping)
        and request_counts.get("oauth") == 1
        and request_counts.get("marketCalendar") == 1
        and isinstance(price_request_count, int)
        and 1 <= price_request_count <= MAX_PRICE_REQUESTS_PER_RUN
        and isinstance(response_sha256, Mapping)
        and re.fullmatch(r"[0-9a-f]{64}", str(response_sha256.get("oauth") or ""))
        and re.fullmatch(
            r"[0-9a-f]{64}", str(response_sha256.get("marketCalendar") or "")
        )
        and isinstance(response_sha256.get("prices"), list)
        and len(response_sha256["prices"]) == price_request_count
        and all(
            re.fullmatch(r"[0-9a-f]{64}", str(value or ""))
            for value in response_sha256["prices"]
        )
        and isinstance(lineage, Mapping)
        and lineage.get("status") == "VERIFIED_STAGE3_REQUEST_SCOPE"
        and lineage.get("requestScopeSha256") == expected["requestScopeSha256"]
        and all(
            actual[key] == expected[key]
            for key in ("file", "sha256", "hashBasis", "requestScopeSha256")
        )
    )


def same_stage3_scope_matches(
    scope: Any,
    request_source_artifact: Mapping[str, Any],
) -> bool:
    expected = _sanitize_request_source_artifact(request_source_artifact)
    actual = _sanitize_request_source_artifact(
        scope.get("sourceArtifact") if isinstance(scope, Mapping) else None
    )
    return bool(
        expected
        and actual
        and scope.get("status") == "VERIFIED_STAGE3_REQUEST_SCOPE"
        and all(
            actual[key] == expected[key]
            for key in ("file", "sha256", "hashBasis", "requestScopeSha256")
        )
    )


def build_same_stage3_readiness(
    *,
    scope: Mapping[str, Any],
    existing_shadow: Any,
    collector_enabled: bool,
) -> dict[str, Any]:
    source = scope.get("sourceArtifact") if isinstance(scope, Mapping) else None
    source_valid = (
        scope.get("status") == "VERIFIED_STAGE3_REQUEST_SCOPE"
        and _sanitize_request_source_artifact(source) is not None
    )
    matched = source_valid and toss_shadow_matches_stage3(existing_shadow, source)
    symbols = scope.get("symbols") if isinstance(scope.get("symbols"), list) else []
    existing_lineage = (
        existing_shadow.get("requestLineage")
        if isinstance(existing_shadow, Mapping)
        else None
    )
    existing_source = (
        existing_lineage.get("requestSourceArtifact")
        if isinstance(existing_lineage, Mapping)
        else None
    )
    idempotency_key = same_stage3_idempotency_key(source) if source_valid else None
    if matched:
        status = "EXISTING_MATCHED_SHADOW_REUSABLE"
        request_budget = {"oauth": 0, "marketCalendar": 0, "pricesMax": 0}
        next_action = "reuse_existing_matched_shadow"
    elif source_valid:
        status = "SAME_STAGE3_HANDOFF_READY_FOR_ACTIVATION"
        request_budget = {
            "oauth": 1,
            "marketCalendar": 1,
            "pricesMax": min(MAX_PRICE_REQUESTS_PER_RUN, (len(symbols) + 199) // 200),
        }
        next_action = "activate_registered_mac_collector"
    else:
        status = "STATIC_CONTRACT_DEFECT"
        request_budget = {"oauth": 0, "marketCalendar": 0, "pricesMax": 0}
        next_action = "repair_stage3_handoff_evidence"
    return {
        "status": status,
        "detectedStage3File": source.get("file") if isinstance(source, Mapping) else None,
        "detectedStage3Sha256": source.get("sha256") if isinstance(source, Mapping) else None,
        "stage3HashVerified": source_valid,
        "requestScopeSha256": source.get("requestScopeSha256") if isinstance(source, Mapping) else None,
        "existingShadowSourceFile": existing_source.get("file") if isinstance(existing_source, Mapping) else None,
        "existingShadowSourceSha256": existing_source.get("sha256") if isinstance(existing_source, Mapping) else None,
        "sourceArtifactMatches": bool(matched),
        "idempotencyKey": idempotency_key,
        "collectorRequired": bool(source_valid and not matched),
        "collectorEnabled": bool(collector_enabled),
        "requestBudget": request_budget,
        "publishReady": source_valid,
        "canonicalAnalysisCanContinue": True,
        "primaryHandoffBlocker": None if matched else "MAC_COLLECTOR_CONFIGURATION_REQUIRED" if source_valid else str(scope.get("status") or "STATIC_CONTRACT_DEFECT"),
        "nextAction": next_action,
    }


def reserve_same_stage3_sentinel(
    directory: Path,
    idempotency_key: str,
    request_source_artifact: Mapping[str, Any],
    reserved_at: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", idempotency_key):
        raise ValueError("invalid_idempotency_key")
    source = _sanitize_request_source_artifact(request_source_artifact)
    if source is None or _parse_timestamp(reserved_at) is None:
        raise ValueError("invalid_sentinel_contract")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{idempotency_key}.json"
    payload = {
        "schemaVersion": "toss-same-stage3-sentinel-v1",
        "status": "IN_PROGRESS",
        "idempotencyKey": idempotency_key,
        "requestSourceArtifact": source,
        "reservedAt": reserved_at,
    }
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "SENTINEL_INVALID", "path": str(path)}
        status = str(existing.get("status") or "SENTINEL_INVALID")
        return {
            "status": status if status in {"IN_PROGRESS", "SUCCESS", "FAILED"} else "SENTINEL_INVALID",
            "path": str(path),
        }
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    return {"status": "RESERVED", "path": str(path)}


def finish_same_stage3_sentinel(
    path: Path,
    *,
    status: str,
    completed_at: str,
    artifact_sha256: str | None = None,
) -> None:
    if status not in {"SUCCESS", "FAILED"} or _parse_timestamp(completed_at) is None:
        raise ValueError("invalid_sentinel_completion")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("status") != "IN_PROGRESS":
        raise ValueError("sentinel_not_in_progress")
    if artifact_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256):
        raise ValueError("invalid_artifact_sha256")
    payload.update({"status": status, "completedAt": completed_at})
    if artifact_sha256 is not None:
        payload["artifactSha256"] = artifact_sha256
        payload["artifactFile"] = "TOSS_MARKET_DATA_SHADOW.json"
        payload["artifactHashBasis"] = "CANONICAL_JSON"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, path)


def _shadow_archive_filename(payload: Mapping[str, Any]) -> str:
    lineage = payload.get("requestLineage")
    source = (
        lineage.get("requestSourceArtifact") if isinstance(lineage, Mapping) else None
    )
    try:
        key = same_stage3_idempotency_key(source)
    except (TypeError, ValueError):
        key = _canonical_sha256(payload)
    status = re.sub(r"[^A-Z0-9_]+", "_", str(payload.get("status") or "UNKNOWN").upper())[:48]
    revision = _canonical_sha256(payload)[:16]
    return f"TOSS_MARKET_DATA_SHADOW_{key[:16]}_{status}_{revision}.json"


def publish_toss_shadow_with_archive(
    *,
    previous_shadow: Any,
    current_shadow: Mapping[str, Any],
    uploader: Callable[[str, Mapping[str, Any]], None],
) -> dict[str, Any]:
    uploaded: list[str] = []
    try:
        canonical_sha256 = _canonical_sha256(current_shadow)
        if isinstance(previous_shadow, Mapping):
            previous_name = _shadow_archive_filename(previous_shadow)
            uploader(previous_name, previous_shadow)
            uploaded.append(previous_name)
        current_name = _shadow_archive_filename(current_shadow)
        uploader(current_name, current_shadow)
        uploaded.append(current_name)
        uploader("TOSS_MARKET_DATA_SHADOW.json", current_shadow)
        uploaded.append("TOSS_MARKET_DATA_SHADOW.json")
    except Exception as exc:
        return {
            "status": "ARCHIVE_OR_CANONICAL_FAILED",
            "safeErrorCategory": type(exc).__name__,
            "uploadedFiles": uploaded,
            "canonicalPublished": False,
            "canonicalArtifactSha256": None,
        }
    return {
        "status": "ARCHIVE_AND_CANONICAL_COMPLETE",
        "safeErrorCategory": None,
        "uploadedFiles": uploaded,
        "canonicalPublished": True,
        "canonicalArtifactSha256": canonical_sha256,
    }


def _rate_limit_evidence(response: _Response) -> dict[str, Any]:
    headers = {
        str(key).lower(): str(value)
        for key, value in (getattr(response, "headers", {}) or {}).items()
    }
    evidence = {
        "limit": headers.get("x-ratelimit-limit"),
        "remaining": headers.get("x-ratelimit-remaining"),
        "reset": headers.get("x-ratelimit-reset"),
        "retryAfter": headers.get("retry-after"),
    }
    evidence["requiredHeadersPresent"] = all(
        evidence[key] is not None for key in ("limit", "remaining", "reset")
    )
    return evidence


def _utc_iso(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _http_date_evidence(
    response: _Response,
    *,
    local_received_at: datetime.datetime,
) -> dict[str, Any]:
    headers = {
        str(key).lower(): str(value)
        for key, value in (getattr(response, "headers", {}) or {}).items()
    }
    raw_date = headers.get("date")
    evidence: dict[str, Any] = {
        "httpDateHeaderPresent": raw_date is not None,
        "httpDateParseStatus": "MISSING" if raw_date is None else "INVALID",
        "parsedHttpDateAt": None,
        "httpDatePrecision": "SECONDS",
        "localToHttpDateOffsetMs": None,
    }
    if raw_date is None:
        return evidence
    try:
        parsed = email.utils.parsedate_to_datetime(raw_date)
    except (TypeError, ValueError, OverflowError):
        return evidence
    if not isinstance(parsed, datetime.datetime) or parsed.utcoffset() is None:
        return evidence
    parsed_utc = parsed.astimezone(datetime.timezone.utc).replace(microsecond=0)
    local_utc = local_received_at.astimezone(datetime.timezone.utc)
    evidence.update(
        {
            "httpDateParseStatus": "VALID",
            "parsedHttpDateAt": _utc_iso(parsed_utc),
            "localToHttpDateOffsetMs": int(
                round((parsed_utc - local_utc).total_seconds() * 1000)
            ),
        }
    )
    return evidence


def _clock_summary_template() -> dict[str, Any]:
    return {
        "responseBatchCount": 0,
        "httpDatePresentResponses": 0,
        "httpDateValidResponses": 0,
        "httpDateInvalidResponses": 0,
        "payloadTimestampRows": 0,
        "payloadTimestampMin": None,
        "payloadTimestampMax": None,
        "payloadAfterLocalReceiptRows": 0,
        "payloadComparedToHttpDateRows": 0,
        "payloadAfterHttpDateRows": 0,
        "localToHttpDateMinOffsetMs": None,
        "localToHttpDateMaxOffsetMs": None,
        "payloadToLocalReceiptMinOffsetMs": None,
        "payloadToLocalReceiptMaxOffsetMs": None,
        "payloadToHttpDateMinOffsetMs": None,
        "payloadToHttpDateMaxOffsetMs": None,
        "clockReferenceStatus": "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT",
        "primaryRootCause": None,
        "rootCauseCounts": {cause: 0 for cause in CLOCK_ROOT_CAUSES},
        "classifiableFailureRows": 0,
        "classifiedRootCauseRows": 0,
        "rootCauseCountMatches": True,
        "unknownOrUnclassifiedRows": 0,
    }


def _response_clock_rows(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    clock_domain = result.get("clockDomainEvidence")
    responses = (
        clock_domain.get("responses")
        if isinstance(clock_domain, Mapping)
        and isinstance(clock_domain.get("responses"), Mapping)
        else {}
    )
    rows = [responses.get("oauth"), responses.get("marketCalendar")]
    price_rows = responses.get("prices")
    if isinstance(price_rows, list):
        rows.extend(price_rows)
    return [row for row in rows if isinstance(row, dict)]


def _refresh_response_clock_summary(result: dict[str, Any]) -> None:
    rows = _response_clock_rows(result)
    offsets = [
        row["localToHttpDateOffsetMs"]
        for row in rows
        if isinstance(row.get("localToHttpDateOffsetMs"), int)
    ]
    summary = result["clockDomainEvidence"]["summary"]
    summary.update(
        {
            "responseBatchCount": len(rows),
            "httpDatePresentResponses": sum(
                row.get("httpDateHeaderPresent") is True for row in rows
            ),
            "httpDateValidResponses": sum(
                row.get("httpDateParseStatus") == "VALID" for row in rows
            ),
            "httpDateInvalidResponses": sum(
                row.get("httpDateParseStatus") == "INVALID" for row in rows
            ),
            "localToHttpDateMinOffsetMs": min(offsets) if offsets else None,
            "localToHttpDateMaxOffsetMs": max(offsets) if offsets else None,
            "clockReferenceStatus": (
                "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"
                if rows
                and any(row.get("httpDateParseStatus") != "VALID" for row in rows)
                else "HTTP_DATE_REFERENCE_VALID"
                if rows
                else "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT"
            ),
        }
    )


def _base_result(
    *,
    symbols: list[str],
    retrieved_at: str,
    calendar_date: str,
    capability_artifact_sha256: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "TOSS_SHADOW_SCHEMA_INVALID",
        "mode": "SHADOW_ONLY",
        "provider": "TOSS_OPEN_API",
        "priceSemantics": "LATEST_QUOTE_NOT_HISTORICAL_ADJUSTED_CANDLE",
        "adjustedPriceSemantics": "NOT_APPLICABLE_TO_PRICES_ENDPOINT",
        "capabilityArtifactSha256": capability_artifact_sha256,
        "requestScopeSha256": _canonical_sha256(
            {"symbols": symbols, "calendarDate": calendar_date}
        ),
        "requestLineage": {
            "status": "REQUEST_SCOPE_LINEAGE_INCOMPLETE",
            "requestSourceArtifact": None,
            "requestScopeSha256": _symbol_scope_sha256(symbols),
            "providerSymbolMappingStatus": "NOT_EVALUATED",
            "providerSymbolMappingRule": "US_CLASS_SHARE_HYPHEN_TO_DOT",
            "providerMappedRows": 0,
            "providerRequestScopeSha256": None,
            "batches": [],
        },
        "collectionStartedAt": retrieved_at,
        "retrievedAt": retrieved_at,
        "responseReceivedAt": {
            "oauth": None,
            "marketCalendar": None,
            "prices": [],
        },
        "clockDomainEvidence": {
            "httpDatePrecision": "SECONDS",
            "responses": {
                "oauth": None,
                "marketCalendar": None,
                "prices": [],
            },
            "summary": _clock_summary_template(),
        },
        "sourceAsOf": None,
        "marketTimezone": "America/New_York",
        "requestCounts": {"oauth": 0, "marketCalendar": 0, "prices": 0},
        "endpointGroups": {
            "oauth": "AUTH",
            "marketCalendar": "MARKET_INFO",
            "prices": "MARKET_DATA",
        },
        "responseSha256": {"marketCalendar": None, "prices": []},
        "httpStatusCategories": {
            "oauth": None,
            "marketCalendar": None,
            "prices": [],
        },
        "rateLimitHeaders": {"oauth": None, "marketCalendar": None, "prices": []},
        "rateLimitHeadersComplete": False,
        "calendar": {"schemaValid": False, "marketTimezone": "Asia/Seoul"},
        "prices": [],
        "summary": {
            "requestedRows": len(symbols),
            "matchedRows": 0,
            "missingRows": len(symbols),
            "invalidRows": 0,
            "duplicateRows": 0,
            "responseSha256Rows": 0,
        },
        "diagnostics": {
            "timestampMissingRows": 0,
            "futureTimestampRows": 0,
            "outOfCalendarDateRows": 0,
            "unreturnedSymbolRows": len(symbols),
            "staleOrFutureRows": 0,
            "currencyConflictRows": 0,
            "minFutureSkewMs": None,
            "maxFutureSkewMs": None,
        },
        "eligible": False,
        "tossEvidenceExcluded": True,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "circuitBreaker": "OPEN_FOR_RUN",
        "affectedEndpointGroup": None,
        "httpStatusCategory": None,
        "safeErrorCategory": None,
        "safeErrorCode": None,
        "accountHeaderUsed": False,
        "orderEndpointUsed": False,
    }


def _failure_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED"
    if status_code == 429:
        return "TOSS_SHADOW_RATE_LIMITED"
    if status_code >= 500:
        return "TOSS_SHADOW_TRANSIENT_FAILURE"
    return "TOSS_SHADOW_SCHEMA_INVALID"


def build_toss_shadow_blocked_result(
    *,
    status: str,
    safe_error_category: str,
    capability_artifact_sha256: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    result = _base_result(
        symbols=[],
        retrieved_at=retrieved_at or "",
        calendar_date="",
        capability_artifact_sha256=capability_artifact_sha256,
    )
    result["runtimeAction"] = "NOT_RUN_CAPABILITY_BLOCKED"
    return _mark_failure(
        result,
        status=status,
        category=safe_error_category,
        endpoint_group="AUTH",
    )


def _mark_failure(
    result: dict[str, Any],
    *,
    status: str,
    category: str,
    endpoint_group: str,
    http_status: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    result["status"] = status
    result["eligible"] = False
    result["tossEvidenceExcluded"] = True
    result["circuitBreaker"] = "OPEN_FOR_RUN"
    result["prices"] = []
    result["safeErrorCategory"] = category
    result["safeErrorCode"] = error_code
    result["affectedEndpointGroup"] = endpoint_group
    result["httpStatusCategory"] = (
        _status_category(http_status) if http_status is not None else None
    )
    return result


def _validate_calendar(
    payload: Any,
    requested_date: str,
) -> tuple[dict[str, Any] | None, set[datetime.date]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None, set()

    required_days = ("today", "previousBusinessDay", "nextBusinessDay")
    if not all(isinstance(result.get(key), dict) for key in required_days):
        return None, set()
    if result["today"].get("date") != requested_date:
        return None, set()

    parsed_dates = {
        key: _parse_date(result[key].get("date"))
        for key in required_days
    }
    if (
        any(value is None for value in parsed_dates.values())
        or not parsed_dates["previousBusinessDay"]
        < parsed_dates["today"]
        < parsed_dates["nextBusinessDay"]
    ):
        return None, set()

    allowed_price_dates: set[datetime.date] = set()
    safe_days: dict[str, Any] = {}
    for key in required_days:
        day = result[key]
        parsed_day = parsed_dates[key]
        if key in {"today", "previousBusinessDay"}:
            allowed_price_dates.add(parsed_day)
        safe_day: dict[str, Any] = {"date": day["date"]}
        for session_name in ("dayMarket", "preMarket", "regularMarket", "afterMarket"):
            session = day.get(session_name)
            if session is None:
                safe_day[session_name] = None
                continue
            if not isinstance(session, dict):
                return None, set()
            start = _parse_timestamp(session.get("startTime"))
            end = _parse_timestamp(session.get("endTime"))
            if (
                start is None
                or end is None
                or start.utcoffset() != datetime.timedelta(hours=9)
                or end.utcoffset() != datetime.timedelta(hours=9)
                or start >= end
            ):
                return None, set()
            safe_day[session_name] = {
                "startTime": session["startTime"],
                "endTime": session["endTime"],
            }
        safe_days[key] = safe_day
    return safe_days, allowed_price_dates


def _normalize_symbols(symbols: list[str]) -> list[str]:
    valid = {
        symbol.strip().upper()
        for symbol in symbols
        if isinstance(symbol, str)
        and re.fullmatch(r"[A-Za-z0-9.\-]+", symbol.strip())
    }
    return sorted(valid)


def _toss_provider_symbol(canonical_symbol: str) -> str:
    if re.fullmatch(r"[A-Z0-9]{1,10}-[A-Z]", canonical_symbol):
        return canonical_symbol.replace("-", ".")
    return canonical_symbol


def _provider_symbol_maps(
    canonical_symbols: list[str],
) -> tuple[dict[str, str], dict[str, str], bool]:
    canonical_to_provider = {
        symbol: _toss_provider_symbol(symbol) for symbol in canonical_symbols
    }
    provider_to_canonical: dict[str, str] = {}
    collision = False
    for canonical_symbol, provider_symbol in canonical_to_provider.items():
        if provider_symbol in provider_to_canonical:
            collision = True
            continue
        provider_to_canonical[provider_symbol] = canonical_symbol
    return canonical_to_provider, provider_to_canonical, collision


def collect_toss_shadow_market_data(
    session: _Session,
    *,
    client_id: str,
    client_secret: str,
    symbols: list[str],
    retrieved_at: str,
    calendar_date: str,
    capability_artifact_sha256: str,
    request_source_artifact: Mapping[str, Any] | None = None,
    max_price_requests: int = 2,
    clock: Callable[[], datetime.datetime] | None = None,
    monotonic_clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    normalized_symbols = _normalize_symbols(symbols)
    (
        canonical_to_provider,
        provider_to_canonical,
        provider_mapping_collision,
    ) = _provider_symbol_maps(normalized_symbols)
    provider_symbols = sorted(provider_to_canonical)
    result = _base_result(
        symbols=normalized_symbols,
        retrieved_at=retrieved_at,
        calendar_date=calendar_date,
        capability_artifact_sha256=capability_artifact_sha256,
    )
    safe_request_source = _sanitize_request_source_artifact(
        request_source_artifact
    )
    request_scope_matches = (
        safe_request_source is not None
        and safe_request_source.get("requestScopeSha256")
        == result["requestLineage"]["requestScopeSha256"]
    )
    if request_scope_matches:
        result["requestLineage"].update(
            {
                "status": "VERIFIED_STAGE3_REQUEST_SCOPE",
                "requestSourceArtifact": safe_request_source,
            }
        )
    result["requestLineage"].update(
        {
            "providerSymbolMappingStatus": (
                "COLLISION"
                if provider_mapping_collision
                else "VERIFIED_DOT_HYPHEN_ALIAS"
                if any(
                    canonical != provider
                    for canonical, provider in canonical_to_provider.items()
                )
                else "IDENTITY"
            ),
            "providerMappedRows": sum(
                canonical != provider
                for canonical, provider in canonical_to_provider.items()
            ),
            "providerRequestScopeSha256": _symbol_scope_sha256(provider_symbols),
        }
    )
    retrieved_timestamp = _parse_timestamp(retrieved_at)
    if (
        not normalized_symbols
        or provider_mapping_collision
        or safe_request_source is None
        or not request_scope_matches
        or retrieved_timestamp is None
        or _parse_date(calendar_date) is None
        or not re.fullmatch(r"[0-9a-f]{64}", capability_artifact_sha256)
        or max_price_requests <= 0
        or max_price_requests > MAX_PRICE_REQUESTS_PER_RUN
        or len(normalized_symbols) > MAX_SYMBOLS_PER_PRICE_REQUEST * max_price_requests
    ):
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=(
                "provider_symbol_mapping_collision"
                if provider_mapping_collision
                else "request_source_lineage_invalid"
                if safe_request_source is None
                else "request_scope_hash_mismatch"
                if not request_scope_matches
                else "request_contract_invalid"
            ),
            endpoint_group="LOCAL_CONTRACT",
        )

    response_clock = clock or (
        lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    duration_clock = monotonic_clock or time.monotonic

    def begin_request() -> tuple[datetime.datetime, float] | None:
        started = response_clock()
        if (
            not isinstance(started, datetime.datetime)
            or started.utcoffset() is None
            or started < retrieved_timestamp
        ):
            return None
        monotonic_started = duration_clock()
        if not isinstance(monotonic_started, (int, float)):
            return None
        return started.astimezone(datetime.timezone.utc), float(monotonic_started)

    def record_response_received(
        endpoint: str,
        response: _Response,
        request_timing: tuple[datetime.datetime, float],
    ) -> tuple[datetime.datetime, dict[str, Any]] | None:
        request_started_at, monotonic_started = request_timing
        received = response_clock()
        monotonic_finished = duration_clock()
        if (
            not isinstance(received, datetime.datetime)
            or received.utcoffset() is None
            or received < request_started_at
            or not isinstance(monotonic_finished, (int, float))
            or float(monotonic_finished) < monotonic_started
        ):
            return None
        received_utc = received.astimezone(datetime.timezone.utc)
        received_iso = _utc_iso(received_utc)
        clock_evidence = {
            "requestStartedAt": _utc_iso(request_started_at),
            "localResponseReceivedAt": received_iso,
            "requestDurationMs": int(
                round((float(monotonic_finished) - monotonic_started) * 1000)
            ),
            **_http_date_evidence(
                response,
                local_received_at=received_utc,
            ),
        }
        if endpoint == "prices":
            result["responseReceivedAt"]["prices"].append(received_iso)
            result["clockDomainEvidence"]["responses"]["prices"].append(
                clock_evidence
            )
        else:
            result["responseReceivedAt"][endpoint] = received_iso
            result["clockDomainEvidence"]["responses"][endpoint] = clock_evidence
        result["retrievedAt"] = received_iso
        _refresh_response_clock_summary(result)
        return received_utc, clock_evidence

    token_timing = begin_request()
    if token_timing is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="oauth_request_started_at_invalid",
            endpoint_group="AUTH",
        )
    result["requestCounts"]["oauth"] = 1
    try:
        token_response = session.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except Exception as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_TRANSIENT_FAILURE",
            category=type(exc).__name__,
            endpoint_group="AUTH",
        )

    if record_response_received("oauth", token_response, token_timing) is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="oauth_received_at_invalid",
            endpoint_group="AUTH",
        )

    result["rateLimitHeaders"]["oauth"] = _rate_limit_evidence(token_response)
    token_status = int(getattr(token_response, "status_code", 0) or 0)
    result["httpStatusCategories"]["oauth"] = _status_category(token_status)
    if token_status != 200:
        return _mark_failure(
            result,
            status=_failure_status(token_status),
            category=f"oauth_http_{token_status}",
            endpoint_group="AUTH",
            http_status=token_status,
            error_code=_safe_error_code(token_response, oauth=True),
        )
    try:
        token_payload = token_response.json()
    except (TypeError, ValueError) as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=f"oauth_json_{type(exc).__name__}",
            endpoint_group="AUTH",
        )
    access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    if (
        not isinstance(access_token, str)
        or not access_token
        or token_payload.get("token_type") != "Bearer"
        or not isinstance(token_payload.get("expires_in"), int)
        or token_payload["expires_in"] <= 0
    ):
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="oauth_schema_invalid",
            endpoint_group="AUTH",
        )

    authorization = {"Authorization": f"Bearer {access_token}"}
    calendar_timing = begin_request()
    if calendar_timing is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="market_calendar_request_started_at_invalid",
            endpoint_group="MARKET_INFO",
        )
    result["requestCounts"]["marketCalendar"] = 1
    try:
        calendar_response = session.get(
            US_CALENDAR_ENDPOINT,
            params={"date": calendar_date},
            headers=authorization,
            timeout=20,
        )
    except Exception as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_TRANSIENT_FAILURE",
            category=type(exc).__name__,
            endpoint_group="MARKET_INFO",
        )
    if record_response_received(
        "marketCalendar", calendar_response, calendar_timing
    ) is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="market_calendar_received_at_invalid",
            endpoint_group="MARKET_INFO",
        )
    calendar_status = int(getattr(calendar_response, "status_code", 0) or 0)
    result["httpStatusCategories"]["marketCalendar"] = _status_category(
        calendar_status
    )
    result["rateLimitHeaders"]["marketCalendar"] = _rate_limit_evidence(calendar_response)
    result["responseSha256"]["marketCalendar"] = _response_sha256(calendar_response)
    result["summary"]["responseSha256Rows"] += 1
    if calendar_status != 200:
        return _mark_failure(
            result,
            status=_failure_status(calendar_status),
            category=f"market_calendar_http_{calendar_status}",
            endpoint_group="MARKET_INFO",
            http_status=calendar_status,
            error_code=_safe_error_code(calendar_response, oauth=False),
        )
    try:
        calendar_payload = calendar_response.json()
    except (TypeError, ValueError) as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=f"market_calendar_json_{type(exc).__name__}",
            endpoint_group="MARKET_INFO",
        )
    safe_calendar, allowed_price_dates = _validate_calendar(calendar_payload, calendar_date)
    if safe_calendar is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="market_calendar_schema_invalid",
            endpoint_group="MARKET_INFO",
        )
    result["calendar"] = {
        "schemaValid": True,
        "marketTimezone": "Asia/Seoul",
        "requestedDate": calendar_date,
        **safe_calendar,
    }

    requested = set(normalized_symbols)
    seen: dict[str, dict[str, Any]] = {}
    returned_canonical_symbols: set[str] = set()
    returned_provider_symbols: set[str] = set()
    invalid_rows = 0
    duplicate_rows = 0
    timestamp_missing_rows = 0
    future_timestamp_rows = 0
    out_of_calendar_date_rows = 0
    future_skew_ms: list[int] = []
    currency_conflicts = 0
    source_timestamps: list[tuple[datetime.datetime, str]] = []
    payload_timestamps: list[datetime.datetime] = []
    payload_to_local_offsets_ms: list[int] = []
    payload_to_http_offsets_ms: list[int] = []
    local_to_http_offsets_ms: list[int] = []
    root_cause_counts = {cause: 0 for cause in CLOCK_ROOT_CAUSES}

    for offset in range(0, len(normalized_symbols), MAX_SYMBOLS_PER_PRICE_REQUEST):
        batch = normalized_symbols[offset : offset + MAX_SYMBOLS_PER_PRICE_REQUEST]
        provider_batch = [canonical_to_provider[symbol] for symbol in batch]
        batch_lineage = {
            "batchIndex": offset // MAX_SYMBOLS_PER_PRICE_REQUEST,
            "requestedCount": len(batch),
            "returnedCount": 0,
            "batchRequestScopeSha256": _symbol_scope_sha256(batch),
            "batchProviderRequestScopeSha256": _symbol_scope_sha256(
                provider_batch
            ),
            "batchReturnedScopeSha256": None,
            "batchCanonicalReturnedScopeSha256": None,
            "missingSymbolSha256": [],
        }
        result["requestLineage"]["batches"].append(batch_lineage)
        price_timing = begin_request()
        if price_timing is None:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_SCHEMA_INVALID",
                category="prices_request_started_at_invalid",
                endpoint_group="MARKET_DATA",
            )
        result["requestCounts"]["prices"] += 1
        try:
            price_response = session.get(
                PRICES_ENDPOINT,
                params={"symbols": ",".join(provider_batch)},
                headers=authorization,
                timeout=20,
            )
        except Exception as exc:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_TRANSIENT_FAILURE",
                category=type(exc).__name__,
                endpoint_group="MARKET_DATA",
            )
        price_clock_result = record_response_received(
            "prices", price_response, price_timing
        )
        if price_clock_result is None:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_SCHEMA_INVALID",
                category="prices_received_at_invalid",
                endpoint_group="MARKET_DATA",
            )
        price_received_at, price_clock_evidence = price_clock_result
        price_clock_evidence.update(
            {
                "payloadTimestampRows": 0,
                "payloadTimestampMin": None,
                "payloadTimestampMax": None,
                "payloadAfterLocalReceiptRows": 0,
                "payloadComparedToHttpDateRows": 0,
                "payloadAfterHttpDateRows": 0,
                "payloadToLocalReceiptMinOffsetMs": None,
                "payloadToLocalReceiptMaxOffsetMs": None,
                "payloadToHttpDateMinOffsetMs": None,
                "payloadToHttpDateMaxOffsetMs": None,
            }
        )
        parsed_http_date = _parse_timestamp(
            price_clock_evidence.get("parsedHttpDateAt")
        )
        local_to_http_offset = price_clock_evidence.get(
            "localToHttpDateOffsetMs"
        )
        if isinstance(local_to_http_offset, int):
            local_to_http_offsets_ms.append(local_to_http_offset)
        batch_payload_timestamps: list[datetime.datetime] = []
        batch_returned_canonical_symbols: set[str] = set()
        batch_returned_provider_symbols: set[str] = set()
        batch_payload_to_local_offsets_ms: list[int] = []
        batch_payload_to_http_offsets_ms: list[int] = []
        price_status = int(getattr(price_response, "status_code", 0) or 0)
        result["httpStatusCategories"]["prices"].append(
            _status_category(price_status)
        )
        result["rateLimitHeaders"]["prices"].append(_rate_limit_evidence(price_response))
        result["responseSha256"]["prices"].append(_response_sha256(price_response))
        result["summary"]["responseSha256Rows"] += 1
        if price_status != 200:
            return _mark_failure(
                result,
                status=_failure_status(price_status),
                category=f"prices_http_{price_status}",
                endpoint_group="MARKET_DATA",
                http_status=price_status,
                error_code=_safe_error_code(price_response, oauth=False),
            )
        try:
            price_payload = price_response.json()
        except (TypeError, ValueError) as exc:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_SCHEMA_INVALID",
                category=f"prices_json_{type(exc).__name__}",
                endpoint_group="MARKET_DATA",
            )
        rows = price_payload.get("result") if isinstance(price_payload, dict) else None
        if not isinstance(rows, list):
            invalid_rows += 1
            continue
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            provider_symbol = row.get("symbol")
            canonical_symbol = (
                provider_to_canonical.get(provider_symbol)
                if isinstance(provider_symbol, str)
                else None
            )
            if canonical_symbol is None:
                invalid_rows += 1
                continue
            if (
                canonical_symbol in returned_canonical_symbols
                or provider_symbol in returned_provider_symbols
            ):
                duplicate_rows += 1
                continue
            returned_canonical_symbols.add(canonical_symbol)
            returned_provider_symbols.add(provider_symbol)
            batch_returned_canonical_symbols.add(canonical_symbol)
            batch_returned_provider_symbols.add(provider_symbol)
            price = _decimal(row.get("lastPrice"))
            currency = row.get("currency")
            if price is None or price <= 0 or not isinstance(currency, str):
                invalid_rows += 1
                continue
            if currency != "USD":
                currency_conflicts += 1
                continue
            timestamp = _parse_timestamp(row.get("timestamp"))
            if timestamp is None:
                timestamp_missing_rows += 1
                root_cause_counts["PAYLOAD_TIMESTAMP_MISSING"] += 1
                continue
            timestamp_utc = timestamp.astimezone(datetime.timezone.utc)
            batch_payload_timestamps.append(timestamp_utc)
            payload_timestamps.append(timestamp_utc)
            payload_to_local = int(
                round((timestamp_utc - price_received_at).total_seconds() * 1000)
            )
            batch_payload_to_local_offsets_ms.append(payload_to_local)
            payload_to_local_offsets_ms.append(payload_to_local)
            payload_to_http: int | None = None
            if parsed_http_date is not None:
                payload_to_http = int(
                    round((timestamp_utc - parsed_http_date).total_seconds() * 1000)
                )
                batch_payload_to_http_offsets_ms.append(payload_to_http)
                payload_to_http_offsets_ms.append(payload_to_http)
            if timestamp > price_received_at:
                future_timestamp_rows += 1
                future_skew_ms.append(
                    max(
                        0,
                        int(round((timestamp - price_received_at).total_seconds() * 1000)),
                    )
                )
                if parsed_http_date is None:
                    root_cause_counts[
                        "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"
                    ] += 1
                elif timestamp_utc <= parsed_http_date:
                    root_cause_counts[
                        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"
                    ] += 1
                else:
                    root_cause_counts[
                        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
                    ] += 1
                continue
            if (
                timestamp.astimezone(ZoneInfo("America/New_York")).date()
                not in allowed_price_dates
            ):
                out_of_calendar_date_rows += 1
                root_cause_counts["CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT"] += 1
                continue
            safe_row = {
                "symbol": canonical_symbol,
                "timestamp": row["timestamp"],
                "lastPrice": format(price, "f"),
                "currency": currency,
                "providerSymbolSha256": hashlib.sha256(
                    provider_symbol.encode("utf-8")
                ).hexdigest(),
                "sourceAsOfUtc": timestamp.astimezone(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            seen[canonical_symbol] = safe_row
            source_timestamps.append((timestamp, row["timestamp"]))

        batch_missing_symbols = sorted(
            set(batch).difference(batch_returned_canonical_symbols)
        )
        batch_lineage.update(
            {
                "returnedCount": len(batch_returned_canonical_symbols),
                "batchReturnedScopeSha256": _symbol_scope_sha256(
                    sorted(batch_returned_provider_symbols)
                ),
                "batchCanonicalReturnedScopeSha256": _symbol_scope_sha256(
                    sorted(batch_returned_canonical_symbols)
                ),
                "missingSymbolSha256": [
                    hashlib.sha256(symbol.encode("utf-8")).hexdigest()
                    for symbol in batch_missing_symbols
                ],
            }
        )

        price_clock_evidence.update(
            {
                "payloadTimestampRows": len(batch_payload_timestamps),
                "payloadTimestampMin": (
                    _utc_iso(min(batch_payload_timestamps))
                    if batch_payload_timestamps
                    else None
                ),
                "payloadTimestampMax": (
                    _utc_iso(max(batch_payload_timestamps))
                    if batch_payload_timestamps
                    else None
                ),
                "payloadAfterLocalReceiptRows": sum(
                    value > 0 for value in batch_payload_to_local_offsets_ms
                ),
                "payloadComparedToHttpDateRows": len(
                    batch_payload_to_http_offsets_ms
                ),
                "payloadAfterHttpDateRows": sum(
                    value > 0 for value in batch_payload_to_http_offsets_ms
                ),
                "payloadToLocalReceiptMinOffsetMs": (
                    min(batch_payload_to_local_offsets_ms)
                    if batch_payload_to_local_offsets_ms
                    else None
                ),
                "payloadToLocalReceiptMaxOffsetMs": (
                    max(batch_payload_to_local_offsets_ms)
                    if batch_payload_to_local_offsets_ms
                    else None
                ),
                "payloadToHttpDateMinOffsetMs": (
                    min(batch_payload_to_http_offsets_ms)
                    if batch_payload_to_http_offsets_ms
                    else None
                ),
                "payloadToHttpDateMaxOffsetMs": (
                    max(batch_payload_to_http_offsets_ms)
                    if batch_payload_to_http_offsets_ms
                    else None
                ),
            }
        )

    missing = requested.difference(seen)
    unreturned_symbols = requested.difference(returned_canonical_symbols)
    root_cause_counts["PARTIAL_SYMBOL_RESPONSE"] += len(unreturned_symbols)
    result["summary"].update(
        {
            "matchedRows": len(seen),
            "missingRows": len(missing),
            "invalidRows": invalid_rows,
            "duplicateRows": duplicate_rows,
        }
    )
    result["diagnostics"] = {
        "timestampMissingRows": timestamp_missing_rows,
        "futureTimestampRows": future_timestamp_rows,
        "outOfCalendarDateRows": out_of_calendar_date_rows,
        "unreturnedSymbolRows": len(unreturned_symbols),
        "staleOrFutureRows": (
            timestamp_missing_rows
            + future_timestamp_rows
            + out_of_calendar_date_rows
        ),
        "currencyConflictRows": currency_conflicts,
        "minFutureSkewMs": min(future_skew_ms) if future_skew_ms else None,
        "maxFutureSkewMs": max(future_skew_ms) if future_skew_ms else None,
    }
    response_clock_rows = _response_clock_rows(result)
    root_cause_precedence = (
        "PAYLOAD_TIMESTAMP_MISSING",
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES",
        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE",
        "HTTP_DATE_REFERENCE_MISSING_OR_INVALID",
        "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT",
        "PARTIAL_SYMBOL_RESPONSE",
    )
    primary_root_cause = next(
        (
            cause
            for cause in root_cause_precedence
            if root_cause_counts[cause] > 0
        ),
        None,
    )
    if root_cause_counts["PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"]:
        clock_reference_status = "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
    elif root_cause_counts["LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"]:
        clock_reference_status = "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"
    elif any(
        row.get("httpDateParseStatus") != "VALID" for row in response_clock_rows
    ):
        clock_reference_status = "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"
    elif response_clock_rows:
        clock_reference_status = "HTTP_DATE_REFERENCE_VALID"
    else:
        clock_reference_status = "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT"
    classified_rows = sum(root_cause_counts.values())
    classifiable_rows = (
        timestamp_missing_rows
        + future_timestamp_rows
        + out_of_calendar_date_rows
        + len(unreturned_symbols)
    )
    result["clockDomainEvidence"]["summary"] = {
        "responseBatchCount": len(response_clock_rows),
        "httpDatePresentResponses": sum(
            row.get("httpDateHeaderPresent") is True for row in response_clock_rows
        ),
        "httpDateValidResponses": sum(
            row.get("httpDateParseStatus") == "VALID" for row in response_clock_rows
        ),
        "httpDateInvalidResponses": sum(
            row.get("httpDateParseStatus") == "INVALID" for row in response_clock_rows
        ),
        "payloadTimestampRows": len(payload_timestamps),
        "payloadTimestampMin": (
            _utc_iso(min(payload_timestamps)) if payload_timestamps else None
        ),
        "payloadTimestampMax": (
            _utc_iso(max(payload_timestamps)) if payload_timestamps else None
        ),
        "payloadAfterLocalReceiptRows": sum(
            value > 0 for value in payload_to_local_offsets_ms
        ),
        "payloadComparedToHttpDateRows": len(payload_to_http_offsets_ms),
        "payloadAfterHttpDateRows": sum(
            value > 0 for value in payload_to_http_offsets_ms
        ),
        "localToHttpDateMinOffsetMs": (
            min(local_to_http_offsets_ms) if local_to_http_offsets_ms else None
        ),
        "localToHttpDateMaxOffsetMs": (
            max(local_to_http_offsets_ms) if local_to_http_offsets_ms else None
        ),
        "payloadToLocalReceiptMinOffsetMs": (
            min(payload_to_local_offsets_ms)
            if payload_to_local_offsets_ms
            else None
        ),
        "payloadToLocalReceiptMaxOffsetMs": (
            max(payload_to_local_offsets_ms)
            if payload_to_local_offsets_ms
            else None
        ),
        "payloadToHttpDateMinOffsetMs": (
            min(payload_to_http_offsets_ms)
            if payload_to_http_offsets_ms
            else None
        ),
        "payloadToHttpDateMaxOffsetMs": (
            max(payload_to_http_offsets_ms)
            if payload_to_http_offsets_ms
            else None
        ),
        "clockReferenceStatus": clock_reference_status,
        "primaryRootCause": primary_root_cause,
        "rootCauseCounts": root_cause_counts,
        "classifiableFailureRows": classifiable_rows,
        "classifiedRootCauseRows": classified_rows,
        "rootCauseCountMatches": classifiable_rows == classified_rows,
        "unknownOrUnclassifiedRows": abs(classifiable_rows - classified_rows),
    }
    if classifiable_rows != classified_rows:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="clock_root_cause_count_mismatch",
            endpoint_group="LOCAL_CONTRACT",
        )
    rate_rows = [
        result["rateLimitHeaders"]["oauth"],
        result["rateLimitHeaders"]["marketCalendar"],
        *result["rateLimitHeaders"]["prices"],
    ]
    result["rateLimitHeadersComplete"] = all(
        isinstance(row, dict) and row.get("requiredHeadersPresent") is True
        for row in rate_rows
    )

    if duplicate_rows or currency_conflicts:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SOURCE_CONFLICT",
            category=(
                "duplicate_symbol_response"
                if duplicate_rows
                else "currency_mismatch"
            ),
            endpoint_group="MARKET_DATA",
        )
    if invalid_rows or not result["rateLimitHeadersComplete"]:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=("prices_schema_invalid" if invalid_rows else "rate_limit_headers_missing"),
            endpoint_group="MARKET_DATA",
        )
    if (
        timestamp_missing_rows
        or future_timestamp_rows
        or out_of_calendar_date_rows
        or missing
    ):
        if timestamp_missing_rows:
            category = "price_timestamp_missing"
        elif future_timestamp_rows:
            category = "price_timestamp_after_response"
        elif out_of_calendar_date_rows:
            category = "price_timestamp_outside_calendar"
        else:
            category = "partial_symbol_response"
        return _mark_failure(
            result,
            status="TOSS_SHADOW_STALE_OR_PARTIAL",
            category=category,
            endpoint_group="MARKET_DATA",
        )

    latest = max(source_timestamps, key=lambda item: item[0])
    result.update(
        {
            "status": PASS_STATUS,
            "sourceAsOf": latest[1],
            "prices": [seen[symbol] for symbol in sorted(seen)],
            "eligible": True,
            "tossEvidenceExcluded": False,
            "circuitBreaker": "CLOSED",
            "safeErrorCategory": None,
            "safeErrorCode": None,
        }
    )
    return result


def _alert_fingerprint(status: str, category: Any, endpoint: Any) -> str:
    return _canonical_sha256(
        {
            "status": status,
            "safeErrorCategory": str(category or "none"),
            "affectedEndpointGroup": str(endpoint or "none"),
        }
    )


def _latest_http_status_category(result: Mapping[str, Any]) -> str:
    direct = result.get("httpStatusCategory")
    if isinstance(direct, str) and direct:
        return direct
    categories = result.get("httpStatusCategories")
    if not isinstance(categories, Mapping):
        return "not_available"
    prices = categories.get("prices")
    if isinstance(prices, list) and prices and isinstance(prices[-1], str):
        return prices[-1]
    for key in ("marketCalendar", "oauth"):
        value = categories.get(key)
        if isinstance(value, str) and value:
            return value
    return "not_available"


def dispatch_toss_shadow_alert(
    result: Mapping[str, Any],
    *,
    previous_status: str | None,
    sent_fingerprints: set[str],
    sender: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    status = str(result.get("status") or "TOSS_SHADOW_SCHEMA_INVALID")
    alert_type: str | None = None
    if status == PASS_STATUS:
        if previous_status in FAILURE_STATUSES:
            alert_type = "TOSS_SHADOW_RECOVERED"
        else:
            return {
                "status": "ALERT_NOT_REQUIRED",
                "alertType": None,
                "alertFingerprint": None,
                "safeErrorCategory": None,
            }
    elif status in FAILURE_STATUSES:
        alert_type = "TOSS_SHADOW_FAILURE"
    else:
        return {
            "status": "ALERT_NOT_REQUIRED",
            "alertType": None,
            "alertFingerprint": None,
            "safeErrorCategory": None,
        }

    fingerprint = _alert_fingerprint(
        alert_type,
        result.get("safeErrorCategory"),
        result.get("affectedEndpointGroup"),
    )
    if fingerprint in sent_fingerprints:
        return {
            "status": "ALERT_SUPPRESSED_DUPLICATE",
            "alertType": alert_type,
            "alertFingerprint": fingerprint,
            "safeErrorCategory": None,
        }
    sent_fingerprints.add(fingerprint)

    counts = result.get("requestCounts") or {}
    http_status_category = _latest_http_status_category(result)
    clock_domain = result.get("clockDomainEvidence")
    clock_summary = (
        clock_domain.get("summary")
        if isinstance(clock_domain, Mapping)
        and isinstance(clock_domain.get("summary"), Mapping)
        else {}
    )
    root_cause_counts = clock_summary.get("rootCauseCounts")
    affected_rows = (
        sum(
            int(value)
            for value in root_cause_counts.values()
            if isinstance(value, int) and value > 0
        )
        if isinstance(root_cause_counts, Mapping)
        else 0
    )
    offset_min = clock_summary.get("payloadToLocalReceiptMinOffsetMs")
    offset_max = clock_summary.get("payloadToLocalReceiptMaxOffsetMs")
    offset_range = (
        f"{offset_min}..{offset_max}"
        if isinstance(offset_min, int) and isinstance(offset_max, int)
        else "not_available"
    )
    request_count_labels = {
        key: str(counts.get(key))
        if isinstance(counts.get(key), int) and counts.get(key) >= 0
        else "unknown"
        for key in ("oauth", "marketCalendar", "prices")
    }
    if alert_type == "TOSS_SHADOW_RECOVERED":
        message = (
            "✅ *Toss SHADOW source recovered*\n"
            f"Status: `{status}`\n"
            "Toss evidence eligible=true\n"
            "canonical analysis continued=true"
        )
    else:
        next_action = (
            "verify hashed missing-symbol lineage and provider symbol mapping; "
            "do not retry full scope"
            if result.get("safeErrorCategory") == "partial_symbol_response"
            else "verify registered egress, credentials, rate limit, and source contract"
        )
        message = (
            "⚠️ *Toss SHADOW source excluded*\n"
            f"Status: `{status}`\n"
            f"Error: `{result.get('safeErrorCategory') or 'unclassified'}`\n"
            f"HTTP: `{http_status_category}`\n"
            f"EndpointGroup: `{result.get('affectedEndpointGroup') or 'unknown'}`\n"
            f"ClockReference: `{clock_summary.get('clockReferenceStatus') or 'not_available'}`\n"
            f"AffectedRows: `{affected_rows}`\n"
            f"OffsetMs: `{offset_range}`\n"
            "Requests: "
            f"oauth={request_count_labels['oauth']}, "
            f"calendar={request_count_labels['marketCalendar']}, "
            f"prices={request_count_labels['prices']}\n"
            "CircuitBreaker: `OPEN_FOR_RUN`\n"
            "Toss evidence excluded=true\n"
            "canonical analysis continued=true\n"
            f"Next: {next_action}"
        )

    try:
        delivery = sender(message, channel="alert") or {}
    except Exception as exc:
        return {
            "status": "ALERT_DELIVERY_FAILED",
            "alertType": alert_type,
            "alertFingerprint": fingerprint,
            "safeErrorCategory": type(exc).__name__,
        }
    if delivery.get("delivered") is True:
        alert_status = "ALERT_DELIVERED"
    elif (
        delivery.get("attempted") is False
        and delivery.get("safeErrorCategory") == "config_missing"
    ):
        alert_status = "ALERT_CONFIG_MISSING"
    else:
        alert_status = "ALERT_DELIVERY_FAILED"
    return {
        "status": alert_status,
        "alertType": alert_type,
        "alertFingerprint": fingerprint,
        "safeErrorCategory": delivery.get("safeErrorCategory"),
        "httpStatusCategory": http_status_category,
    }
