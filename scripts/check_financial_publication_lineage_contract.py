from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fixtures/financial_publication_lineage_contract.json"

EXPECTED_ENDPOINTS = {
    "SEC_COMPANY_TICKER_MAP": "https://www.sec.gov/files/company_tickers.json",
    "SEC_COMPANYFACTS_BULK": (
        "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
    ),
    "SEC_SUBMISSIONS_BULK": (
        "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip"
    ),
}
ALLOWED_CASE_STATUSES = {
    "FINANCIAL_LINEAGE_VERIFIED",
    "IDENTIFIER_LINEAGE_INCOMPLETE",
    "FISCAL_PERIOD_MISSING",
    "EXACT_FACT_NOT_FOUND",
    "FACT_ACCESSION_AMBIGUOUS",
    "SUBMISSIONS_ACCESSION_MISSING",
    "ACCEPTANCE_TIMESTAMP_MISSING",
    "PUBLICATION_AFTER_RETRIEVAL_REJECTED",
}


def _parse_utc(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed.astimezone(dt.timezone.utc)


def _case_status(case: dict[str, object]) -> str:
    if case["identifierLineageExact"] is not True:
        return "IDENTIFIER_LINEAGE_INCOMPLETE"
    if not str(case.get("fiscalPeriod") or ""):
        return "FISCAL_PERIOD_MISSING"
    exact_matches = int(case["exactFactMatches"])
    if exact_matches == 0:
        return "EXACT_FACT_NOT_FOUND"
    if exact_matches > 1:
        return "FACT_ACCESSION_AMBIGUOUS"
    if case["submissionsAccessionMatched"] is not True:
        return "SUBMISSIONS_ACCESSION_MISSING"
    published_at = _parse_utc(case.get("acceptanceDateTime"))
    if published_at is None:
        return "ACCEPTANCE_TIMESTAMP_MISSING"
    retrieved_at = _parse_utc(case["financialRetrievedAt"])
    assert retrieved_at is not None
    if published_at > retrieved_at:
        return "PUBLICATION_AFTER_RETRIEVAL_REJECTED"
    return "FINANCIAL_LINEAGE_VERIFIED"


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["schemaVersion"] == "financial-publication-lineage-capability-v1"
    assert contract["mode"] == "STATIC_OFFICIAL_SOURCE_CAPABILITY_CONTRACT"
    assert contract["status"] == "OFFICIAL_SEC_BULK_CAPABILITY_PROBE_REQUIRED"
    assert contract["runtimeEnabled"] is False
    assert contract["externalRequestCount"] == 0
    assert contract["producerMutationImplemented"] is False
    assert contract["canonicalSourceChanged"] is False
    assert contract["policyImpact"] == "NONE_REPORT_ONLY"
    assert contract["stage1To7PolicyChanged"] is False

    local = contract["localEvidenceAudit"]
    assert local == {
        "cikLikeIdentityFields": 0,
        "dailyFinancialPublishedAtRows": 0,
        "dailyFinancialRetrievedAtRows": 0,
        "dailyFinancialSourceRows": 0,
        "dailyFiscalPeriodRows": 0,
        "dailySourceFiles": 26,
        "dailySourceRows": 5675,
        "financialHistoryEntityRows": 308,
        "financialHistoryFileSha256": (
            "c685290f1fb645ca562192e68526ab0a29356162543257358cf68282059e68ee"
        ),
        "financialHistoryFilesInspected": 1,
        "identityMapRows": 5677,
        "identityMapSha256": (
            "3e6f626a9d69ac37b2a60f780ac0016bd002da9a110394a964c5c6176c61ad58"
        ),
        "publicationLikeFields": 0,
        "statementRowsAcrossViews": 6740,
        "upstreamAuditArtifactSha256": (
            "6f7f48ca7355b5cb4e9ffc1eaadbd91bf288f7f6197de146f36f8624fd307fa9"
        ),
    }

    semantics = contract["requiredProducerSemantics"]
    assert semantics["financialSource"] == "YFINANCE_HISTORY_SEC_EDGAR_EXACT_LINEAGE"
    assert semantics["fiscalPeriodBasis"] == "EXACT_STATEMENT_PERIOD_END"
    assert semantics["financialPublishedAtBasis"] == "SEC_ACCEPTANCE_DATE_TIME"
    assert semantics["financialRetrievedAtBasis"] == "HARVESTER_UTC_RETRIEVAL_TIME"
    assert semantics["infoValueWithoutFiscalPeriodEligible"] is False
    assert semantics["symbolOnlyJoinAllowed"] is False
    assert semantics["nearestValueMatchAllowed"] is False
    assert semantics["timestampFallbackAllowed"] is False
    assert semantics["legacyNetIncomeAsOfIsPublicationTime"] is False
    assert semantics["publishedAtMustNotExceedRetrievedAt"] is True
    assert semantics["sourceRecordSha256Required"] is True

    source_contracts = contract["officialSourceContracts"]
    assert len(source_contracts) == len(EXPECTED_ENDPOINTS)
    by_id = {row["sourceId"]: row for row in source_contracts}
    assert len(by_id) == len(source_contracts)
    assert set(by_id) == set(EXPECTED_ENDPOINTS)
    for source_id, endpoint in EXPECTED_ENDPOINTS.items():
        row = by_id[source_id]
        assert row["endpoint"] == endpoint
        host = (urlparse(endpoint).hostname or "").lower()
        assert host == "sec.gov" or host.endswith(".sec.gov")
        assert row["requestLimit"] == 1
        assert row["retryAllowed"] is False
        assert row["paginationAllowed"] is False
        assert row["rawResponsePersisted"] is False

    join = contract["exactJoinContract"]
    assert join["identifierKey"] == "TEN_DIGIT_CIK"
    assert join["factToSubmissionKey"] == "ACCESSION_NUMBER"
    assert join["publicationField"] == "acceptanceDateTime"
    assert join["filingDateAsPublicationAllowed"] is False
    assert join["fiscalPeriodAsPublicationAllowed"] is False
    assert join["retrievalTimeAsPublicationAllowed"] is False
    assert join["amendmentBackdatingAllowed"] is False
    assert join["duplicateCandidateDisposition"] == "FAIL_CLOSED"
    assert join["unknownOrUnclassifiedRows"] == 0
    assert join["exactFactMatchFields"] == [
        "taxonomy",
        "concept",
        "unit",
        "value",
        "periodStart",
        "periodEnd",
        "form",
        "accessionNumber",
    ]
    assert set(join["eligibleForms"]) == {
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
    assert join["eligibleConcepts"] == {
        "ifrs-full": [
            "ProfitLoss",
            "ProfitLossAttributableToOwnersOfParent",
        ],
        "us-gaap": [
            "NetIncomeLoss",
            "NetIncomeLossAvailableToCommonStockholdersBasic",
            "ProfitLoss",
        ],
    }

    budget = contract["boundedOneShotApprovalPackage"]
    assert budget["approvalPhrase"] == (
        "AUTHORIZE SEC FINANCIAL PUBLICATION LINEAGE BULK ONE-SHOT"
    )
    assert budget["requestCounts"] == {
        "secCompanyTickerMap": 1,
        "secCompanyfactsBulk": 1,
        "secSubmissionsBulk": 1,
        "total": 3,
    }
    assert budget["retry"] == 0
    assert budget["pagination"] == 0
    assert budget["persistentRawResponseStorage"] is False
    assert budget["privateTemporaryArchiveOnly"] is True
    assert budget["deleteTemporaryArchivesBeforeCompletion"] is True
    assert budget["drivePublish"] is False
    assert budget["producerActivation"] is False
    assert budget["brokerOrSidecarStateMutation"] is False

    cases = contract["syntheticCases"]
    assert cases
    assert len({case["caseId"] for case in cases}) == len(cases)
    observed = set()
    for case in cases:
        assert "symbol" not in case and "ticker" not in case and "cik" not in case
        status = _case_status(case)
        assert status in ALLOWED_CASE_STATUSES
        assert status == case["expectedStatus"]
        observed.add(status)
    assert observed == ALLOWED_CASE_STATUSES

    summary = contract["auditSummary"]
    assert summary == {
        "externalRequestCount": 0,
        "fabricatedPublicationTimestampRows": 0,
        "unknownOrUnclassifiedRows": 0,
        "producerRowsChanged": 0,
        "stage1To7PolicyChangedRows": 0,
    }
    assert _canonical_sha256(contract) == _canonical_sha256(
        json.loads(json.dumps(contract))
    )

    print(
        "[FINANCIAL_PUBLICATION_LINEAGE_CONTRACT] PASS "
        "status=OFFICIAL_SEC_BULK_CAPABILITY_PROBE_REQUIRED "
        "externalRequests=0 fabricatedPublicationTimestamps=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
