from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from run_macro_event_clock_capability_probe import (
    PASS_STATUS,
    collect_macro_event_clock_capability,
    macro_event_clock_runtime_decision,
    run_macro_event_clock_capability_probe,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, sort_keys=True).encode("utf-8")
        )
        self.headers = {
            "Content-Type": content_type,
            "Date": "Fri, 21 Aug 2026 12:00:00 GMT",
            "Last-Modified": "Fri, 21 Aug 2026 11:00:00 GMT",
        }
        self.is_redirect = False
        self.is_permanent_redirect = False

    def json(self) -> Any:
        if isinstance(self._payload, bytes):
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(
        self,
        *,
        fred_status: int = 200,
        bls_calendar_status: int = 200,
        bea_api_error_code: str | None = None,
    ) -> None:
        self.fred_status = fred_status
        self.bls_calendar_status = bls_calendar_status
        self.bea_api_error_code = bea_api_error_code
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if "fomccalendars" in url:
            return FakeResponse(
                200,
                b"<html><title>Federal Open Market Committee</title>"
                b"<h1>Meeting calendars and information</h1>"
                b"<h2>2026 FOMC Meetings</h2><div>September 15-16</div></html>",
                content_type="text/html",
            )
        if url.endswith("bls.ics"):
            return FakeResponse(
                self.bls_calendar_status,
                b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
                b"DTSTART:20260821T123000Z\r\n"
                b"SUMMARY:Consumer Price Index\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
                content_type="text/calendar",
            )
        if "api.bls.gov" in url:
            return FakeResponse(
                200,
                {
                    "status": "REQUEST_SUCCEEDED",
                    "Results": {
                        "series": [
                            {
                                "seriesID": "PRIVATE-BLS-SERIES",
                                "data": [
                                    {
                                        "year": "2026",
                                        "period": "M07",
                                        "value": "private-value",
                                        "footnotes": [{"code": "R", "text": "Revised"}],
                                    }
                                ],
                            }
                        ]
                    },
                },
            )
        if "bea.gov/news/schedule" in url:
            return FakeResponse(
                200,
                b"<html><title>Release Schedule</title><h1>Release Schedule</h1>"
                b"<div>August 26 8:30 AM GDP (Second Estimate)</div></html>",
                content_type="text/html",
            )
        if "apps.bea.gov/api/data" in url:
            return FakeResponse(
                200,
                {
                    "BEAAPI": {
                        "Results": {
                            "Error": {"APIErrorCode": self.bea_api_error_code}
                        }
                    }
                }
                if self.bea_api_error_code is not None
                else {
                    "BEAAPI": {
                        "Results": {
                            "Dataset": [
                                {
                                    "DatasetName": "PRIVATE_DATASET",
                                    "DatasetDescription": "private description",
                                }
                            ]
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
                            "name": "Private release name",
                            "realtime_start": "2026-08-21",
                            "realtime_end": "2026-08-21",
                        }
                    ]
                }
                if self.fred_status == 200
                else {"error_code": 403, "error_message": "private auth detail"},
            )
        if url.endswith("/fred/releases/dates"):
            return FakeResponse(
                200,
                {
                    "release_dates": [
                        {
                            "release_id": 1,
                            "release_name": "Private release name",
                            "date": "2026-08-21",
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def _environment() -> dict[str, str]:
    return {
        "BEA_API_KEY": "private-bea-key",
        "FRED_API_KEY": "private-fred-key",
    }


def main() -> int:
    assert macro_event_clock_runtime_decision({}) == (
        False,
        "bea_api_key_missing",
    )
    assert macro_event_clock_runtime_decision(_environment()) == (
        True,
        "bounded_shadow_probe_enabled",
    )

    session = FakeSession()
    result = collect_macro_event_clock_capability(
        session=session,
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert result["schemaVersion"] == "macro-event-clock-shadow-capability-v1"
    assert result["status"] == PASS_STATUS
    assert result["mode"] == "SHADOW_ONLY"
    assert result["requestCounts"] == {
        "federalReserveCalendar": 1,
        "blsMetadata": 1,
        "blsData": 1,
        "beaMetadata": 1,
        "beaData": 1,
        "fredMetadata": 1,
        "fredData": 1,
    }
    assert result["externalRequestCount"] == 7
    assert result["requestBudgetCompliant"] is True
    assert result["publicationEffectiveTimestampSeparated"] is True
    assert result["lookAheadViolationRows"] == 0
    assert result["unknownOrUnclassifiedRows"] == 0
    assert result["canonicalSourceChanged"] is False
    assert result["policyImpact"] == "NONE_REPORT_ONLY"
    assert result["stage4To7Impact"] == "NONE"
    assert result["rawResponseStored"] is False
    assert result["secretValuesStoredOrPrinted"] is False
    assert result["paginationUsed"] is False
    assert result["retryCount"] == 0
    assert result["recurringProviderEnabled"] is False
    assert result["brokerOrSidecarStateMutation"] is False
    assert {row["sourceId"] for row in result["sources"]} == {
        "FEDERAL_RESERVE_FOMC_CALENDAR",
        "BLS_RELEASE_CALENDAR_AND_DATA",
        "BEA_RELEASE_SCHEDULE_AND_DATASET_METADATA",
        "FRED_RELEASE_METADATA_AND_SOURCE_DATES",
    }
    assert all(row["status"] == "SOURCE_CAPABILITY_PASS" for row in result["sources"])
    assert all(row["publicationEffectiveSeparated"] is True for row in result["sources"])
    assert len(result["evidenceSha256"]) == 64

    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (
        "private-bea-key",
        "private-fred-key",
        "PRIVATE-BLS-SERIES",
        "private-value",
        "PRIVATE_DATASET",
        "Private release name",
        "private auth detail",
    ):
        assert forbidden not in rendered

    repeated = collect_macro_event_clock_capability(
        session=FakeSession(),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert repeated == result

    blocked = collect_macro_event_clock_capability(
        session=FakeSession(fred_status=403),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert blocked["status"] == "MACRO_EVENT_CLOCK_AUTH_OR_ENTITLEMENT_BLOCKED"
    assert (
        blocked["primaryBlocker"]
        == "FRED_RELEASE_METADATA_AND_SOURCE_DATES_AUTH_OR_ENTITLEMENT_BLOCKED"
    )
    assert blocked["analysisContinued"] is True
    assert blocked["unknownOrUnclassifiedRows"] == 0

    bad_key = collect_macro_event_clock_capability(
        session=FakeSession(fred_status=400),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert bad_key["status"] == "MACRO_EVENT_CLOCK_AUTH_OR_ENTITLEMENT_BLOCKED"

    public_access_blocked = collect_macro_event_clock_capability(
        session=FakeSession(bls_calendar_status=403),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert public_access_blocked["status"] == "MACRO_EVENT_CLOCK_SOURCE_ACCESS_BLOCKED"
    assert public_access_blocked["sources"][1]["status"] == "SOURCE_ACCESS_BLOCKED"
    assert public_access_blocked["requestCounts"]["blsData"] == 1
    assert public_access_blocked["sources"][1]["dataContractStatus"] == "PASS"

    bea_error = collect_macro_event_clock_capability(
        session=FakeSession(bea_api_error_code="private"),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert bea_error["status"] == "MACRO_EVENT_CLOCK_RESPONSE_CONTRACT_INVALID"
    assert bea_error["sources"][2]["primaryBlocker"] == "BEA_API_ERROR_SUBTYPE_UNVERIFIED"

    bea_bad_key = collect_macro_event_clock_capability(
        session=FakeSession(bea_api_error_code="1"),
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert bea_bad_key["status"] == "MACRO_EVENT_CLOCK_AUTH_OR_ENTITLEMENT_BLOCKED"
    assert bea_bad_key["sources"][2]["primaryBlocker"] == "BEA_API_USER_ID_REJECTED"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reproof_session = FakeSession()
        reproof = run_macro_event_clock_capability_probe(
            session=reproof_session,
            environment=_environment(),
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-21T12:00:00Z",
        )
        assert reproof["status"] == PASS_STATUS
        assert (root / "result.json").exists()
        sentinel = json.loads((root / "sentinel.json").read_text(encoding="utf-8"))
        assert sentinel["status"] == "COMPLETE"
        assert sentinel["requestCounts"] == reproof["requestCounts"]
        assert len(sentinel["resultSha256"]) == 64

        request_count = len(reproof_session.requests)
        try:
            run_macro_event_clock_capability_probe(
                session=reproof_session,
                environment=_environment(),
                output_path=root / "result.json",
                sentinel_path=root / "sentinel.json",
                retrieved_at="2026-08-21T12:01:00Z",
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate probe must fail before network")
        assert len(reproof_session.requests) == request_count

    workflow = (
        ROOT / ".github/workflows/macro-event-clock-capability-probe.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert (
        "AUTHORIZE MACRO EVENT CLOCK SHADOW READ-ONLY CAPABILITY PROBE"
        in workflow
    )
    job_prelude, steps = workflow.split("    steps:", 1)
    probe_step = steps.split(
        "      - name: Run bounded macro event clock probe", 1
    )[1].split("      - name:", 1)[0]
    for secret in ("BEA_API_KEY", "FRED_API_KEY"):
        assert f"secrets.{secret}" not in job_prelude
        assert f"secrets.{secret}" in probe_step

    assert all(
        "offset" not in (request[2].get("params") or {})
        for request in session.requests
    )
    assert len(session.requests) == 7

    print(
        "[MACRO_EVENT_CLOCK_CAPABILITY] PASS "
        "sources=4 requests=7 rawStored=false policyImpact=NONE_REPORT_ONLY"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
