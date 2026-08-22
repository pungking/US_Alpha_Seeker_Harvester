from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import requests

from official_shadow_runtime import (
    build_collection_contract,
    build_durable_collection_sentinel,
    classify_existing_artifact,
    classify_durable_collection_sentinel,
    dispatch_official_shadow_alert,
    durable_collection_sentinel_filename,
    finish_collection_sentinel,
    persist_shadow_artifact,
    reserve_collection_sentinel,
    reuse_existing_artifact,
    canonical_sha256,
)
from run_macro_event_clock_capability_probe import (
    MACRO_SHADOW_PASS_STATUS,
    MACRO_SHADOW_REQUEST_BUDGETS,
    MACRO_SHADOW_SOURCE_IDS,
    MACRO_SHADOW_SOURCE_WINDOW_BASES,
    build_macro_event_clock_shadow_not_run_result,
    collect_macro_event_clock_shadow,
    macro_event_clock_shadow_runtime_decision,
)
from sec_finra_shadow_evidence import (
    REQUEST_BUDGETS as SEC_FINRA_REQUEST_BUDGETS,
    SOURCE_IDS as SEC_FINRA_SOURCE_IDS,
    SOURCE_WINDOW_BASES as SEC_FINRA_SOURCE_WINDOW_BASES,
    sec_finra_shadow_runtime_decision,
)


ROOT = Path(__file__).resolve().parents[1]
READINESS_PATH = ROOT / "fixtures/official_shadow_producer_readiness.json"


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, sort_keys=True).encode("utf-8")
        )
        self.headers = {
            "Date": "Fri, 21 Aug 2026 12:00:00 GMT",
            "Last-Modified": "Fri, 21 Aug 2026 11:00:00 GMT",
        }
        self.is_redirect = False
        self.is_permanent_redirect = False

    def json(self) -> Any:
        if isinstance(self._payload, bytes):
            raise ValueError("not json")
        return self._payload


