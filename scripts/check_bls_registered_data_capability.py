from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import requests

from run_macro_event_clock_capability_probe import (
    BLS_CAPABILITY_SERIES_ID,
    BLS_REGISTERED_APPROVAL,
    BLS_REGISTERED_PASS_STATUS,
    BLS_REGISTERED_REQUEST_BUDGETS,
    BLS_REGISTERED_SCHEMA_VERSION,
    collect_bls_registered_data_capability,
    run_bls_registered_data_capability_probe,
)


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_KEY = "private-registration-key"


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.headers = {"Date": "Sat, 22 Aug 2026 12:00:00 GMT"}
        self.is_redirect = False
        self.is_permanent_redirect = False

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(
        self,
        *,
        status_code: int = 200,
        response_status: str = "REQUEST_SUCCEEDED",
        catalog: bool = True,
        series: bool = True,
        exception: requests.RequestException | None = None,
    ) -> None:
        self.status_code = status_code
        self.response_status = response_status
        self.catalog = catalog
        self.series = series
        self.exception = exception
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if self.exception is not None:
            raise self.exception
        rows = []
        if self.series:
            row: dict[str, Any] = {
                "seriesID": BLS_CAPABILITY_SERIES_ID,
                "data": [
                    {
                        "year": "2026",
                        "period": "M07",
                        "value": "private-value",
                        "footnotes": [],
                    }
                ],
            }
            if self.catalog:
                row["catalog"] = {
                    "series_title": "private-title",
                    "series_id": BLS_CAPABILITY_SERIES_ID,
                }
            rows.append(row)
        return FakeResponse(
            self.status_code,
            {
                "status": self.response_status,
                "message": ["private-message"] if self.response_status != "REQUEST_SUCCEEDED" else [],
                "Results": [{"series": rows}],
            },
        )


def _collect(
    session: FakeSession,
    *,
    key: str = PRIVATE_KEY,
) -> dict[str, Any]:
    return collect_bls_registered_data_capability(
        session=session,
        environment={"BLS_API_KEY": key},
        retrieved_at="2026-08-22T12:00:00Z",
    )


def main() -> int:
    missing_session = FakeSession()
    missing = _collect(missing_session, key="")
    assert missing["status"] == "BLS_REGISTRATION_KEY_NOT_VISIBLE_LOCALLY"
    assert missing["requestCounts"] == {
        key: 0 for key in BLS_REGISTERED_REQUEST_BUDGETS
    }
    assert missing_session.requests == []

    session = FakeSession()
    result = _collect(session)
    assert result["schemaVersion"] == BLS_REGISTERED_SCHEMA_VERSION
    assert result["status"] == BLS_REGISTERED_PASS_STATUS
    assert result["mode"] == "SHADOW_ONLY_REGISTERED_DATA_PROBE"
    expected_counts = {key: 0 for key in BLS_REGISTERED_REQUEST_BUDGETS}
    expected_counts["blsRegisteredData"] = 1
    assert result["requestCounts"] == expected_counts
    assert result["externalRequestCount"] == 1
    assert result["requestBudgetCompliant"] is True
    assert result["httpStatusCategory"] == "2xx"
    assert len(result["responseSha256"]) == 64
    assert result["seriesRows"] == 1
    assert result["observationRows"] == 1
    assert result["catalogPresentRows"] == 1
    assert result["effectivePeriodRows"] == 1
    assert result["publicationTimestampAvailable"] is False
    assert result["registrationKeyUsed"] is True
    assert result["calendarStatus"] == "BLS_CALENDAR_SOURCE_ACCESS_BLOCKED"
    assert result["unknownOrUnclassifiedRows"] == 0
    assert result["rawResponseStored"] is False
    assert result["secretStoredOrPrinted"] is False
    assert result["canonicalSourceChanged"] is False
    assert result["policyImpact"] == "NONE_REPORT_ONLY"
    assert result["accountHeaderUsed"] is False
    assert result["orderEndpointUsed"] is False
    assert result["brokerOrSidecarStateMutation"] is False
    assert len(session.requests) == 1
    method, url, kwargs = session.requests[0]
    assert method == "POST"
    assert url == "https://api.bls.gov/publicAPI/v2/timeseries/data/"
    assert kwargs["json"]["registrationkey"] == PRIVATE_KEY
    assert kwargs["json"]["seriesid"] == [BLS_CAPABILITY_SERIES_ID]
    assert kwargs["json"]["catalog"] is True

    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (PRIVATE_KEY, "private-value", "private-title", "private-message"):
        assert forbidden not in rendered

    for status_code in (401, 403):
        assert _collect(FakeSession(status_code=status_code))["status"] == (
            "BLS_AUTH_OR_REGISTRATION_BLOCKED"
        )
    assert _collect(FakeSession(status_code=429))["status"] == (
        "BLS_REGISTERED_API_RATE_LIMITED"
    )
    for failed_session in (
        FakeSession(status_code=500),
        FakeSession(exception=requests.Timeout("private-timeout")),
    ):
        assert _collect(failed_session)["status"] == (
            "BLS_REGISTERED_API_TRANSIENT_FAILURE"
        )
    assert _collect(FakeSession(response_status="REQUEST_FAILED"))["status"] == (
        "BLS_REGISTERED_API_SCHEMA_INVALID"
    )
    assert _collect(FakeSession(catalog=False))["status"] == (
        "BLS_REGISTERED_API_SCHEMA_INVALID"
    )
    assert _collect(FakeSession(series=False))["status"] == (
        "BLS_REGISTERED_API_SCHEMA_INVALID"
    )
    assert _collect(FakeSession()) == result

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        run_session = FakeSession()
        written = run_bls_registered_data_capability_probe(
            session=run_session,
            environment={"BLS_API_KEY": PRIVATE_KEY},
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-22T12:00:00Z",
            approval=BLS_REGISTERED_APPROVAL,
        )
        assert written["status"] == BLS_REGISTERED_PASS_STATUS
        request_count = len(run_session.requests)
        try:
            run_bls_registered_data_capability_probe(
                session=run_session,
                environment={"BLS_API_KEY": PRIVATE_KEY},
                output_path=root / "result.json",
                sentinel_path=root / "sentinel.json",
                retrieved_at="2026-08-22T12:01:00Z",
                approval=BLS_REGISTERED_APPROVAL,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate registered probe must fail before network")
        assert len(run_session.requests) == request_count

    workflow = (
        ROOT / ".github/workflows/bls-registered-data-capability-probe.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert BLS_REGISTERED_APPROVAL in workflow
    assert "secrets.BLS_API_KEY" in workflow
    for forbidden in (
        "schedule:",
        "pull_request:",
        "push:",
        "secrets.BEA_API_KEY",
        "secrets.FRED_API_KEY",
        "bls.ics",
        "_sched",
    ):
        assert forbidden not in workflow

    print(
        "[BLS_REGISTERED_DATA_CAPABILITY] PASS "
        "requests=1 calendarRequests=0 rawStored=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
