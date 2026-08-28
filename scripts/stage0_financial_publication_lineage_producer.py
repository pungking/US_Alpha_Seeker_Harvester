from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import os
import re
import shutil
import string
import sys
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from official_shadow_runtime import (
    _atomic_write_json,
    _sentinel_path,
    canonical_sha256,
    finish_collection_sentinel,
    persist_shadow_artifact,
    reserve_collection_sentinel,
)
from run_financial_publication_lineage_bulk_probe import (
    ENDPOINTS,
    REQUEST_BUDGETS,
    _exact_decimal,
    _parse_utc,
    _ten_digit_cik,
    build_ticker_cik_index,
)
from sec_finra_shadow_evidence import _BoundedClient, _submission_lineage


PRODUCER_APPROVAL = "AUTHORIZE STAGE0 SEC FINANCIAL LINEAGE PRODUCER BOUNDED ONE-SHOT"
PRODUCER_RECURRING_APPROVAL = (
    "AUTHORIZE STAGE0 SEC FINANCIAL LINEAGE PRODUCER RECURRING SECOND-BATCH"
)
PRODUCER_SCHEMA_VERSION = "stage0-sec-financial-publication-lineage-v1"
PRODUCER_SOURCE_FAMILY = "STAGE0_SEC_FINANCIAL_PUBLICATION_LINEAGE"
PRODUCER_PASS_STATUS = "STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_PASS"
PRODUCER_CURRENT_FILENAME = "STAGE0_SEC_FINANCIAL_PUBLICATION_LINEAGE.json"
PRODUCER_COLLECTION_KEY = canonical_sha256(
    {
        "approval": PRODUCER_APPROVAL,
        "requestScope": sorted(REQUEST_BUDGETS),
        "schemaVersion": PRODUCER_SCHEMA_VERSION,
        "sourceFamily": PRODUCER_SOURCE_FAMILY,
    }
)

ELIGIBLE_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "20-F",
    "20-F/A",
    "40-F",
    "40-F/A",
    "6-K",
    "6-K/A",
}
ACCEPTED_IDENTITY_STATUSES = {
    "IDENTIFIER_LINEAGE_VERIFIED",
    "IDENTIFIER_ALIAS_RESOLVED",
}
SOURCE_METRIC_CONCEPTS = {
    "Net Income": (("us-gaap", "NetIncomeLoss"), ("ifrs-full", "ProfitLoss")),
    "Net Income Common Stockholders": (
        ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
        ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
    ),
    "Net Income Including Noncontrolling Interests": (
        ("us-gaap", "NetIncomeLoss"),
        ("ifrs-full", "ProfitLoss"),
    ),
}
CLASSIFICATIONS = {
    "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL",
    "FINANCIAL_LINEAGE_VERIFIED_AMENDMENT",
    "FINANCIAL_LINEAGE_DUPLICATE_SAME_ACCESSION_COLLAPSED",
    "FINANCIAL_LINEAGE_MULTIPLE_ACCESSIONS_AMBIGUOUS",
    "FINANCIAL_LINEAGE_FACT_NOT_FOUND",
    "FINANCIAL_LINEAGE_SUBMISSION_MISSING",
    "FINANCIAL_LINEAGE_FORM_MISMATCH",
    "FINANCIAL_LINEAGE_IDENTITY_INVALID",
    "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED",
    "FINANCIAL_LINEAGE_NOT_APPLICABLE",
}
VERIFIED_STATUSES = {
    "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL",
    "FINANCIAL_LINEAGE_VERIFIED_AMENDMENT",
    "FINANCIAL_LINEAGE_DUPLICATE_SAME_ACCESSION_COLLAPSED",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _valid_sha256(value: Any) -> bool:
    return SHA256_RE.fullmatch(str(value or "")) is not None


def _collection_window(value: Any) -> str:
    text = str(value or "")
    if DATE_RE.fullmatch(text) is None:
        raise ValueError("recurring_collection_window_invalid")
    try:
        if dt.date.fromisoformat(text).isoformat() != text:
            raise ValueError
    except ValueError as exc:
        raise ValueError("recurring_collection_window_invalid") from exc
    return text


def recurring_collection_key(collection_window: str) -> str:
    normalized_window = _collection_window(collection_window)
    return canonical_sha256(
        {
            "activationMode": "RECURRING_SECOND_BATCH",
            "collectionWindow": normalized_window,
            "requestScope": sorted(REQUEST_BUDGETS),
            "schemaVersion": PRODUCER_SCHEMA_VERSION,
            "sourceFamily": PRODUCER_SOURCE_FAMILY,
        }
    )


def _source_group(file_name: str, suffix: str) -> str | None:
    match = re.fullmatch(rf"([A-Z])_stocks_{suffix}\.json", str(file_name or ""))
    return match.group(1) if match else None


def _file_index(files: list[Mapping[str, Any]], suffix: str) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for source_file in files:
        group = _source_group(str(source_file.get("fileName") or ""), suffix)
        if group is None or group in indexed or not _valid_sha256(source_file.get("contentSha256")):
            raise ValueError(f"invalid_or_duplicate_{suffix}_source_file")
        if not isinstance(source_file.get("payload"), Mapping):
            raise ValueError(f"invalid_{suffix}_source_payload")
        indexed[group] = source_file
    return indexed


def _history_rows(entry: Any) -> list[Mapping[str, Any]]:
    if not isinstance(entry, Mapping):
        return []
    if isinstance(entry.get("financials"), list):
        rows = [row for row in entry["financials"] if isinstance(row, Mapping)]
    else:
        rows = []
        for key, value in entry.items():
            if str(key).startswith("_") or not isinstance(value, Mapping):
                continue
            rows.append({**dict(value), "date": value.get("date") or key})
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("date") or ""),
            1 if str(row.get("_periodType") or "").upper() == "QUARTERLY" else 0,
        ),
        reverse=True,
    )


