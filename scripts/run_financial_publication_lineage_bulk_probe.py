from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

import requests

from official_shadow_runtime import (
    _atomic_write_json,
    _sentinel_path,
    canonical_sha256,
    finish_collection_sentinel,
    reserve_collection_sentinel,
)
from sec_finra_shadow_evidence import _BoundedClient, _submission_lineage


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fixtures/financial_publication_lineage_contract.json"
APPROVAL = "AUTHORIZE SEC FINANCIAL PUBLICATION LINEAGE BULK ONE-SHOT"
PASS_STATUS = "SEC_FINANCIAL_PUBLICATION_LINEAGE_BULK_CAPABILITY_PASS"
SCHEMA_VERSION = "sec-financial-publication-lineage-bulk-probe-v1"
SOURCE_FAMILY = "SEC_FINANCIAL_PUBLICATION_LINEAGE_BULK"
REQUEST_BUDGETS = {
    "secCompanyTickerMap": 1,
    "secCompanyfactsBulk": 1,
    "secSubmissionsBulk": 1,
}
ENDPOINTS = {
    "secCompanyTickerMap": "https://www.sec.gov/files/company_tickers.json",
    "secCompanyfactsBulk": (
        "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
    ),
    "secSubmissionsBulk": (
        "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
    ),
}
COLLECTION_KEY = canonical_sha256(
    {
        "contractSha256": canonical_sha256(
            json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        ),
        "schemaVersion": SCHEMA_VERSION,
        "sourceFamily": SOURCE_FAMILY,
        "requestScope": sorted(ENDPOINTS),
    }
)
ACCEPTED_IDENTITY_STATUSES = {
    "EFFECTIVE_ALIAS_RESOLVED",
    "IDENTIFIER_ALIAS_RESOLVED",
    "IDENTIFIER_LINEAGE_VERIFIED",
    "OFFICIAL_CURRENT_TICKER_CIK_EXACT",
}


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.utcoffset() is None:
        return None
    return parsed.astimezone(dt.timezone.utc)


