from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo


COLLECTION_WINDOW_BASIS = "AMERICA_NEW_YORK_PUBLICATION_DATE"
COLLECTION_WINDOW_TIMEZONE = ZoneInfo("America/New_York")
PRE_PERSISTENCE_ARTIFACT_HASH_BASIS = (
    "PRE_PERSISTENCE_COLLECTION_EVIDENCE"
)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _parse_timestamp(value: Any) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid_retrieved_at") from exc
    if parsed.utcoffset() is None:
        raise ValueError("retrieved_at_timezone_required")
    return parsed


def build_collection_contract(
    *,
    source_family: str,
    schema_version: str,
    source_ids: Iterable[str],
    source_window_bases: Mapping[str, str],
    retrieved_at: str,
) -> dict[str, Any]:
    family = str(source_family or "").strip()
    schema = str(schema_version or "").strip()
    scope = sorted({str(source_id).strip() for source_id in source_ids if str(source_id).strip()})
    if not family or not schema or not scope:
        raise ValueError("collection_contract_scope_required")
    if set(source_window_bases) != set(scope) or any(
        not str(source_window_bases[source_id] or "").strip()
        for source_id in scope
    ):
        raise ValueError("source_window_contract_required")
    publication_date = _parse_timestamp(retrieved_at).astimezone(
        COLLECTION_WINDOW_TIMEZONE
    ).date().isoformat()
    request_scope_sha256 = canonical_sha256(scope)
    source_windows = {
        source_id: {
            "collectionWindow": (
                "NOT_COLLECTED"
                if str(source_window_bases[source_id]).startswith("EXCLUDED_")
                else publication_date
            ),
            "collectionWindowBasis": str(source_window_bases[source_id]),
        }
        for source_id in scope
    }
    contract = {
        "sourceFamily": family,
        "schemaVersion": schema,
        "collectionWindow": publication_date,
        "collectionWindowBasis": COLLECTION_WINDOW_BASIS,
        "sourceCollectionWindows": source_windows,
        "collectionWindowContractStatus": (
            "VERIFIED_SOURCE_PUBLICATION_OBSERVATION_WINDOWS"
        ),
        "requestScopeSha256": request_scope_sha256,
    }
    return {**contract, "collectionKey": canonical_sha256(contract)}


def classify_existing_artifact(
    artifact: Any,
    contract: Mapping[str, Any],
    *,
    success_statuses: set[str],
) -> str:
    if not isinstance(artifact, Mapping):
        return "COLLECTION_CONTRACT_MISMATCH"
    required_matches = all(
        artifact.get(key) == contract.get(key)
        for key in (
            "sourceFamily",
            "schemaVersion",
            "collectionWindow",
            "collectionWindowBasis",
            "sourceCollectionWindows",
            "collectionWindowContractStatus",
            "requestScopeSha256",
            "collectionKey",
        )
    )
    safe_contract = (
        artifact.get("canonicalSourceChanged") is False
        and artifact.get("policyImpact") == "NONE_REPORT_ONLY"
        and artifact.get("rawResponseStored") is False
        and int(artifact.get("unknownOrUnclassifiedRows", 0) or 0) == 0
        and re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("evidenceSha256") or ""))
        is not None
        and artifact.get("evidenceHashBasis")
        == "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    )
    if not required_matches or not safe_contract:
        return "COLLECTION_CONTRACT_MISMATCH"
    hashed = dict(artifact)
    evidence_sha256 = str(hashed.pop("evidenceSha256"))
    if canonical_sha256(hashed) != evidence_sha256:
        return "COLLECTION_CONTRACT_MISMATCH"
    return (
        "EXISTING_MATCHED_SUCCESS"
        if str(artifact.get("status") or "") in success_statuses
        else "EXISTING_MATCHED_FAILURE"
    )