class MacroSession:
    def __init__(
        self,
        fred_status: int = 200,
        failure_url_token: str | None = None,
    ) -> None:
        self.fred_status = fred_status
        self.failure_url_token = failure_url_token
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if self.failure_url_token and self.failure_url_token in url:
            raise requests.Timeout("private timeout detail")
        if "fomccalendars" in url:
            return FakeResponse(
                200,
                b"<html><h1>Federal Open Market Committee</h1>"
                b"<div>Meeting calendars</div><div>September</div></html>",
            )
        if "bea.gov/news/schedule" in url:
            return FakeResponse(
                200,
                b"<html><h1>Release Schedule</h1><div>8:30 AM</div></html>",
            )
        if "apps.bea.gov/api/data" in url:
            return FakeResponse(
                200,
                {
                    "BEAAPI": {
                        "Results": {
                            "Dataset": [{"DatasetName": "PRIVATE_DATASET"}]
                        }
                    }
                },
            )
        if url.endswith("/fred/releases"):
            return FakeResponse(
                self.fred_status,
                {
                    "releases": [
                        {
                            "id": 1,
                            "name": "PRIVATE_RELEASE",
                            "realtime_start": "2026-08-21",
                            "realtime_end": "2026-08-21",
                        }
                    ]
                },
            )
        if url.endswith("/fred/releases/dates"):
            return FakeResponse(
                200,
                {
                    "release_dates": [
                        {
                            "release_id": 1,
                            "release_name": "PRIVATE_RELEASE",
                            "date": "2026-08-21",
                        }
                    ]
                },
            )
        if url == "https://api.bls.gov/publicAPI/v2/timeseries/data/":
            return FakeResponse(
                200,
                {
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {
                        "series": [
                            {
                                "seriesID": "CUUR0000SA0",
                                "catalog": {"series_title": "PRIVATE_TITLE"},
                                "data": [{"year": "2026", "period": "M07"}],
                            }
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def _macro_environment() -> dict[str, str]:
    return {
        "MACRO_EVENT_CLOCK_SHADOW_PROVIDER_ENABLED": "true",
        "BEA_API_KEY": "private-bea-key",
        "FRED_API_KEY": "private-fred-key",
        "BLS_API_KEY": "private-bls-key",
    }


def _test_collection_contract_and_reuse() -> None:
    assert sec_finra_shadow_runtime_decision(
        {"SEC_FINRA_SHADOW_PROVIDER_ENABLED": "true"}
    ) == (False, "sec_fair_access_contact_missing")
    source_ids = ("SOURCE_B", "SOURCE_A")
    source_window_bases = {
        "SOURCE_A": "SOURCE_A_PUBLICATION_OBSERVATION_DATE_ET",
        "SOURCE_B": "SOURCE_B_PUBLICATION_OBSERVATION_DATE_ET",
    }
    first = build_collection_contract(
        source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
        schema_version="sec-finra-shadow-evidence-v1",
        source_ids=source_ids,
        source_window_bases=source_window_bases,
        retrieved_at="2026-08-21T22:00:00Z",
    )
    second = build_collection_contract(
        source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
        schema_version="sec-finra-shadow-evidence-v1",
        source_ids=reversed(source_ids),
        source_window_bases=source_window_bases,
        retrieved_at="2026-08-22T03:00:00Z",
    )
    next_window = build_collection_contract(
        source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
        schema_version="sec-finra-shadow-evidence-v1",
        source_ids=source_ids,
        source_window_bases=source_window_bases,
        retrieved_at="2026-08-22T04:01:00Z",
    )
    assert first == second
    assert first["collectionWindow"] == "2026-08-21"
    assert first["collectionWindowBasis"] == "AMERICA_NEW_YORK_PUBLICATION_DATE"
    assert first["collectionWindowContractStatus"] == (
        "VERIFIED_SOURCE_PUBLICATION_OBSERVATION_WINDOWS"
    )
    assert set(first["sourceCollectionWindows"]) == set(source_ids)
    assert len(first["requestScopeSha256"]) == 64
    assert len(first["collectionKey"]) == 64
    assert next_window["collectionWindow"] == "2026-08-22"
    assert next_window["collectionKey"] != first["collectionKey"]
    try:
        build_collection_contract(
            source_family="INVALID",
            schema_version="invalid-v1",
            source_ids=("SOURCE_A",),
            source_window_bases={},
            retrieved_at="2026-08-21T22:00:00Z",
        )
    except ValueError as exc:
        assert str(exc) == "source_window_contract_required"
    else:
        raise AssertionError("missing source publication window must fail closed")

    existing = {
        "schemaVersion": "sec-finra-shadow-evidence-v1",
        "status": "SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE",
        **first,
        "requestCounts": {"secDiscovery": 3, "finraOauth": 1},
        "externalRequestCount": 4,
        "evidenceHashBasis": "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH",
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "rawResponseStored": False,
        "unknownOrUnclassifiedRows": 0,
    }
    existing["evidenceSha256"] = canonical_sha256(existing)
    assert classify_existing_artifact(
        existing,
        first,
        success_statuses={"SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"},
    ) == "EXISTING_MATCHED_SUCCESS"
    reused = reuse_existing_artifact(
        existing,
        request_counter_names=("secDiscovery", "finraOauth"),
        matched_status="EXISTING_MATCHED_SUCCESS",
    )
    assert reused["runtimeAction"] == "EXISTING_MATCHED_SHADOW_REUSED"
    assert reused["requestCounts"] == {"finraOauth": 0, "secDiscovery": 0}
    assert reused["externalRequestCount"] == 0
    assert reused["sourceEvidenceSha256"] == existing["evidenceSha256"]
    assert len(reused["evidenceSha256"]) == 64
    assert reused["canonicalSourceChanged"] is False
    assert reused["policyImpact"] == "NONE_REPORT_ONLY"

    failed = {**existing, "status": "SEC_FINRA_SHADOW_PARTIAL"}
    failed.pop("evidenceSha256")
    failed["evidenceSha256"] = canonical_sha256(failed)
    assert classify_existing_artifact(
        failed,
        first,
        success_statuses={"SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"},
    ) == "EXISTING_MATCHED_FAILURE"
    assert classify_existing_artifact(
        existing,
        next_window,
        success_statuses={"SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"},
    ) == "COLLECTION_CONTRACT_MISMATCH"
    tampered = {**existing, "externalRequestCount": 99}
    assert classify_existing_artifact(
        tampered,
        first,
        success_statuses={"SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"},
    ) == "COLLECTION_CONTRACT_MISMATCH"


def _test_sentinel_and_publish() -> None:
    contract = build_collection_contract(
        source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
        schema_version="sec-finra-shadow-evidence-v1",
        source_ids=("SEC", "FINRA"),
        source_window_bases={
            "SEC": "SEC_PUBLICATION_OBSERVATION_DATE_ET",
            "FINRA": "FINRA_PUBLICATION_OBSERVATION_DATE_ET",
        },
        retrieved_at="2026-08-21T22:00:00Z",
    )
    durable_name = durable_collection_sentinel_filename(contract)
    assert durable_name.startswith("OFFICIAL_SHADOW_SENTINEL_")
    assert durable_name.endswith(".json")
    durable = build_durable_collection_sentinel(
        contract,
        status="IN_PROGRESS",
        reserved_at="2026-08-21T22:00:00Z",
    )
    assert classify_durable_collection_sentinel(durable, contract) == (
        "EXISTING_IN_PROGRESS"
    )
    completed = build_durable_collection_sentinel(
        contract,
        status="COMPLETE",
        reserved_at="2026-08-21T22:00:00Z",
        completed_at="2026-08-21T22:01:00Z",
        artifact_sha256="f" * 64,
        request_counts={"sec": 1},
    )
    assert classify_durable_collection_sentinel(completed, contract) == (
        "EXISTING_COMPLETE"
    )
    assert classify_durable_collection_sentinel(
        {**completed, "status": "FAILED"}, contract
    ) == "DURABLE_SENTINEL_INVALID"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reserved = reserve_collection_sentinel(
            root,
            source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
            collection_key="b" * 64,
            reserved_at="2026-08-21T22:00:00Z",
        )
        assert reserved["status"] == "RESERVED"
        duplicate = reserve_collection_sentinel(
            root,
            source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
            collection_key="b" * 64,
            reserved_at="2026-08-21T22:01:00Z",
        )
        assert duplicate["status"] == "EXISTING_IN_PROGRESS"
        finish_collection_sentinel(
            Path(reserved["path"]),
            status="FAILED",
            completed_at="2026-08-21T22:02:00Z",
            artifact_sha256=None,
            request_counts={"secDiscovery": 1},
        )
        failed = reserve_collection_sentinel(
            root,
            source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
            collection_key="b" * 64,
            reserved_at="2026-08-21T22:03:00Z",
        )
        assert failed["status"] == "EXISTING_FAILED"

    writes: list[str] = []
    uploads: list[str] = []

    def writer(path: str, payload: dict[str, Any], label: str) -> None:
        del payload, label
        writes.append(path)

    def uploader(filename: str, payload: dict[str, Any], parent_id: str) -> None:
        del payload, parent_id
        uploads.append(filename)

    result = persist_shadow_artifact(
        {
            "schemaVersion": "example-v1",
            "status": "SOURCE_PASS",
            "evidenceSha256": "c" * 64,
            "collectionKey": contract["collectionKey"],
        },
        local_path="state/example.json",
        current_filename="EXAMPLE.json",
        archive_prefix="EXAMPLE",
        parent_id="private-parent",
        writer=writer,
        uploader=uploader,
    )
    assert result["artifactPersistenceStatus"] == "LOCAL_AND_DRIVE_PUBLISHED"
    assert writes == ["state/example.json"]
    assert len(uploads) == 2 and uploads[-1] == "EXAMPLE.json"
    hashed_result = dict(result)
    result_sha256 = hashed_result.pop("evidenceSha256")
    assert canonical_sha256(hashed_result) == result_sha256

    def failing_uploader(
        filename: str,
        payload: dict[str, Any],
        parent_id: str,
    ) -> None:
        del payload, parent_id
        if filename == "EXAMPLE.json":
            raise RuntimeError("private drive detail")

    failed_result = persist_shadow_artifact(
        {
            "schemaVersion": "example-v1",
            "status": "SOURCE_PASS",
            "evidenceSha256": "d" * 64,
            "collectionKey": contract["collectionKey"],
        },
        local_path="state/example.json",
        current_filename="EXAMPLE.json",
        archive_prefix="EXAMPLE",
        parent_id="private-parent",
        writer=writer,
        uploader=failing_uploader,
    )
    assert failed_result["artifactPersistenceStatus"] == "DRIVE_PUBLISH_FAILED"
    assert failed_result["artifactPersistenceErrorCategory"] == "RuntimeError"
    assert "private drive detail" not in json.dumps(failed_result)


def _test_alert_deduplication() -> None:
    messages: list[str] = []

    def sender(message: str, *, channel: str) -> dict[str, Any]:
        assert channel == "alert"
        messages.append(message)
        return {"attempted": True, "delivered": True, "safeErrorCategory": None}

    result = {
        "sourceFamily": "MACRO_EVENT_CLOCK_OFFICIAL_EVIDENCE",
        "collectionKey": "e" * 64,
        "status": "MACRO_EVENT_CLOCK_SHADOW_PARTIAL",
        "primaryBlocker": "FRED_RELEASE_CONTRACT_INVALID",
        "externalRequestCount": 5,
        "canonicalSourceChanged": False,
        "analysisContinued": True,
        "privateSecret": "never-render-this",
    }
    fingerprints: set[str] = set()
    delivered = dispatch_official_shadow_alert(
        result,
        success_statuses={MACRO_SHADOW_PASS_STATUS},
        sent_fingerprints=fingerprints,
        sender=sender,
    )
    duplicate = dispatch_official_shadow_alert(
        result,
        success_statuses={MACRO_SHADOW_PASS_STATUS},
        sent_fingerprints=fingerprints,
        sender=sender,
    )
    assert delivered["status"] == "ALERT_DELIVERED"
    assert duplicate["status"] == "ALERT_SUPPRESSED_DUPLICATE"
    assert len(messages) == 1
    assert "never-render-this" not in messages[0]

    unknown_count = {**result, "collectionKey": "f" * 64}
    unknown_count["externalRequestCount"] = None
    dispatch_official_shadow_alert(
        unknown_count,
        success_statuses={MACRO_SHADOW_PASS_STATUS},
        sent_fingerprints=fingerprints,
        sender=sender,
    )
    assert "Requests: `unverified`" in messages[-1]


def _test_macro_shadow_contract() -> None:
    macro_collection = build_collection_contract(
        source_family="MACRO_EVENT_CLOCK_OFFICIAL_EVIDENCE",
        schema_version="macro-event-clock-shadow-v1",
        source_ids=MACRO_SHADOW_SOURCE_IDS,
        source_window_bases=MACRO_SHADOW_SOURCE_WINDOW_BASES,
        retrieved_at="2026-08-21T22:00:00Z",
    )
    assert macro_collection["sourceCollectionWindows"][
        "BLS_OFFICIAL_RELEASE_CALENDAR"
    ]["collectionWindow"] == "NOT_COLLECTED"
    assert macro_event_clock_shadow_runtime_decision({}) == (
        False,
        "shadow_provider_disabled",
    )
    missing_key = dict(_macro_environment())
    missing_key.pop("BLS_API_KEY")
    assert macro_event_clock_shadow_runtime_decision(missing_key) == (
        False,
        "bls_api_key_missing",
    )
    assert macro_event_clock_shadow_runtime_decision(_macro_environment()) == (
        True,
        "server_side_shadow_enabled",
    )

    session = MacroSession()
    result = collect_macro_event_clock_shadow(
        session=session,
        environment=_macro_environment(),
        retrieved_at="2026-08-21T22:00:00Z",
    )
    assert result["schemaVersion"] == "macro-event-clock-shadow-v1"
    assert result["status"] == MACRO_SHADOW_PASS_STATUS
    assert result["requestCounts"] == {
        "beaData": 1,
        "beaMetadata": 1,
        "blsHtmlFallback": 0,
        "blsIcal": 0,
        "blsRegisteredData": 1,
        "federalReserveCalendar": 1,
        "finra": 0,
        "fredData": 1,
        "fredMetadata": 1,
        "sec": 0,
        "toss": 0,
    }
    assert result["requestBudgets"] == MACRO_SHADOW_REQUEST_BUDGETS
    assert result["externalRequestCount"] == 6
    assert result["requestBudgetCompliant"] is True
    assert result["blsCalendarStatus"] == "BLS_CALENDAR_STATIC_EGRESS_REQUIRED"
    assert result["publicationEffectiveTimestampSeparated"] is True
    assert result["unknownOrUnclassifiedRows"] == 0
    assert result["rawResponseStored"] is False
    assert result["canonicalSourceChanged"] is False
    assert result["policyImpact"] == "NONE_REPORT_ONLY"
    assert result["analysisEligible"] is False
    for source in result["sources"]:
        assert source["sourceStatus"] == source["status"]
        assert source["retrievedAt"] == "2026-08-21T22:00:00Z"
        assert source["publicationTimestampBasis"]
        assert source["effectivePeriodBasis"]
        assert source["revisionOrAmendmentStatus"]
        assert source["publicationDelayStatus"]
        assert isinstance(source["requestCounts"], dict)
    bls_calendar = next(
        source
        for source in result["sources"]
        if source["sourceId"] == "BLS_OFFICIAL_RELEASE_CALENDAR"
    )
    assert bls_calendar["requestCounts"] == {
        "blsHtmlFallback": 0,
        "blsIcal": 0,
    }
    assert all("bls.ics" not in url for _, url, _ in session.requests)
    assert all("schedule/news_release" not in url for _, url, _ in session.requests)
    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (
        "private-bea-key",
        "private-fred-key",
        "private-bls-key",
        "PRIVATE_DATASET",
        "PRIVATE_RELEASE",
        "PRIVATE_TITLE",
    ):
        assert forbidden not in rendered

    disabled = build_macro_event_clock_shadow_not_run_result(
        "shadow_provider_disabled"
    )
    assert disabled["externalRequestCount"] == 0
    assert disabled["blsCalendarStatus"] == "BLS_CALENDAR_STATIC_EGRESS_REQUIRED"
    assert disabled["canonicalSourceChanged"] is False
    assert disabled["policyImpact"] == "NONE_REPORT_ONLY"

    rate_limited_session = MacroSession(fred_status=429)
    rate_limited = collect_macro_event_clock_shadow(
        session=rate_limited_session,
        environment=_macro_environment(),
        retrieved_at="2026-08-21T22:00:00Z",
    )
    assert rate_limited["status"] == "MACRO_EVENT_CLOCK_SHADOW_PARTIAL"
    assert rate_limited["unknownOrUnclassifiedRows"] == 0
    assert rate_limited["requestCounts"]["fredMetadata"] == 1
    assert rate_limited["requestCounts"]["fredData"] == 0

    timed_out_session = MacroSession(failure_url_token="/fred/releases")
    timed_out = collect_macro_event_clock_shadow(
        session=timed_out_session,
        environment=_macro_environment(),
        retrieved_at="2026-08-21T22:00:00Z",
    )
    assert timed_out["status"] == "MACRO_EVENT_CLOCK_SHADOW_PARTIAL"
    assert timed_out["requestCounts"]["fredMetadata"] == 1
    assert timed_out["requestCounts"]["fredData"] == 0
    assert "private timeout detail" not in json.dumps(timed_out, sort_keys=True)


def _test_static_wiring() -> None:
    harvester = (ROOT / "harvester.py").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
    one_shot_workflow = (
        ROOT / ".github/workflows/official-shadow-producer-one-shot.yml"
    ).read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for expected in (
        '"MACRO_EVENT_CLOCK_SHADOW_PROVIDER_ENABLED", False',
        "ensure_macro_event_clock_shadow",
        'GITHUB_EVENT_NAME == "schedule"',
        'summary_payload["macroEventClockShadow"]',
        "build_collection_contract",
        "reserve_collection_sentinel",
        "persist_shadow_artifact",
        "durable_collection_sentinel_filename",
        "run_official_shadow_producer_one_shot",
        "--official-shadow-producer-one-shot",
    ):
        assert expected in harvester
    for expected in (
        "SEC_FINRA_SHADOW_PROVIDER_ENABLED: 'false'",
        "MACRO_EVENT_CLOCK_SHADOW_PROVIDER_ENABLED: 'false'",
        "BEA_API_KEY: ${{ secrets.BEA_API_KEY }}",
        "FRED_API_KEY: ${{ secrets.FRED_API_KEY }}",
        "BLS_API_KEY: ${{ secrets.BLS_API_KEY }}",
        "state/macro-event-clock-shadow.json",
    ):
        assert expected in workflow
    for forbidden in (
        "BLS_API_KEY=", "registrationkey=", "bls.ics?registrationkey"
    ):
        assert forbidden not in workflow + one_shot_workflow
    one_shot_function = harvester.split(
        "def run_official_shadow_producer_one_shot", 1
    )[1].split("def ensure_toss_read_only_capability_once", 1)[0]
    assert one_shot_function.index("OFFICIAL_SHADOW_ONE_SHOT_APPROVAL") < (
        one_shot_function.index('find_file_id("US_Alpha_Seeker")')
    )
    assert "workflow_dispatch:" in one_shot_workflow
    assert "schedule:" not in one_shot_workflow
    assert "pull_request:" not in one_shot_workflow
    assert "push:" not in one_shot_workflow
    assert "SEC_FINRA_SHADOW_PROVIDER_ENABLED: 'true'" in one_shot_workflow
    assert "MACRO_EVENT_CLOCK_SHADOW_PROVIDER_ENABLED: 'true'" in one_shot_workflow
    assert "--official-shadow-producer-one-shot" in one_shot_workflow
    assert "BLS_API_KEY: ${{ secrets.BLS_API_KEY }}" in one_shot_workflow
    assert "AUTHORIZE OFFICIAL SHADOW PRODUCER BOUNDED ONE-SHOT" in readme
    assert "AUTHORIZE OFFICIAL SHADOW PRODUCER BOUNDED ONE-SHOT" in (
        one_shot_workflow
    )


def _test_static_readiness_artifact() -> None:
    readiness = json.loads(READINESS_PATH.read_text(encoding="utf-8"))
    assert readiness["schemaVersion"] == "official-shadow-producer-readiness-v1"
    assert readiness["status"] == (
        "OFFICIAL_SHADOW_PRODUCER_READY_WITH_BLS_CALENDAR_EXCLUDED"
    )
    assert readiness["credentialCapabilityStatus"] == (
        "PASS_ALL_CREDENTIAL_CAPABILITIES"
    )
    assert readiness["enabledByDefault"] is False
    assert readiness["recurringActivationAuthorized"] is False
    assert readiness["requestBudget"] == {
        "secFinra": SEC_FINRA_REQUEST_BUDGETS,
        "macro": MACRO_SHADOW_REQUEST_BUDGETS,
    }
    assert set(SEC_FINRA_SOURCE_WINDOW_BASES) == set(SEC_FINRA_SOURCE_IDS)
    assert set(MACRO_SHADOW_SOURCE_WINDOW_BASES) == set(MACRO_SHADOW_SOURCE_IDS)
    classifications = {
        row["sourceId"]: row["status"]
        for row in readiness["sourceClassifications"]
    }
    assert set(classifications) == {
        *SEC_FINRA_SOURCE_IDS,
        "FEDERAL_RESERVE_FOMC_CALENDAR",
        "FRED_RELEASE_METADATA_AND_SOURCE_DATES",
        "BEA_RELEASE_SCHEDULE_AND_DATASET_METADATA",
        "BLS_REGISTERED_DATA_OBSERVATION_CATALOG",
        "BLS_OFFICIAL_RELEASE_CALENDAR",
    }
    assert classifications["BLS_OFFICIAL_RELEASE_CALENDAR"] == (
        "SOURCE_EXCLUDED_STATIC_EGRESS_REQUIRED"
    )
    assert all(
        status == "EXISTING_RUNTIME_PRODUCER_READY"
        for source_id, status in classifications.items()
        if source_id != "BLS_OFFICIAL_RELEASE_CALENDAR"
    )
    assert readiness["unknownOrUnclassifiedRows"] == 0
    assert readiness["externalRequestCount"] == 0
    assert readiness["canonicalSourceChanged"] is False
    assert readiness["policyImpact"] == "NONE_REPORT_ONLY"
    assert readiness["brokerOrSidecarStateMutation"] is False
    assert len(canonical_sha256(readiness)) == 64


def main() -> int:
    _test_collection_contract_and_reuse()
    _test_sentinel_and_publish()
    _test_alert_deduplication()
    _test_macro_shadow_contract()
    _test_static_wiring()
    _test_static_readiness_artifact()
    print(
        "[OFFICIAL_SHADOW_PRODUCER_READINESS] PASS "
        "externalRequests=0 defaultsEnabled=false blsCalendar=excluded"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