def _ten_digit_cik(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > 10 or int(text) <= 0:
        return None
    return text.zfill(10)


def _exact_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _contract() -> dict[str, Any]:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if (
        payload.get("schemaVersion")
        != "financial-publication-lineage-capability-v1"
        or payload.get("boundedOneShotApprovalPackage", {}).get("approvalPhrase")
        != APPROVAL
    ):
        raise ValueError("financial_publication_lineage_contract_invalid")
    return payload


def build_ticker_cik_index(
    payload: Any,
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    rows = list(payload.values()) if isinstance(payload, dict) else payload
    rows = rows if isinstance(rows, list) else []
    collected: dict[str, set[str]] = {}
    invalid_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        cik = _ten_digit_cik(row.get("cik_str"))
        if not ticker or cik is None:
            invalid_rows += 1
            continue
        collected.setdefault(ticker, set()).add(cik)
    index = {ticker: tuple(sorted(ciks)) for ticker, ciks in sorted(collected.items())}
    return index, {
        "tickerRows": len(rows),
        "validTickerCikRows": len(rows) - invalid_rows,
        "invalidTickerCikRows": invalid_rows,
        "ambiguousTickerRows": sum(len(ciks) != 1 for ciks in index.values()),
        "uniqueCikRows": len({cik for ciks in index.values() for cik in ciks}),
    }


def _exact_fact_candidates(
    companyfacts: Any,
    *,
    fiscal_period: str,
    value: Decimal,
    contract: Mapping[str, Any],
) -> list[dict[str, Any]]:
    facts = companyfacts.get("facts") if isinstance(companyfacts, dict) else None
    if not isinstance(facts, dict):
        return []
    join = contract["exactJoinContract"]
    eligible_concepts = join["eligibleConcepts"]
    eligible_forms = set(join["eligibleForms"])
    candidates: list[dict[str, Any]] = []
    for taxonomy, concepts in sorted(eligible_concepts.items()):
        taxonomy_facts = facts.get(taxonomy)
        if not isinstance(taxonomy_facts, dict):
            continue
        for concept in concepts:
            concept_row = taxonomy_facts.get(concept)
            units = concept_row.get("units") if isinstance(concept_row, dict) else None
            if not isinstance(units, dict):
                continue
            for unit, rows in sorted(units.items()):
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    row_value = _exact_decimal(row.get("val"))
                    if (
                        row_value != value
                        or str(row.get("end") or "") != fiscal_period
                        or str(row.get("form") or "") not in eligible_forms
                        or not str(row.get("start") or "")
                        or not str(row.get("accn") or "")
                        or not str(unit or "")
                    ):
                        continue
                    candidates.append(
                        {
                            "taxonomy": taxonomy,
                            "concept": concept,
                            "unit": str(unit),
                            "value": row.get("val"),
                            "periodStart": str(row["start"]),
                            "periodEnd": fiscal_period,
                            "form": str(row["form"]),
                            "accessionNumber": str(row["accn"]),
                        }
                    )
    return candidates


def match_exact_financial_lineage(
    *,
    identity: Mapping[str, Any],
    history_record: Mapping[str, Any],
    ticker_cik_index: Mapping[str, tuple[str, ...]],
    companyfacts: Any,
    submissions: Any,
    retrieved_at: str,
) -> dict[str, Any]:
    if str(identity.get("identifierLineageStatus") or "") not in ACCEPTED_IDENTITY_STATUSES:
        return {"status": "IDENTIFIER_LINEAGE_INCOMPLETE"}
    ticker = str(identity.get("effectiveSymbol") or "").strip().upper()
    ciks = ticker_cik_index.get(ticker, ())
    if len(ciks) != 1:
        return {
            "status": (
                "IDENTIFIER_CIK_NOT_FOUND"
                if not ciks
                else "IDENTIFIER_CIK_AMBIGUOUS"
            )
        }
    fiscal_period = str(history_record.get("fiscalPeriod") or "").strip()
    try:
        dt.date.fromisoformat(fiscal_period)
    except ValueError:
        return {"status": "FISCAL_PERIOD_MISSING"}
    value = _exact_decimal(history_record.get("value"))
    if value is None:
        return {"status": "EXACT_FACT_NOT_FOUND"}
    company_cik = _ten_digit_cik(
        companyfacts.get("cik") if isinstance(companyfacts, dict) else None
    )
    submission_cik = _ten_digit_cik(
        submissions.get("cik") if isinstance(submissions, dict) else None
    )
    if company_cik != ciks[0] or submission_cik != ciks[0]:
        return {"status": "IDENTIFIER_CIK_MISMATCH"}

    candidates = _exact_fact_candidates(
        companyfacts,
        fiscal_period=fiscal_period,
        value=value,
        contract=_contract(),
    )
    if not candidates:
        return {"status": "EXACT_FACT_NOT_FOUND"}
    if len(candidates) != 1:
        return {"status": "FACT_ACCESSION_AMBIGUOUS"}
    candidate = candidates[0]
    submission = _submission_lineage(submissions, candidate["accessionNumber"])
    if submission.get("status") != "ACCESSION_MATCHED":
        return {"status": "SUBMISSIONS_ACCESSION_MISSING"}
    if submission.get("observedForm") != candidate["form"]:
        return {"status": "SUBMISSIONS_FORM_MISMATCH"}
    published_at = _parse_utc(submission.get("publishedAt"))
    if published_at is None:
        return {"status": "ACCEPTANCE_TIMESTAMP_MISSING"}
    retrieved = _parse_utc(retrieved_at)
    if retrieved is None:
        raise ValueError("retrieved_at_utc_required")
    if published_at > retrieved:
        return {"status": "PUBLICATION_AFTER_RETRIEVAL_REJECTED"}
    published_iso = published_at.isoformat().replace("+00:00", "Z")
    return {
        "status": "FINANCIAL_LINEAGE_VERIFIED",
        "financialSource": "YFINANCE_HISTORY_SEC_EDGAR_EXACT_LINEAGE",
        "fiscalPeriod": fiscal_period,
        "financialPublishedAt": published_iso,
        "financialRetrievedAt": retrieved.isoformat().replace("+00:00", "Z"),
        "sourceRecordSha256": canonical_sha256(
            {**candidate, "acceptanceDateTime": published_iso, "cik": ciks[0]}
        ),
    }


def _archive_join_shape(
    *,
    ticker_cik_index: Mapping[str, tuple[str, ...]],
    companyfacts_path: Path,
    submissions_path: Path,
    retrieved_at: str,
) -> tuple[str, dict[str, Any]]:
    shape: dict[str, Any] = {
        "companyfactsArchiveMembers": 0,
        "submissionsArchiveMembers": 0,
        "commonCikMembers": 0,
        "membersInspected": 0,
        "candidateHistoryRows": 0,
        "verifiedLineageRows": 0,
        "rejectedLineageRows": 0,
        "invalidArchiveMemberRows": 0,
        "classificationCounts": {},
        "exactJoinObserved": False,
    }
    inverse: dict[str, list[str]] = {}
    for ticker, ciks in ticker_cik_index.items():
        if len(ciks) == 1:
            inverse.setdefault(ciks[0], []).append(ticker)
    try:
        company_archive = zipfile.ZipFile(companyfacts_path)
        submission_archive = zipfile.ZipFile(submissions_path)
    except (OSError, zipfile.BadZipFile):
        return "SEC_BULK_ARCHIVE_INVALID", shape
    with company_archive, submission_archive:
        contract = _contract()["exactJoinContract"]
        eligible_concepts = contract["eligibleConcepts"]
        eligible_forms = set(contract["eligibleForms"])
        company_names = {name for name in company_archive.namelist() if name.endswith(".json")}
        submission_names = {
            name for name in submission_archive.namelist() if name.endswith(".json")
        }
        shape["companyfactsArchiveMembers"] = len(company_names)
        shape["submissionsArchiveMembers"] = len(submission_names)
        common = sorted(company_names & submission_names)
        shape["commonCikMembers"] = len(common)
        for name in common:
            match = re.search(r"CIK(\d{10})\.json$", name)
            if match is None or len(inverse.get(match.group(1), [])) != 1:
                shape["invalidArchiveMemberRows"] += 1
                continue
            try:
                companyfacts = json.loads(company_archive.read(name))
                submissions = json.loads(submission_archive.read(name))
            except (KeyError, ValueError, zipfile.BadZipFile):
                shape["invalidArchiveMemberRows"] += 1
                continue
            shape["membersInspected"] += 1
            seen: set[tuple[str, str]] = set()
            facts = companyfacts.get("facts") if isinstance(companyfacts, dict) else {}
            for taxonomy_name, concept_names in sorted(eligible_concepts.items()):
                taxonomy = facts.get(taxonomy_name) if isinstance(facts, dict) else None
                if not isinstance(taxonomy, dict):
                    continue
                for concept_name in concept_names:
                    concept = taxonomy.get(concept_name)
                    units = concept.get("units") if isinstance(concept, dict) else {}
                    for rows in units.values() if isinstance(units, dict) else ():
                        for row in rows if isinstance(rows, list) else ():
                            if not isinstance(row, dict):
                                continue
                            value = _exact_decimal(row.get("val"))
                            period = str(row.get("end") or "")
                            if (
                                value is None
                                or not period
                                or str(row.get("form") or "") not in eligible_forms
                            ):
                                continue
                            key = (str(value), period)
                            if key in seen:
                                continue
                            seen.add(key)
                            shape["candidateHistoryRows"] += 1
                            result = match_exact_financial_lineage(
                                identity={
                                    "effectiveSymbol": inverse[match.group(1)][0],
                                    "identifierLineageStatus": (
                                        "OFFICIAL_CURRENT_TICKER_CIK_EXACT"
                                    ),
                                },
                                history_record={"value": row.get("val"), "fiscalPeriod": period},
                                ticker_cik_index=ticker_cik_index,
                                companyfacts=companyfacts,
                                submissions=submissions,
                                retrieved_at=retrieved_at,
                            )
                            status = str(result["status"])
                            counts = shape["classificationCounts"]
                            counts[status] = int(counts.get(status, 0)) + 1
                            if status == "FINANCIAL_LINEAGE_VERIFIED":
                                shape["verifiedLineageRows"] = 1
                                shape["exactJoinObserved"] = True
                                return PASS_STATUS, shape
                            shape["rejectedLineageRows"] += 1
    return "SEC_FINANCIAL_PUBLICATION_LINEAGE_EXACT_JOIN_NOT_OBSERVED", shape


def reserve_bulk_probe(sentinel_dir: Path, reserved_at: str) -> Path:
    reservation = reserve_collection_sentinel(
        sentinel_dir,
        source_family=SOURCE_FAMILY,
        collection_key=COLLECTION_KEY,
        reserved_at=reserved_at,
    )
    if reservation["status"] != "RESERVED":
        raise FileExistsError(str(reservation["path"]))
    return Path(str(reservation["path"]))


def _sentinel(sentinel_dir: Path) -> Path:
    return _sentinel_path(sentinel_dir, SOURCE_FAMILY, COLLECTION_KEY)


def _safe_result(
    *,
    status: str,
    retrieved_at: str,
    client: _BoundedClient,
    responses: Mapping[str, Any],
    ticker_shape: Mapping[str, Any],
    join_shape: Mapping[str, Any],
    safe_error_category: str | None,
    archives_deleted: bool,
) -> dict[str, Any]:
    counts = {key: int(client.counts.get(key, 0)) for key in sorted(REQUEST_BUDGETS)}
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "mode": "SHADOW_ONLY_BOUNDED_CAPABILITY_PROBE",
        "status": status,
        "retrievedAt": retrieved_at,
        "collectionKey": COLLECTION_KEY,
        "requestCounts": counts,
        "externalRequestCount": sum(counts.values()),
        "requestBudgetCompliant": all(
            counts[key] <= REQUEST_BUDGETS[key] for key in REQUEST_BUDGETS
        ),
        "retryCount": 0,
        "paginationUsed": False,
        "sourceResponses": dict(responses),
        "tickerCikShape": dict(ticker_shape),
        "exactJoinShape": dict(join_shape),
        "safeErrorCategory": safe_error_category,
        "temporaryArchivesDeleted": archives_deleted,
        "rawResponseStored": False,
        "secretStoredOrPrinted": False,
        "actualIdentifiersStoredOrPrinted": False,
        "googleDrivePublished": False,
        "recurringActivationAuthorized": False,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "stage1To7PolicyChanged": False,
        "brokerOrSidecarStateMutation": False,
        "unknownOrUnclassifiedRows": 0,
        "evidenceHashBasis": "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH",
    }
    result["evidenceSha256"] = canonical_sha256(result)
    return result


def run_bulk_probe(
    *,
    session: Any,
    environment: Mapping[str, Any],
    output_path: Path,
    sentinel_dir: Path,
    raw_temp_dir: Path,
    retrieved_at: str,
    approval: str,
) -> dict[str, Any]:
    if approval != APPROVAL:
        raise RuntimeError("sec_financial_lineage_probe_approval_required")
    sec_user_agent = str(environment.get("SEC_USER_AGENT") or "").strip()
    if "@" not in sec_user_agent:
        raise RuntimeError("sec_fair_access_contact_missing")
    if output_path.exists():
        raise FileExistsError(output_path)
    sentinel_path = _sentinel(sentinel_dir)
    sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
    if sentinel.get("status") != "IN_PROGRESS":
        raise FileExistsError(sentinel_path)

    client = _BoundedClient(session, REQUEST_BUDGETS)
    responses: dict[str, Any] = {}
    ticker_shape: dict[str, Any] = {}
    join_shape: dict[str, Any] = {}
    status = "SEC_FINANCIAL_PUBLICATION_LINEAGE_PROBE_RUNTIME_FAILURE"
    safe_error_category: str | None = None
    raw_temp_dir.mkdir(parents=True, exist_ok=False)
    try:
        response, body, metadata = client.request(
            "secCompanyTickerMap",
            "GET",
            ENDPOINTS["secCompanyTickerMap"],
            headers={"User-Agent": sec_user_agent, "Accept-Encoding": "gzip, deflate"},
        )
        responses["SEC_COMPANY_TICKER_MAP"] = metadata
        if response is None or not 200 <= int(response.status_code) < 300:
            status = "SEC_COMPANY_TICKER_MAP_HTTP_FAILURE"
        else:
            ticker_index, ticker_shape = build_ticker_cik_index(json.loads(body))
            if not ticker_index or ticker_shape["ambiguousTickerRows"]:
                status = "SEC_COMPANY_TICKER_MAP_CONTRACT_INVALID"
            else:
                companyfacts_path = raw_temp_dir / "companyfacts.zip"
                response, metadata = client.stream_to_file(
                    "secCompanyfactsBulk",
                    "GET",
                    ENDPOINTS["secCompanyfactsBulk"],
                    companyfacts_path,
                    headers={"User-Agent": sec_user_agent, "Accept-Encoding": "identity"},
                )
                responses["SEC_COMPANYFACTS_BULK"] = metadata
                if response is None or not 200 <= int(response.status_code) < 300:
                    status = "SEC_COMPANYFACTS_BULK_HTTP_FAILURE"
                else:
                    submissions_path = raw_temp_dir / "submissions.zip"
                    response, metadata = client.stream_to_file(
                        "secSubmissionsBulk",
                        "GET",
                        ENDPOINTS["secSubmissionsBulk"],
                        submissions_path,
                        headers={"User-Agent": sec_user_agent, "Accept-Encoding": "identity"},
                    )
                    responses["SEC_SUBMISSIONS_BULK"] = metadata
                    if response is None or not 200 <= int(response.status_code) < 300:
                        status = "SEC_SUBMISSIONS_BULK_HTTP_FAILURE"
                    else:
                        status, join_shape = _archive_join_shape(
                            ticker_cik_index=ticker_index,
                            companyfacts_path=companyfacts_path,
                            submissions_path=submissions_path,
                            retrieved_at=retrieved_at,
                        )
    except (json.JSONDecodeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        safe_error_category = type(exc).__name__
        status = "SEC_FINANCIAL_PUBLICATION_LINEAGE_SOURCE_CONTRACT_INVALID"
    finally:
        shutil.rmtree(raw_temp_dir, ignore_errors=True)

    result = _safe_result(
        status=status,
        retrieved_at=retrieved_at,
        client=client,
        responses=responses,
        ticker_shape=ticker_shape,
        join_shape=join_shape,
        safe_error_category=safe_error_category,
        archives_deleted=not raw_temp_dir.exists(),
    )
    _atomic_write_json(output_path, result)
    finish_collection_sentinel(
        sentinel_path,
        status="COMPLETE" if status == PASS_STATUS else "FAILED",
        completed_at=_utc_now(),
        artifact_sha256=str(result["evidenceSha256"]),
        request_counts=client.counts,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sentinel-dir", type=Path, required=True)
    parser.add_argument("--raw-temp-dir", type=Path)
    parser.add_argument("--retrieved-at", default=None)
    parser.add_argument("--reserve-only", action="store_true")
    args = parser.parse_args()
    retrieved_at = str(args.retrieved_at or _utc_now())
    if args.reserve_only:
        reserve_bulk_probe(args.sentinel_dir, retrieved_at)
        print("[SEC_FINANCIAL_LINEAGE_BULK_PROBE] reservation=RESERVED requests=0")
        return 0
    if args.output is None or args.raw_temp_dir is None:
        parser.error("--output and --raw-temp-dir are required")
    session = requests.Session()
    session.trust_env = False
    try:
        result = run_bulk_probe(
            session=session,
            environment=os.environ,
            output_path=args.output,
            sentinel_dir=args.sentinel_dir,
            raw_temp_dir=args.raw_temp_dir,
            retrieved_at=retrieved_at,
            approval=str(os.getenv("SEC_FINANCIAL_LINEAGE_PROBE_APPROVAL") or ""),
        )
    finally:
        session.close()
    print(
        "[SEC_FINANCIAL_LINEAGE_BULK_PROBE] "
        f"status={result['status']} requests={result['externalRequestCount']} "
        "rawStored=false drivePublished=false"
    )
    return 0 if result["status"] == PASS_STATUS else 2


if __name__ == "__main__":
    raise SystemExit(main())