def _identity_for_symbol(identity_map: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    row = identity_map.get(symbol)
    if not isinstance(row, Mapping):
        return {
            "effectiveSymbol": symbol,
            "sourceSymbol": symbol,
            "identifierLineageStatus": "IDENTIFIER_LINEAGE_MISSING",
        }
    effective = str(row.get("symbol") or "").strip().upper()
    source = str(row.get("sourceSymbol") or symbol).strip().upper()
    if source != symbol or not effective:
        status = "IDENTIFIER_LINEAGE_AMBIGUOUS"
    elif effective == symbol:
        status = "IDENTIFIER_LINEAGE_VERIFIED"
    else:
        status = "IDENTIFIER_ALIAS_RESOLVED"
    return {
        "effectiveSymbol": effective or symbol,
        "sourceSymbol": source or symbol,
        "identifierLineageStatus": status,
    }


def build_financial_input_rows(
    *,
    identity_map: Mapping[str, Any],
    identity_map_sha256: str,
    daily_files: list[Mapping[str, Any]],
    history_files: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not _valid_sha256(identity_map_sha256):
        raise ValueError("identity_map_sha256_required")
    daily_index = _file_index(daily_files, "daily")
    history_index = _file_index(history_files, "history")
    if set(daily_index) != set(history_index):
        raise ValueError("daily_history_source_groups_mismatch")

    result: list[dict[str, Any]] = []
    for group in sorted(daily_index):
        daily_file = daily_index[group]
        history_file = history_index[group]
        daily_payload = daily_file["payload"]
        history_payload = history_file["payload"]
        for key, daily_row in sorted(daily_payload.items()):
            if str(key).startswith("_"):
                continue
            if not isinstance(daily_row, Mapping):
                raise ValueError("invalid_stage0_daily_row")
            symbol = str(daily_row.get("symbol") or key).strip().upper()
            identity = _identity_for_symbol(identity_map, symbol)
            record: dict[str, Any] = {
                "identity": identity,
                "identityMapSha256": identity_map_sha256,
                "financialMetricBasis": "NET_INCOME",
                "sourceMetricLabel": None,
                "value": daily_row.get("netIncome"),
                "fiscalPeriod": str(daily_row.get("netIncomeAsOf") or "").strip() or None,
                "sourceDailyFile": daily_file["fileName"],
                "sourceDailyFileSha256": daily_file["contentSha256"],
                "sourceHistoryFile": history_file["fileName"],
                "sourceHistoryFileSha256": history_file["contentSha256"],
                "inputStatus": "READY_FOR_EXACT_SEC_LINEAGE",
            }
            if str(daily_row.get("netIncomeSource") or "").upper() != "HISTORY":
                record["inputStatus"] = "FINANCIAL_LINEAGE_NOT_APPLICABLE"
                result.append(record)
                continue
            fiscal_period = str(record.get("fiscalPeriod") or "")
            value = _exact_decimal(record.get("value"))
            selected_label: str | None = None
            if value is not None and fiscal_period:
                history_entry = history_payload.get(symbol)
                for history_row in _history_rows(history_entry):
                    if str(history_row.get("date") or "") != fiscal_period:
                        continue
                    for label in SOURCE_METRIC_CONCEPTS:
                        if _exact_decimal(history_row.get(label)) == value:
                            selected_label = label
                            break
                    if selected_label is not None:
                        break
            if selected_label is None:
                record["inputStatus"] = "FINANCIAL_LINEAGE_FACT_NOT_FOUND"
            else:
                record["sourceMetricLabel"] = selected_label
            result.append(record)
    return result


def _exact_candidates(
    companyfacts: Any,
    *,
    source_metric_label: str,
    value: Any,
    fiscal_period: str,
) -> tuple[list[dict[str, Any]], bool]:
    concepts = SOURCE_METRIC_CONCEPTS.get(source_metric_label, ())
    decimal_value = _exact_decimal(value)
    facts = companyfacts.get("facts") if isinstance(companyfacts, Mapping) else None
    if not concepts or decimal_value is None or not isinstance(facts, Mapping):
        return [], False
    candidates: list[dict[str, Any]] = []
    exact_but_form_invalid = False
    for taxonomy, concept in concepts:
        concept_row = ((facts.get(taxonomy) or {}).get(concept) or {}) if isinstance(facts.get(taxonomy), Mapping) else {}
        units = concept_row.get("units") if isinstance(concept_row, Mapping) else None
        if not isinstance(units, Mapping):
            continue
        for unit, rows in sorted(units.items()):
            if not str(unit or "") or not isinstance(rows, list):
                continue
            for row in rows:
                if (
                    not isinstance(row, Mapping)
                    or _exact_decimal(row.get("val")) != decimal_value
                    or str(row.get("end") or "") != fiscal_period
                ):
                    continue
                form = str(row.get("form") or "")
                if form not in ELIGIBLE_FORMS:
                    exact_but_form_invalid = True
                    continue
                if not str(row.get("start") or "") or not str(row.get("accn") or ""):
                    continue
                candidates.append(
                    {
                        "taxonomy": taxonomy,
                        "concept": concept,
                        "unit": str(unit),
                        "value": row.get("val"),
                        "periodStart": str(row["start"]),
                        "periodEnd": fiscal_period,
                        "form": form,
                        "accessionNumber": str(row["accn"]),
                    }
                )
    return candidates, exact_but_form_invalid


def _classification_only(record: Mapping[str, Any], classification: str) -> dict[str, Any]:
    return {**dict(record), "classification": classification}


def _candidate_submission_lineage(
    candidate: Mapping[str, Any],
    submissions: Any,
    retrieved: dt.datetime,
) -> dict[str, Any]:
    submission = _submission_lineage(submissions, str(candidate["accessionNumber"]))
    published_at = _parse_utc(submission.get("publishedAt"))
    if submission.get("status") != "ACCESSION_MATCHED" or published_at is None:
        status = "FINANCIAL_LINEAGE_SUBMISSION_MISSING"
    elif submission.get("observedForm") != candidate["form"]:
        status = "FINANCIAL_LINEAGE_FORM_MISMATCH"
    elif published_at > retrieved:
        status = "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED"
    else:
        status = "CANDIDATE_PUBLICATION_LINEAGE_VERIFIED"
    return {
        "accessionNumber": candidate["accessionNumber"],
        "form": candidate["form"],
        "amendmentStatus": (
            "AMENDMENT" if str(candidate["form"]).endswith("/A") else "ORIGINAL"
        ),
        "financialPublishedAt": _utc_iso(published_at) if published_at is not None else None,
        "status": status,
    }


def classify_financial_lineage(
    *,
    record: Mapping[str, Any],
    ticker_cik_index: Mapping[str, tuple[str, ...]],
    companyfacts: Any,
    submissions: Any,
    retrieved_at: str,
    source_response_hashes: Mapping[str, str],
) -> dict[str, Any]:
    input_status = str(record.get("inputStatus") or "READY_FOR_EXACT_SEC_LINEAGE")
    if input_status in {
        "FINANCIAL_LINEAGE_NOT_APPLICABLE",
        "FINANCIAL_LINEAGE_FACT_NOT_FOUND",
    }:
        return _classification_only(record, input_status)

    identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
    identity_status = str(identity.get("identifierLineageStatus") or "")
    effective_symbol = str(identity.get("effectiveSymbol") or "").strip().upper()
    ciks = ticker_cik_index.get(effective_symbol, ())
    if identity_status not in ACCEPTED_IDENTITY_STATUSES or len(ciks) != 1:
        return _classification_only(record, "FINANCIAL_LINEAGE_IDENTITY_INVALID")
    cik = ciks[0]
    if (
        _ten_digit_cik(companyfacts.get("cik") if isinstance(companyfacts, Mapping) else None) != cik
        or _ten_digit_cik(submissions.get("cik") if isinstance(submissions, Mapping) else None) != cik
    ):
        return _classification_only(record, "FINANCIAL_LINEAGE_IDENTITY_INVALID")

    candidates, exact_but_form_invalid = _exact_candidates(
        companyfacts,
        source_metric_label=str(record.get("sourceMetricLabel") or ""),
        value=record.get("value"),
        fiscal_period=str(record.get("fiscalPeriod") or ""),
    )
    if not candidates:
        return _classification_only(
            record,
            "FINANCIAL_LINEAGE_FORM_MISMATCH"
            if exact_but_form_invalid
            else "FINANCIAL_LINEAGE_FACT_NOT_FOUND",
        )

    canonical_candidates = {
        canonical_sha256(candidate): candidate for candidate in candidates
    }
    unique = sorted(
        canonical_candidates.values(),
        key=lambda candidate: (
            candidate["accessionNumber"],
            candidate["form"],
            candidate["taxonomy"],
            candidate["concept"],
            candidate["unit"],
        ),
    )
    accessions = {candidate["accessionNumber"] for candidate in unique}
    retrieved = _parse_utc(retrieved_at)
    if retrieved is None:
        raise ValueError("retrieved_at_utc_required")
    if len(unique) != 1 or len(accessions) != 1:
        candidate_lineage = [
            _candidate_submission_lineage(candidate, submissions, retrieved)
            for candidate in unique
        ]
        if any(
            row["status"] == "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED"
            for row in candidate_lineage
        ):
            classification = "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED"
        else:
            classification = "FINANCIAL_LINEAGE_MULTIPLE_ACCESSIONS_AMBIGUOUS"
        return {
            **dict(record),
            "classification": classification,
            "candidateRows": len(candidates),
            "uniqueCandidateRows": len(unique),
            "candidateScopeSha256": canonical_sha256(unique),
            "candidateLineage": candidate_lineage,
            "candidateLineageScopeSha256": canonical_sha256(candidate_lineage),
        }

    candidate = unique[0]
    candidate_lineage = _candidate_submission_lineage(candidate, submissions, retrieved)
    if candidate_lineage["status"] == "FINANCIAL_LINEAGE_SUBMISSION_MISSING":
        return _classification_only(record, "FINANCIAL_LINEAGE_SUBMISSION_MISSING")
    if candidate_lineage["status"] == "FINANCIAL_LINEAGE_FORM_MISMATCH":
        return _classification_only(record, "FINANCIAL_LINEAGE_FORM_MISMATCH")
    if candidate_lineage["status"] == "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED":
        return _classification_only(
            record, "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED"
        )
    published_at = _parse_utc(candidate_lineage["financialPublishedAt"])
    if published_at is None:
        return _classification_only(record, "FINANCIAL_LINEAGE_SUBMISSION_MISSING")

    duplicate_rows = len(candidates) - len(unique)
    classification = (
        "FINANCIAL_LINEAGE_DUPLICATE_SAME_ACCESSION_COLLAPSED"
        if duplicate_rows
        else (
            "FINANCIAL_LINEAGE_VERIFIED_AMENDMENT"
            if candidate["form"].endswith("/A")
            else "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL"
        )
    )
    published_iso = _utc_iso(published_at)
    retrieved_iso = _utc_iso(retrieved)
    hash_basis = {
        "cik": cik,
        **candidate,
        "acceptanceDateTime": published_iso,
        "retrievedAt": retrieved_iso,
        "sourceResponseHashes": dict(sorted(source_response_hashes.items())),
    }
    return {
        **dict(record),
        "classification": classification,
        "financialSource": "YFINANCE_HISTORY_SEC_EDGAR_EXACT_LINEAGE",
        "financialMetricBasis": {
            "metric": str(record.get("financialMetricBasis") or ""),
            "sourceLabel": str(record.get("sourceMetricLabel") or ""),
            "taxonomy": candidate["taxonomy"],
            "concept": candidate["concept"],
            "unit": candidate["unit"],
        },
        "fiscalPeriod": {
            "start": candidate["periodStart"],
            "end": candidate["periodEnd"],
        },
        "form": candidate["form"],
        "accessionNumber": candidate["accessionNumber"],
        "tenDigitCik": cik,
        "financialPublishedAt": published_iso,
        "financialRetrievedAt": retrieved_iso,
        "amendmentStatus": (
            "AMENDMENT" if candidate["form"].endswith("/A") else "ORIGINAL"
        ),
        "collapsedDuplicateRows": duplicate_rows,
        "financialSourceRecordHashBasis": hash_basis,
        "financialSourceRecordSha256": canonical_sha256(hash_basis),
    }


def _load_payload(source: Any, cik: str) -> Any:
    return source(cik) if callable(source) else source.get(cik, {})


def build_lineage_artifact(
    *,
    records: list[Mapping[str, Any]],
    ticker_cik_index: Mapping[str, tuple[str, ...]],
    companyfacts_by_cik: Any,
    submissions_by_cik: Any,
    retrieved_at: str,
    source_files: list[Mapping[str, Any]],
    identity_map_sha256: str,
    source_response_hashes: Mapping[str, str],
    request_counts: Mapping[str, int],
    collection_window: str | None = None,
    collection_key: str | None = None,
    recurring_activation_authorized: bool = False,
) -> dict[str, Any]:
    retrieved = _parse_utc(retrieved_at)
    if retrieved is None:
        raise ValueError("retrieved_at_utc_required")
    effective_collection_window = (
        _collection_window(collection_window)
        if collection_window is not None
        else _utc_iso(retrieved)[:10]
    )
    if recurring_activation_authorized:
        expected_collection_key = recurring_collection_key(effective_collection_window)
        if collection_key != expected_collection_key:
            raise ValueError("recurring_collection_key_invalid")
        effective_collection_key = expected_collection_key
    else:
        if collection_window is not None or collection_key is not None:
            raise ValueError("one_shot_collection_override_invalid")
        effective_collection_key = None
    if not _valid_sha256(identity_map_sha256):
        raise ValueError("identity_map_sha256_required")
    inventory = sorted(
        (
            {
                "fileName": str(row.get("fileName") or ""),
                "sourceKind": str(row.get("sourceKind") or ""),
                "contentSha256": str(row.get("contentSha256") or ""),
                "hashBasis": "CANONICAL_JSON_DOWNLOADED_FROM_DRIVE",
            }
            for row in source_files
        ),
        key=lambda row: (row["sourceKind"], row["fileName"]),
    )
    if len({(row["sourceKind"], row["fileName"]) for row in inventory}) != len(inventory):
        raise ValueError("duplicate_source_file_identity")
    if any(not row["fileName"] or not _valid_sha256(row["contentSha256"]) for row in inventory):
        raise ValueError("invalid_source_file_inventory")
    response_hashes = dict(sorted(source_response_hashes.items()))
    if set(response_hashes) != set(REQUEST_BUDGETS) or any(
        not _valid_sha256(value) for value in response_hashes.values()
    ):
        raise ValueError("source_response_hashes_invalid")

    ordered_records = sorted(
        (dict(row) for row in records),
        key=lambda row: (
            str((row.get("identity") or {}).get("effectiveSymbol") or ""),
            str(row.get("sourceDailyFile") or ""),
            str(row.get("fiscalPeriod") or ""),
        ),
    )
    lineage_rows: list[dict[str, Any]] = []
    for record in ordered_records:
        identity = record.get("identity") if isinstance(record.get("identity"), Mapping) else {}
        symbol = str(identity.get("effectiveSymbol") or "").upper()
        ciks = ticker_cik_index.get(symbol, ())
        cik = ciks[0] if len(ciks) == 1 else ""
        lineage_rows.append(
            classify_financial_lineage(
                record=record,
                ticker_cik_index=ticker_cik_index,
                companyfacts=_load_payload(companyfacts_by_cik, cik) if cik else {},
                submissions=_load_payload(submissions_by_cik, cik) if cik else {},
                retrieved_at=retrieved_at,
                source_response_hashes=response_hashes,
            )
        )

    classification_counts: dict[str, int] = {}
    for row in lineage_rows:
        status = str(row.get("classification") or "")
        classification_counts[status] = classification_counts.get(status, 0) + 1
    unknown = sum(status not in CLASSIFICATIONS for status in classification_counts)
    verified = sum(classification_counts.get(status, 0) for status in VERIFIED_STATUSES)
    not_applicable = classification_counts.get("FINANCIAL_LINEAGE_NOT_APPLICABLE", 0)
    ambiguous = classification_counts.get(
        "FINANCIAL_LINEAGE_MULTIPLE_ACCESSIONS_AMBIGUOUS", 0
    )
    unresolved = len(lineage_rows) - verified - not_applicable
    counts = {key: int(request_counts.get(key, 0)) for key in sorted(REQUEST_BUDGETS)}
    request_budget_compliant = all(
        counts[key] <= REQUEST_BUDGETS[key] for key in REQUEST_BUDGETS
    )
    request_budget_exact = counts == REQUEST_BUDGETS
    source_inventory_sha256 = canonical_sha256(inventory)
    input_hash = canonical_sha256(
        {
            "identityMapSha256": identity_map_sha256,
            "records": ordered_records,
            "sourceFileHashes": inventory,
            "sourceResponseHashes": response_hashes,
        }
    )
    output_hash = canonical_sha256(
        {
            "classificationCounts": dict(sorted(classification_counts.items())),
            "publicationLineageRows": lineage_rows,
        }
    )
    if unknown:
        producer_status = "STAGE0_SEC_FINANCIAL_LINEAGE_CONTRACT_INVALID"
    elif verified == 0:
        producer_status = "STAGE0_SEC_FINANCIAL_LINEAGE_NO_VERIFIED_ROWS"
    elif not request_budget_exact:
        producer_status = "STAGE0_SEC_FINANCIAL_LINEAGE_REQUEST_BUDGET_INVALID"
    else:
        producer_status = PRODUCER_PASS_STATUS
    result = {
        "schemaVersion": PRODUCER_SCHEMA_VERSION,
        "mode": "SHADOW_ONLY_STAGE0_FINANCIAL_PUBLICATION_LINEAGE",
        "status": producer_status,
        "runId": f"stage0-sec-lineage-{input_hash[:16]}",
        "generatedAt": _utc_iso(retrieved),
        "collectionWindow": effective_collection_window,
        "collectionKey": effective_collection_key
        or canonical_sha256(
            {
                "collectionWindow": effective_collection_window,
                "inputHash": input_hash,
                "schemaVersion": PRODUCER_SCHEMA_VERSION,
            }
        ),
        "sourceFileCount": len(inventory),
        "sourceInputRows": len(ordered_records),
        "sourceParsedRows": len(lineage_rows),
        "sourceRejectedRows": unresolved,
        "sourceFileHashes": inventory,
        "sourceInventorySha256": source_inventory_sha256,
        "identityMapSha256": identity_map_sha256,
        "sourceResponseHashes": response_hashes,
        "requestCounts": counts,
        "externalRequestCount": sum(counts.values()),
        "requestBudgetCompliant": request_budget_compliant,
        "requestBudgetExact": request_budget_exact,
        "retryCount": 0,
        "paginationUsed": False,
        "publicationLineageRows": lineage_rows,
        "classificationCounts": dict(sorted(classification_counts.items())),
        "verifiedRows": verified,
        "ambiguousRows": ambiguous,
        "unresolvedRows": unresolved,
        "unknownOrUnclassifiedRows": unknown,
        "rawResponseStored": False,
        "stageProgressionGate": "STAGE0_LOCKED",
        "recurringActivationAuthorized": recurring_activation_authorized,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "Stage1To7PolicyChanged": False,
        "brokerOrSidecarStateMutation": False,
        "inputHash": input_hash,
        "outputHash": output_hash,
        "hashBasis": {
            "inputHash": "CANONICAL_SOURCE_INVENTORY_IDENTITY_MAP_SEC_RESPONSE_HASHES_AND_INPUT_ROWS",
            "outputHash": "CANONICAL_PRIVATE_LINEAGE_ROWS_AND_CLASSIFICATION_COUNTS",
        },
    }
    result["evidenceSha256"] = canonical_sha256(result)
    return result


def safe_aggregate(private_artifact: Mapping[str, Any]) -> dict[str, Any]:
    safe_keys = (
        "schemaVersion",
        "mode",
        "status",
        "runId",
        "generatedAt",
        "collectionWindow",
        "collectionKey",
        "sourceFileCount",
        "sourceInputRows",
        "sourceParsedRows",
        "sourceRejectedRows",
        "sourceInventorySha256",
        "identityMapSha256",
        "sourceResponseHashes",
        "requestCounts",
        "externalRequestCount",
        "requestBudgetCompliant",
        "requestBudgetExact",
        "retryCount",
        "paginationUsed",
        "classificationCounts",
        "verifiedRows",
        "ambiguousRows",
        "unresolvedRows",
        "unknownOrUnclassifiedRows",
        "rawResponseStored",
        "stageProgressionGate",
        "recurringActivationAuthorized",
        "canonicalSourceChanged",
        "policyImpact",
        "Stage1To7PolicyChanged",
        "brokerOrSidecarStateMutation",
        "inputHash",
        "outputHash",
        "hashBasis",
        "artifactPersistenceStatus",
        "artifactPersistenceErrorCategory",
        "archiveFilename",
    )
    result = {key: private_artifact.get(key) for key in safe_keys if key in private_artifact}
    result.update(
        publicationLineageRows=len(private_artifact.get("publicationLineageRows") or []),
        privatePublicationLineageRowsStored=False,
        privateArtifactSha256=canonical_sha256(private_artifact),
        actualIdentifiersStoredOrPrinted=False,
        secretStoredOrPrinted=False,
        googleDrivePublished=(
            private_artifact.get("artifactPersistenceStatus") == "LOCAL_AND_DRIVE_PUBLISHED"
        ),
        rawResponseStored=False,
    )
    result["evidenceSha256"] = canonical_sha256(result)
    return result


def reserve_producer(
    sentinel_dir: Path,
    reserved_at: str,
    *,
    collection_window: str | None = None,
    recurring_activation_authorized: bool = False,
) -> Path:
    collection_key = (
        recurring_collection_key(collection_window or "")
        if recurring_activation_authorized
        else PRODUCER_COLLECTION_KEY
    )
    if not recurring_activation_authorized and collection_window is not None:
        raise ValueError("one_shot_collection_override_invalid")
    reservation = reserve_collection_sentinel(
        sentinel_dir,
        source_family=PRODUCER_SOURCE_FAMILY,
        collection_key=collection_key,
        reserved_at=reserved_at,
    )
    if reservation["status"] != "RESERVED":
        raise FileExistsError(str(reservation["path"]))
    return Path(str(reservation["path"]))


def _sentinel(sentinel_dir: Path, collection_key: str = PRODUCER_COLLECTION_KEY) -> Path:
    return _sentinel_path(sentinel_dir, PRODUCER_SOURCE_FAMILY, collection_key)


def _zip_loader(archive: zipfile.ZipFile) -> Callable[[str], Any]:
    names = set(archive.namelist())

    def load(cik: str) -> Any:
        name = f"CIK{cik}.json"
        if name not in names:
            return {}
        return json.loads(archive.read(name))

    return load


def _drive_source_files(
    *,
    find_file_id: Callable[[str, str | None], Any],
    download_json: Callable[[str], Any],
    list_files: Callable[[str], list[Mapping[str, Any]]],
) -> tuple[str, Mapping[str, Any], str, list[dict[str, Any]], list[dict[str, Any]]]:
    root_id = find_file_id("US_Alpha_Seeker", None)
    system_id = find_file_id("System_Identity_Maps", root_id)
    daily_id = find_file_id("Financial_Data_Daily", system_id)
    history_id = find_file_id("Financial_Data_History_5Y", system_id)
    identity_id = find_file_id("Ticker_ID_Mapping_Final.json", system_id)
    if not all((root_id, system_id, daily_id, history_id, identity_id)):
        raise ValueError("drive_stage0_source_contract_incomplete")
    identity_map = download_json(identity_id)
    if not isinstance(identity_map, Mapping):
        raise ValueError("identity_map_invalid")
    identity_hash = canonical_sha256(identity_map)

    def read_folder(folder_id: str, suffix: str) -> list[dict[str, Any]]:
        files = [
            row
            for row in list_files(folder_id)
            if _source_group(str(row.get("name") or ""), suffix) is not None
        ]
        groups = {_source_group(str(row.get("name") or ""), suffix) for row in files}
        if groups != set(string.ascii_uppercase) or len(files) != 26:
            raise ValueError(f"incomplete_stage0_{suffix}_source_inventory")
        result = []
        for row in sorted(files, key=lambda value: str(value.get("name") or "")):
            payload = download_json(str(row.get("id") or ""))
            if not isinstance(payload, Mapping):
                raise ValueError(f"invalid_stage0_{suffix}_source_payload")
            result.append(
                {
                    "fileName": str(row["name"]),
                    "contentSha256": canonical_sha256(payload),
                    "payload": payload,
                }
            )
        return result

    return system_id, identity_map, identity_hash, read_folder(daily_id, "daily"), read_folder(history_id, "history")


def run_bounded_producer(
    *,
    session: Any,
    environment: Mapping[str, Any],
    safe_output_path: Path,
    private_output_path: Path,
    sentinel_dir: Path,
    raw_temp_dir: Path,
    retrieved_at: str | None,
    approval: str,
    find_file_id: Callable[[str, str | None], Any],
    download_json: Callable[[str], Any],
    list_files: Callable[[str], list[Mapping[str, Any]]],
    upload_json: Callable[[str, dict[str, Any], str], None],
    utc_now: Callable[[], str] = _utc_now,
    collection_window: str | None = None,
    recurring_activation_authorized: bool = False,
) -> dict[str, Any]:
    expected_approval = (
        PRODUCER_RECURRING_APPROVAL
        if recurring_activation_authorized
        else PRODUCER_APPROVAL
    )
    if approval != expected_approval:
        raise RuntimeError("stage0_sec_financial_lineage_producer_approval_required")
    if recurring_activation_authorized:
        effective_collection_key = recurring_collection_key(collection_window or "")
    else:
        if collection_window is not None:
            raise ValueError("one_shot_collection_override_invalid")
        effective_collection_key = PRODUCER_COLLECTION_KEY
    sec_user_agent = str(environment.get("SEC_USER_AGENT") or "").strip()
    if "@" not in sec_user_agent:
        raise RuntimeError("sec_fair_access_contact_missing")
    if safe_output_path.exists() or private_output_path.exists():
        raise FileExistsError(safe_output_path)
    sentinel_path = _sentinel(sentinel_dir, effective_collection_key)
    sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
    if sentinel.get("status") != "IN_PROGRESS":
        raise FileExistsError(sentinel_path)

    client = _BoundedClient(session, REQUEST_BUDGETS)
    raw_temp_dir.mkdir(parents=True, exist_ok=False)
    status = "STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_RUNTIME_FAILURE"
    safe: dict[str, Any] = {}
    try:
        system_id, identity_map, identity_hash, daily_files, history_files = _drive_source_files(
            find_file_id=find_file_id,
            download_json=download_json,
            list_files=list_files,
        )
        records = build_financial_input_rows(
            identity_map=identity_map,
            identity_map_sha256=identity_hash,
            daily_files=daily_files,
            history_files=history_files,
        )
        response, body, ticker_metadata = client.request(
            "secCompanyTickerMap",
            "GET",
            ENDPOINTS["secCompanyTickerMap"],
            headers={"User-Agent": sec_user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        if response is None or not 200 <= int(response.status_code) < 300:
            raise RuntimeError("sec_company_ticker_map_http_failure")
        ticker_index, ticker_shape = build_ticker_cik_index(json.loads(body))
        if not ticker_index or ticker_shape["invalidTickerCikRows"]:
            raise ValueError("sec_company_ticker_map_contract_invalid")

        companyfacts_path = raw_temp_dir / "companyfacts.zip"
        response, facts_metadata = client.stream_to_file(
            "secCompanyfactsBulk",
            "GET",
            ENDPOINTS["secCompanyfactsBulk"],
            companyfacts_path,
            headers={"User-Agent": sec_user_agent, "Accept-Encoding": "identity"},
        )
        if response is None or not 200 <= int(response.status_code) < 300:
            raise RuntimeError("sec_companyfacts_bulk_http_failure")
        submissions_path = raw_temp_dir / "submissions.zip"
        response, submissions_metadata = client.stream_to_file(
            "secSubmissionsBulk",
            "GET",
            ENDPOINTS["secSubmissionsBulk"],
            submissions_path,
            headers={"User-Agent": sec_user_agent, "Accept-Encoding": "identity"},
        )
        if response is None or not 200 <= int(response.status_code) < 300:
            raise RuntimeError("sec_submissions_bulk_http_failure")
        effective_retrieved_at = str(retrieved_at or utc_now())
        response_hashes = {
            "secCompanyTickerMap": ticker_metadata["responseSha256"],
            "secCompanyfactsBulk": facts_metadata["responseSha256"],
            "secSubmissionsBulk": submissions_metadata["responseSha256"],
        }
        source_files = [
            {"fileName": row["fileName"], "sourceKind": "DAILY", "contentSha256": row["contentSha256"]}
            for row in daily_files
        ] + [
            {"fileName": row["fileName"], "sourceKind": "HISTORY", "contentSha256": row["contentSha256"]}
            for row in history_files
        ]
        with zipfile.ZipFile(companyfacts_path) as company_archive, zipfile.ZipFile(submissions_path) as submission_archive:
            private = build_lineage_artifact(
                records=records,
                ticker_cik_index=ticker_index,
                companyfacts_by_cik=_zip_loader(company_archive),
                submissions_by_cik=_zip_loader(submission_archive),
                retrieved_at=effective_retrieved_at,
                source_files=source_files,
                identity_map_sha256=identity_hash,
                source_response_hashes=response_hashes,
                request_counts=client.counts,
                collection_window=collection_window,
                collection_key=(
                    effective_collection_key if recurring_activation_authorized else None
                ),
                recurring_activation_authorized=recurring_activation_authorized,
            )
        persisted = persist_shadow_artifact(
            private,
            local_path=str(private_output_path),
            current_filename=PRODUCER_CURRENT_FILENAME,
            archive_prefix="STAGE0_SEC_FINANCIAL_PUBLICATION_LINEAGE",
            parent_id=system_id,
            writer=lambda path, payload, _label: _atomic_write_json(Path(path), payload),
            uploader=upload_json,
        )
        if persisted.get("artifactPersistenceStatus") != "LOCAL_AND_DRIVE_PUBLISHED":
            persisted["status"] = "STAGE0_SEC_FINANCIAL_LINEAGE_PERSISTENCE_FAILURE"
            persisted["evidenceSha256"] = canonical_sha256(
                {key: value for key, value in persisted.items() if key != "evidenceSha256"}
            )
            if private_output_path.exists():
                _atomic_write_json(private_output_path, persisted)
        status = str(persisted.get("status") or status)
        safe = safe_aggregate(persisted)
    except (json.JSONDecodeError, OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        status = "STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_RUNTIME_FAILURE"
        effective_retrieved_at = str(retrieved_at or utc_now())
        safe = {
            "schemaVersion": PRODUCER_SCHEMA_VERSION,
            "mode": "SHADOW_ONLY_STAGE0_FINANCIAL_PUBLICATION_LINEAGE",
            "status": status,
            "retrievedAt": effective_retrieved_at,
            "collectionWindow": collection_window,
            "collectionKey": effective_collection_key,
            "requestCounts": dict(sorted(client.counts.items())),
            "externalRequestCount": sum(client.counts.values()),
            "requestBudgetCompliant": all(client.counts[key] <= REQUEST_BUDGETS[key] for key in REQUEST_BUDGETS),
            "requestBudgetExact": client.counts == REQUEST_BUDGETS,
            "safeErrorCategory": type(exc).__name__,
            "publicationLineageRows": 0,
            "privatePublicationLineageRowsStored": False,
            "actualIdentifiersStoredOrPrinted": False,
            "secretStoredOrPrinted": False,
            "rawResponseStored": False,
            "stageProgressionGate": "STAGE0_LOCKED",
            "recurringActivationAuthorized": recurring_activation_authorized,
            "canonicalSourceChanged": False,
            "policyImpact": "NONE_REPORT_ONLY",
            "Stage1To7PolicyChanged": False,
            "brokerOrSidecarStateMutation": False,
            "unknownOrUnclassifiedRows": 0,
        }
        safe["evidenceSha256"] = canonical_sha256(safe)
    finally:
        shutil.rmtree(raw_temp_dir, ignore_errors=True)

    safe["temporaryArchivesDeleted"] = not raw_temp_dir.exists()
    safe["evidenceSha256"] = canonical_sha256(
        {key: value for key, value in safe.items() if key != "evidenceSha256"}
    )
    _atomic_write_json(safe_output_path, safe)
    finish_collection_sentinel(
        sentinel_path,
        status="COMPLETE" if status == PRODUCER_PASS_STATUS else "FAILED",
        completed_at=utc_now(),
        artifact_sha256=str(safe["evidenceSha256"]),
        request_counts=client.counts,
    )
    return safe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--safe-output", type=Path)
    parser.add_argument("--private-output", type=Path)
    parser.add_argument("--sentinel-dir", type=Path, required=True)
    parser.add_argument("--raw-temp-dir", type=Path)
    parser.add_argument("--retrieved-at", default=None)
    parser.add_argument("--collection-window", default=None)
    parser.add_argument("--recurring-activation", action="store_true")
    parser.add_argument("--reserve-only", action="store_true")
    args = parser.parse_args()
    retrieved_at = str(args.retrieved_at) if args.retrieved_at else None
    if args.reserve_only:
        reserve_producer(
            args.sentinel_dir,
            retrieved_at or _utc_now(),
            collection_window=args.collection_window,
            recurring_activation_authorized=args.recurring_activation,
        )
        print("[STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER] reservation=RESERVED requests=0")
        return 0
    if args.safe_output is None or args.private_output is None or args.raw_temp_dir is None:
        parser.error("--safe-output, --private-output and --raw-temp-dir are required")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import harvester

    def list_files(parent_id: str) -> list[Mapping[str, Any]]:
        response = harvester.drive_service.files().list(
            q=f"'{parent_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageSize=1000,
        ).execute()
        if response.get("nextPageToken"):
            raise ValueError("drive_source_inventory_pagination_required")
        return list(response.get("files") or [])

    session = requests.Session()
    session.trust_env = False
    try:
        with redirect_stdout(io.StringIO()):
            result = run_bounded_producer(
                session=session,
                environment=os.environ,
                safe_output_path=args.safe_output,
                private_output_path=args.private_output,
                sentinel_dir=args.sentinel_dir,
                raw_temp_dir=args.raw_temp_dir,
                retrieved_at=retrieved_at,
                approval=str(os.getenv("STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_APPROVAL") or ""),
                find_file_id=harvester.find_file_id,
                download_json=harvester.download_json,
                list_files=list_files,
                upload_json=harvester.upload_json,
                collection_window=args.collection_window,
                recurring_activation_authorized=args.recurring_activation,
            )
    finally:
        session.close()
    print(
        "[STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER] "
        f"status={result['status']} requests={result['externalRequestCount']} "
        "rawStored=false identifiersPrinted=false"
    )
    return 0 if result["status"] == PRODUCER_PASS_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