def _rehash(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("evidenceSha256", None)
    result["evidenceHashBasis"] = "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    result["evidenceSha256"] = canonical_sha256(result)
    return result


def reuse_existing_artifact(
    artifact: Mapping[str, Any],
    *,
    request_counter_names: Iterable[str],
    matched_status: str,
) -> dict[str, Any]:
    if matched_status not in {
        "EXISTING_MATCHED_SUCCESS",
        "EXISTING_MATCHED_FAILURE",
    }:
        raise ValueError("matched_artifact_status_required")
    result = json.loads(json.dumps(dict(artifact), ensure_ascii=True))
    source_hash = str(result.get("sourceEvidenceSha256") or result.get("evidenceSha256") or "")
    zero_counts = {name: 0 for name in sorted(set(request_counter_names))}
    result.update(
        {
            "sourceEvidenceSha256": source_hash if re.fullmatch(r"[0-9a-f]{64}", source_hash) else None,
            "runtimeAction": (
                "EXISTING_MATCHED_SHADOW_REUSED"
                if matched_status == "EXISTING_MATCHED_SUCCESS"
                else "EXISTING_FAILED_SHADOW_PRESERVED"
            ),
            "existingArtifactReuseStatus": matched_status,
            "requestCounts": zero_counts,
            "thisRunRequestCounts": zero_counts,
            "externalRequestCount": 0,
            "thisRunExternalRequestCount": 0,
            "requestBudgetCompliant": True,
        }
    )
    return _rehash(result)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sentinel_path(
    directory: Path,
    source_family: str,
    collection_key: str,
) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", collection_key) is None:
        raise ValueError("invalid_collection_key")
    safe_family = re.sub(r"[^a-z0-9]+", "-", source_family.lower()).strip("-")
    if not safe_family:
        raise ValueError("invalid_source_family")
    return directory / f"{safe_family}-{collection_key}.json"


def durable_collection_sentinel_filename(contract: Mapping[str, Any]) -> str:
    collection_key = str(contract.get("collectionKey") or "")
    if re.fullmatch(r"[0-9a-f]{64}", collection_key) is None:
        raise ValueError("invalid_collection_key")
    safe_family = re.sub(
        r"[^A-Z0-9]+",
        "_",
        str(contract.get("sourceFamily") or "").upper(),
    ).strip("_")
    if not safe_family:
        raise ValueError("invalid_source_family")
    return f"OFFICIAL_SHADOW_SENTINEL_{safe_family}_{collection_key}.json"


def build_durable_collection_sentinel(
    contract: Mapping[str, Any],
    *,
    status: str,
    reserved_at: str,
    completed_at: str | None = None,
    artifact_sha256: str | None = None,
    request_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    if status not in {"IN_PROGRESS", "COMPLETE", "FAILED"}:
        raise ValueError("invalid_sentinel_status")
    _parse_timestamp(reserved_at)
    if status != "IN_PROGRESS":
        _parse_timestamp(completed_at)
    if artifact_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None:
        raise ValueError("invalid_artifact_sha256")
    payload = {
        "schemaVersion": "official-shadow-durable-sentinel-v1",
        "status": status,
        "sourceFamily": contract.get("sourceFamily"),
        "sourceSchemaVersion": contract.get("schemaVersion"),
        "collectionWindow": contract.get("collectionWindow"),
        "collectionWindowBasis": contract.get("collectionWindowBasis"),
        "sourceCollectionWindows": contract.get("sourceCollectionWindows"),
        "collectionWindowContractStatus": contract.get(
            "collectionWindowContractStatus"
        ),
        "requestScopeSha256": contract.get("requestScopeSha256"),
        "collectionKey": contract.get("collectionKey"),
        "reservedAt": reserved_at,
        "completedAt": completed_at,
        "artifactSha256": artifact_sha256,
        "artifactHashBasis": (
            PRE_PERSISTENCE_ARTIFACT_HASH_BASIS
            if artifact_sha256 is not None
            else None
        ),
        "requestCounts": {
            key: int(value)
            for key, value in sorted((request_counts or {}).items())
        },
        "rawResponseStored": False,
        "secretValuesStoredOrPrinted": False,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "brokerOrSidecarStateMutation": False,
        "sentinelHashBasis": "CANONICAL_JSON_WITHOUT_SENTINEL_HASH",
    }
    payload["sentinelSha256"] = canonical_sha256(payload)
    return payload


def classify_durable_collection_sentinel(
    sentinel: Any,
    contract: Mapping[str, Any],
) -> str:
    if not isinstance(sentinel, Mapping):
        return "DURABLE_SENTINEL_MISSING"
    hashed = dict(sentinel)
    sentinel_sha256 = str(hashed.pop("sentinelSha256", ""))
    artifact_sha256 = sentinel.get("artifactSha256")
    artifact_hash_basis = sentinel.get("artifactHashBasis")
    terminal_artifact_hash_valid = (
        sentinel.get("status") == "IN_PROGRESS"
        and artifact_sha256 is None
        and artifact_hash_basis is None
    ) or (
        sentinel.get("status") in {"COMPLETE", "FAILED"}
        and re.fullmatch(r"[0-9a-f]{64}", str(artifact_sha256 or ""))
        is not None
        and artifact_hash_basis
        in {None, PRE_PERSISTENCE_ARTIFACT_HASH_BASIS}
    )
    valid = (
        sentinel.get("schemaVersion") == "official-shadow-durable-sentinel-v1"
        and sentinel.get("sourceFamily") == contract.get("sourceFamily")
        and sentinel.get("sourceSchemaVersion") == contract.get("schemaVersion")
        and sentinel.get("collectionWindow") == contract.get("collectionWindow")
        and sentinel.get("collectionWindowBasis")
        == contract.get("collectionWindowBasis")
        and sentinel.get("sourceCollectionWindows")
        == contract.get("sourceCollectionWindows")
        and sentinel.get("collectionWindowContractStatus")
        == contract.get("collectionWindowContractStatus")
        and sentinel.get("requestScopeSha256")
        == contract.get("requestScopeSha256")
        and sentinel.get("collectionKey") == contract.get("collectionKey")
        and sentinel.get("rawResponseStored") is False
        and sentinel.get("canonicalSourceChanged") is False
        and sentinel.get("policyImpact") == "NONE_REPORT_ONLY"
        and sentinel.get("brokerOrSidecarStateMutation") is False
        and terminal_artifact_hash_valid
        and sentinel.get("sentinelHashBasis")
        == "CANONICAL_JSON_WITHOUT_SENTINEL_HASH"
        and re.fullmatch(r"[0-9a-f]{64}", sentinel_sha256) is not None
        and canonical_sha256(hashed) == sentinel_sha256
    )
    if not valid:
        return "DURABLE_SENTINEL_INVALID"
    return {
        "IN_PROGRESS": "EXISTING_IN_PROGRESS",
        "COMPLETE": "EXISTING_COMPLETE",
        "FAILED": "EXISTING_FAILED",
    }.get(str(sentinel.get("status") or ""), "DURABLE_SENTINEL_INVALID")


def reserve_collection_sentinel(
    directory: Path,
    *,
    source_family: str,
    collection_key: str,
    reserved_at: str,
) -> dict[str, Any]:
    _parse_timestamp(reserved_at)
    path = _sentinel_path(directory, source_family, collection_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": "official-shadow-collection-sentinel-v1",
        "status": "IN_PROGRESS",
        "sourceFamily": source_family,
        "collectionKey": collection_key,
        "reservedAt": reserved_at,
    }
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"status": "EXISTING_SENTINEL_INVALID", "path": str(path)}
        status = str(existing.get("status") or "") if isinstance(existing, Mapping) else ""
        return {
            "status": {
                "IN_PROGRESS": "EXISTING_IN_PROGRESS",
                "COMPLETE": "EXISTING_COMPLETE",
                "FAILED": "EXISTING_FAILED",
            }.get(status, "EXISTING_SENTINEL_INVALID"),
            "path": str(path),
        }
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")
    return {"status": "RESERVED", "path": str(path)}


def finish_collection_sentinel(
    path: Path,
    *,
    status: str,
    completed_at: str,
    artifact_sha256: str | None,
    request_counts: Mapping[str, int],
) -> None:
    if status not in {"COMPLETE", "FAILED"}:
        raise ValueError("invalid_sentinel_terminal_status")
    _parse_timestamp(completed_at)
    existing = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(existing, Mapping) or existing.get("status") != "IN_PROGRESS":
        raise ValueError("sentinel_not_in_progress")
    if artifact_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None:
        raise ValueError("invalid_artifact_sha256")
    _atomic_write_json(
        path,
        {
            **dict(existing),
            "status": status,
            "completedAt": completed_at,
            "artifactSha256": artifact_sha256,
            "artifactHashBasis": (
                PRE_PERSISTENCE_ARTIFACT_HASH_BASIS
                if artifact_sha256 is not None
                else None
            ),
            "requestCounts": {
                key: int(value) for key, value in sorted(request_counts.items())
            },
        },
    )


def persist_shadow_artifact(
    payload: Mapping[str, Any],
    *,
    local_path: str,
    current_filename: str,
    archive_prefix: str,
    parent_id: str,
    writer: Callable[[str, dict[str, Any], str], None],
    uploader: Callable[[str, dict[str, Any], str], None],
) -> dict[str, Any]:
    result = dict(payload)
    result.update(
        artifactPersistenceStatus="LOCAL_AND_DRIVE_PUBLISHED",
        artifactPersistenceErrorCategory=None,
    )
    result = _rehash(result)
    safe_status = re.sub(
        r"[^A-Z0-9_]+",
        "_",
        str(result.get("status") or "UNKNOWN").upper(),
    )[:48]
    collection_key = str(result.get("collectionKey") or "")
    if re.fullmatch(r"[0-9a-f]{64}", collection_key) is None:
        raise ValueError("collection_key_required_for_archive")
    archive_filename = f"{archive_prefix}_{safe_status}_{collection_key[:16]}.json"
    result["archiveFilename"] = archive_filename
    result = _rehash(result)
    try:
        writer(local_path, result, f"{archive_prefix} shadow evidence")
    except Exception as exc:
        result.update(
            artifactPersistenceStatus="LOCAL_WRITE_FAILED",
            artifactPersistenceErrorCategory=type(exc).__name__,
        )
        return _rehash(result)
    try:
        uploader(archive_filename, result, parent_id)
        uploader(current_filename, result, parent_id)
    except Exception as exc:
        result.update(
            artifactPersistenceStatus="DRIVE_PUBLISH_FAILED",
            artifactPersistenceErrorCategory=type(exc).__name__,
        )
        result = _rehash(result)
        try:
            writer(local_path, result, f"{archive_prefix} Drive publish failure")
        except Exception:
            result["artifactPersistenceStatus"] = "LOCAL_REWRITE_AFTER_DRIVE_FAILURE_FAILED"
            result = _rehash(result)
        return result
    return result


def dispatch_official_shadow_alert(
    result: Mapping[str, Any],
    *,
    success_statuses: set[str],
    sent_fingerprints: set[str],
    sender: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    status = str(result.get("status") or "SHADOW_STATUS_INVALID")
    if status in success_statuses:
        return {
            "status": "ALERT_NOT_REQUIRED",
            "alertFingerprint": None,
            "attempted": False,
            "delivered": False,
            "duplicateSuppressed": False,
            "safeErrorCategory": None,
        }
    fingerprint = canonical_sha256(
        {
            "sourceFamily": str(result.get("sourceFamily") or "unknown"),
            "collectionKey": str(result.get("collectionKey") or "unknown"),
            "status": status,
            "primaryBlocker": str(result.get("primaryBlocker") or "unclassified"),
        }
    )
    if fingerprint in sent_fingerprints:
        return {
            "status": "ALERT_SUPPRESSED_DUPLICATE",
            "alertFingerprint": fingerprint,
            "attempted": False,
            "delivered": False,
            "duplicateSuppressed": True,
            "safeErrorCategory": None,
        }
    sent_fingerprints.add(fingerprint)
    external_request_count = result.get("externalRequestCount")
    safe_request_count = (
        str(external_request_count)
        if isinstance(external_request_count, int)
        and external_request_count >= 0
        else "unverified"
    )
    message = (
        "Warning: official SHADOW source excluded\n"
        f"SourceGroup: `{result.get('sourceFamily') or 'unknown'}`\n"
        f"Status: `{status}`\n"
        f"Error: `{result.get('primaryBlocker') or 'unclassified'}`\n"
        f"Requests: `{safe_request_count}`\n"
        "SHADOW evidence excluded=true\n"
        "canonical analysis continued=true"
    )
    try:
        delivery = sender(message, channel="alert") or {}
    except Exception as exc:
        return {
            "status": "ALERT_DELIVERY_FAILED",
            "alertFingerprint": fingerprint,
            "attempted": True,
            "delivered": False,
            "duplicateSuppressed": False,
            "safeErrorCategory": type(exc).__name__,
        }
    attempted = delivery.get("attempted") is True
    delivered = delivery.get("delivered") is True
    if delivered:
        alert_status = "ALERT_DELIVERED"
    elif not attempted and delivery.get("safeErrorCategory") == "config_missing":
        alert_status = "ALERT_CONFIG_MISSING"
    else:
        alert_status = "ALERT_DELIVERY_FAILED"
    return {
        "status": alert_status,
        "alertFingerprint": fingerprint,
        "attempted": attempted,
        "delivered": delivered,
        "duplicateSuppressed": False,
        "safeErrorCategory": delivery.get("safeErrorCategory"),
    }
