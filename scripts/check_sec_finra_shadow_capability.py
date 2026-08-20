from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "fixtures/sec_finra_shadow_capability_contract.json"

ALLOWED_CAPABILITY_STATUSES = {
    "SHADOW_CAPABILITY_READY",
    "OFFICIAL_SOURCE_CONTRACT_INCOMPLETE",
    "IDENTIFIER_LINEAGE_INCOMPLETE",
    "PUBLICATION_DELAY_NOT_MODELED",
    "LICENSE_OR_ACCESS_REVIEW_REQUIRED",
}
EXPECTED_EVIDENCE_CLASSES = {
    "SEC_SECTION16_FORMS_3_4_5": "INSIDER_OWNERSHIP_TRANSACTION",
    "SEC_SCHEDULES_13D_13G": "BENEFICIAL_OWNERSHIP_POSITION_DISCLOSURE",
    "SEC_FORM_13F": "INSTITUTIONAL_HOLDINGS_SNAPSHOT",
    "FINRA_CONSOLIDATED_SHORT_INTEREST": "SHORT_INTEREST_POSITION_SNAPSHOT",
    "FINRA_REG_SHO_DAILY_SHORT_VOLUME": "SHORT_SALE_ACTIVITY_VOLUME",
    "REG_SHO_THRESHOLD_SECURITIES": "REGULATORY_THRESHOLD_STATUS",
}
ALLOWED_IDENTIFIER_EVIDENCE = {
    "STABLE_IDENTIFIER_VERIFIED": "IDENTIFIER_VERIFIED",
    "EFFECTIVE_DATED_ALIAS_VERIFIED": "IDENTIFIER_VERIFIED",
    "IDENTIFIER_LINEAGE_INCOMPLETE": "IDENTIFIER_LINEAGE_INCOMPLETE",
}


def _parse_timestamp(value: object) -> dt.datetime:
    text = str(value or "")
    parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    return parsed


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _official_url(url: object) -> bool:
    host = (urlparse(str(url or "")).hostname or "").lower()
    return (
        host == "sec.gov"
        or host.endswith(".sec.gov")
        or host == "finra.org"
        or host.endswith(".finra.org")
    )


def main() -> int:
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["schemaVersion"] == "sec-finra-shadow-capability-v1"
    assert contract["mode"] == "SHADOW_ONLY"
    assert contract["capabilityStatus"] in ALLOWED_CAPABILITY_STATUSES
    assert contract["capabilityStatus"] == "OFFICIAL_SOURCE_CONTRACT_INCOMPLETE"
    assert contract["primaryBlocker"] == "EXCHANGE_LISTED_REG_SHO_SRO_SOURCE_MATRIX_NOT_SELECTED"
    assert contract["runtimeEnabled"] is False
    assert contract["externalRequestCount"] == 0
    assert contract["canonicalSourceChanged"] is False
    assert contract["policyImpact"] == "NONE_REPORT_ONLY"
    assert contract["stage4To7Impact"] == "NONE"

    sources = contract["sourceContracts"]
    assert isinstance(sources, list) and len(sources) == len(EXPECTED_EVIDENCE_CLASSES)
    source_by_id = {row["sourceId"]: row for row in sources}
    assert len(source_by_id) == len(sources)
    assert set(source_by_id) == set(EXPECTED_EVIDENCE_CLASSES)

    for source_id, evidence_class in EXPECTED_EVIDENCE_CLASSES.items():
        source = source_by_id[source_id]
        assert source["status"] in ALLOWED_CAPABILITY_STATUSES
        assert source["evidenceClass"] == evidence_class
        assert source["semanticMeaning"]
        assert source["prohibitedInference"]
        assert source["directSignalEligible"] is False
        assert source["historicalEvidenceCurrentSentimentEligible"] is False
        assert source["responseSha256Required"] is True
        assert source["amendmentsModeled"] is True
        assert source["amendmentPolicy"]
        assert source["issuerIdentifierPolicy"]
        publication = source["publication"]
        assert publication["eventClock"]
        assert publication["publicationClock"]
        assert publication["timezone"] == "America/New_York"
        assert publication["publicationDelayModeled"] is True
        assert publication["delayModel"]
        assert publication["lookAheadRule"] == "publishedAt<=decisionAt"
        docs = source["officialDocumentation"]
        assert docs and all(_official_url(url) for url in docs)
        machine_contract = source["machineReadableContract"]
        assert machine_contract["format"]
        assert machine_contract["identifier"]

    assert source_by_id["SEC_FORM_13F"]["isRealtimeTradeEvidence"] is False
    assert source_by_id["FINRA_CONSOLIDATED_SHORT_INTEREST"]["isShortSaleVolume"] is False
    assert source_by_id["FINRA_REG_SHO_DAILY_SHORT_VOLUME"]["isShortInterest"] is False
    assert source_by_id["REG_SHO_THRESHOLD_SECURITIES"]["isShortSignal"] is False
    assert (
        source_by_id["REG_SHO_THRESHOLD_SECURITIES"]["status"]
        == "OFFICIAL_SOURCE_CONTRACT_INCOMPLETE"
    )
    assert (
        source_by_id["FINRA_CONSOLIDATED_SHORT_INTEREST"]["status"]
        == "LICENSE_OR_ACCESS_REVIEW_REQUIRED"
    )
    assert (
        source_by_id["FINRA_REG_SHO_DAILY_SHORT_VOLUME"]["status"]
        == "LICENSE_OR_ACCESS_REVIEW_REQUIRED"
    )

    guards = contract["semanticGuards"]
    assert guards == {
        "form13fIsRealtimeTradeEvidence": False,
        "historicalFilingIsCurrentSentiment": False,
        "regShoThresholdIsShortSignal": False,
        "shortInterestEquivalentToShortSaleVolume": False,
    }

    lineage = contract["identifierLineage"]
    assert lineage["existingContract"] == "corporate-action-lineage-v1"
    assert lineage["symbolOnlyJoinAllowed"] is False
    assert lineage["tickerRenameRequiresEffectiveDate"] is True
    assert lineage["mergerOrDelistingRequiresLineage"] is True
    assert lineage["unknownOrUnclassifiedRows"] == 0
    assert "FILING_MANAGER_CIK_IS_NOT_HELD_ISSUER_ID" in lineage["specialCases"]

    cases = contract["syntheticCases"]
    assert cases
    case_ids = {row["caseId"] for row in cases}
    assert len(case_ids) == len(cases)
    look_ahead_rejected = 0
    identifier_incomplete = 0
    for case in cases:
        source_id = case["sourceId"]
        assert source_id in source_by_id
        assert case["identifierSha256"] == case["identifierSha256"].lower()
        assert len(case["identifierSha256"]) == 64
        assert all(char in "0123456789abcdef" for char in case["identifierSha256"])
        assert "symbol" not in case and "ticker" not in case

        published_at = _parse_timestamp(case["publishedAt"])
        decision_at = _parse_timestamp(case["decisionAt"])
        look_ahead_status = (
            "LOOK_AHEAD_REJECTED"
            if published_at > decision_at
            else "PUBLICATION_AVAILABLE"
        )
        assert look_ahead_status == case["expectedLookAheadStatus"]
        look_ahead_rejected += int(look_ahead_status == "LOOK_AHEAD_REJECTED")

        identifier_status = ALLOWED_IDENTIFIER_EVIDENCE[case["identifierEvidence"]]
        assert identifier_status == case["expectedIdentifierStatus"]
        identifier_incomplete += int(identifier_status == "IDENTIFIER_LINEAGE_INCOMPLETE")
        assert case["expectedEvidenceClass"] == EXPECTED_EVIDENCE_CLASSES[source_id]

    assert look_ahead_rejected > 0
    assert identifier_incomplete > 0
    assert _canonical_sha256(contract) == _canonical_sha256(json.loads(json.dumps(contract)))

    summary = contract["auditSummary"]
    assert summary == {
        "externalRequestCount": 0,
        "historicalFilingCurrentSentimentPromotionRows": 0,
        "identifierUnknownOrUnclassifiedRows": 0,
        "lookAheadViolationRows": 0,
        "regShoDirectSignalRows": 0,
        "shortInterestShortSaleVolumeConfusionRows": 0,
        "sourceRows": 6,
    }

    print(
        "[SEC_FINRA_SHADOW_CAPABILITY] "
        f"PASS status={contract['capabilityStatus']} sources={len(sources)} "
        f"lookAheadRejected={look_ahead_rejected} identifierIncomplete={identifier_incomplete} "
        "externalRequests=0 policyImpact=NONE_REPORT_ONLY"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
