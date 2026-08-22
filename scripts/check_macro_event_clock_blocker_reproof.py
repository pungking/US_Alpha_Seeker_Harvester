from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import requests

from run_macro_event_clock_capability_probe import (
    BLS_CALENDAR_MAC_APPROVAL,
    BLS_CALENDAR_MAC_REQUEST_BUDGETS,
    BLS_CALENDAR_MAC_SCHEMA_VERSION,
    BLOCKER_ONLY_APPROVAL,
    BLOCKER_ONLY_PASS_STATUS,
    BLOCKER_ONLY_REQUEST_BUDGETS,
    BLOCKER_ONLY_SCHEMA_VERSION,
    collect_bls_calendar_mac_capability,
    collect_macro_event_clock_blocker_reproof,
    run_bls_calendar_mac_capability_probe,
    run_macro_event_clock_blocker_reproof,
)


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, sort_keys=True).encode("utf-8")
        )
        self.headers = {"Date": "Fri, 21 Aug 2026 12:00:00 GMT"}
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
        ical_status: int = 200,
        html_status: int = 200,
        html_valid: bool = True,
        bea_error_code: str | None = None,
        ical_exception: requests.RequestException | None = None,
    ) -> None:
        self.ical_status = ical_status
        self.html_status = html_status
        self.html_valid = html_valid
        self.bea_error_code = bea_error_code
        self.ical_exception = ical_exception
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if url.endswith("bls.ics"):
            if self.ical_exception is not None:
                raise self.ical_exception
            return FakeResponse(
                self.ical_status,
                b"BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
                b"DTSTART:20260821T123000Z\r\n"
                b"SUMMARY:private release\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            )
        if url.endswith("/schedule/2026/08_sched_list.htm"):
            body = (
                b"<html><h1>August 2026</h1><table>"
                b"<tr><td>Tuesday, August 04, 2026</td><td>10:00 AM</td>"
                b"<td>private release one</td></tr>"
                b"<tr><td>Friday, August 21, 2026</td><td>08:30 AM</td>"
                b"<td>private release two</td></tr></table>"
                b"<p>NOTE: All times on calendar are Eastern Time.</p></html>"
                if self.html_valid
                else b"<html><h1>Schedule unavailable</h1></html>"
            )
            return FakeResponse(self.html_status, body)
        if url == "https://www.bea.gov/news/schedule/":
            return FakeResponse(
                200,
                b"<html><h1>Release Schedule</h1><div>August 26 8:30 AM</div></html>",
            )
        if url == "https://apps.bea.gov/api/data":
            payload = (
                {
                    "BEAAPI": {
                        "Results": {
                            "Error": {"APIErrorCode": self.bea_error_code}
                        }
                    }
                }
                if self.bea_error_code is not None
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
                }
            )
            return FakeResponse(200, payload)
        raise AssertionError(f"unexpected request: {method} {url}")


def _environment() -> dict[str, str]:
    return {"BEA_API_KEY": "private-bea-key"}


def _collect(session: FakeSession, *, activated: bool = True) -> dict[str, Any]:
    return collect_macro_event_clock_blocker_reproof(
        session=session,
        environment=_environment(),
        retrieved_at="2026-08-21T12:00:00Z",
        bea_activation_confirmed=activated,
    )


def main() -> int:
    gated_session = FakeSession()
    gated = _collect(gated_session, activated=False)
    assert gated["status"] == "BEA_KEY_ACTIVATION_REQUIRED"
    assert gated["requestCounts"] == {
        key: 0 for key in BLOCKER_ONLY_REQUEST_BUDGETS
    }
    assert gated_session.requests == []

    primary_session = FakeSession()
    primary = _collect(primary_session)
    assert primary["schemaVersion"] == BLOCKER_ONLY_SCHEMA_VERSION
    assert primary["status"] == BLOCKER_ONLY_PASS_STATUS
    assert primary["mode"] == "SHADOW_ONLY_BLOCKER_REPROOF"
    assert primary["preservedBaselineRunId"] == "32541234706"
    assert primary["requestCounts"] == {
        "federalReserveCalendar": 0,
        "fredMetadata": 0,
        "fredData": 0,
        "sec": 0,
        "finra": 0,
        "toss": 0,
        "blsData": 0,
        "blsIcal": 1,
        "blsHtmlFallback": 0,
        "beaMetadata": 1,
        "beaData": 1,
    }
    assert primary["requestBudgetCompliant"] is True
    assert primary["unknownOrUnclassifiedRows"] == 0
    assert primary["rawResponseStored"] is False
    assert primary["canonicalSourceChanged"] is False
    assert primary["policyImpact"] == "NONE_REPORT_ONLY"
    assert primary["sources"][0]["status"] == "BLS_CALENDAR_ICAL_PASS"
    assert all("api.bls.gov" not in url for _, url, _ in primary_session.requests)
    assert len(primary_session.requests) == 3

    fallback_session = FakeSession(ical_status=403)
    fallback = _collect(fallback_session)
    bls = fallback["sources"][0]
    assert fallback["status"] == BLOCKER_ONLY_PASS_STATUS
    assert bls["status"] == "BLS_CALENDAR_HTML_FALLBACK_PASS"
    assert bls["eventRows"] == 2
    assert bls["parseablePublicationRows"] == 2
    assert bls["explicitEasternTimeRows"] == 2
    assert bls["publicationDateMin"] == "2026-08-04T10:00:00-04:00"
    assert bls["publicationDateMax"] == "2026-08-21T08:30:00-04:00"
    assert len(bls["responseSha256"]) == 64
    assert len(bls["shapeKeySetSha256"]) == 64
    assert fallback["requestCounts"]["blsHtmlFallback"] == 1
    fallback_urls = [url for _, url, _ in fallback_session.requests]
    assert fallback_urls[1].endswith("/schedule/2026/08_sched_list.htm")

    mac_session = FakeSession()
    mac = collect_bls_calendar_mac_capability(
        session=mac_session,
        retrieved_at="2026-08-21T12:00:00Z",
    )
    mac_counts = {key: 0 for key in BLS_CALENDAR_MAC_REQUEST_BUDGETS}
    mac_counts["blsIcal"] = 1
    assert mac["schemaVersion"] == BLS_CALENDAR_MAC_SCHEMA_VERSION
    assert mac["status"] == "BLS_CALENDAR_ICAL_PASS"
    assert mac["mode"] == "SHADOW_ONLY_MAC_CALENDAR_ONE_SHOT"
    assert mac["executionTopology"] == "MAC_SIDE_BOUNDED_ONE_SHOT"
    assert mac["topologyVerdict"] == "BLS_CALENDAR_MAC_TOPOLOGY_READY"
    assert mac["requestCounts"] == mac_counts
    assert mac["externalRequestCount"] == 1
    assert mac["requestBudgetCompliant"] is True
    assert mac["marketTimezone"] == "America/New_York"
    assert mac["registrationKeyUsed"] is False
    assert mac["rawResponseStored"] is False
    assert mac["canonicalSourceChanged"] is False
    assert mac["policyImpact"] == "NONE_REPORT_ONLY"
    assert mac["unknownOrUnclassifiedRows"] == 0
    assert len(mac["source"]["responseSha256"]) == 64
    assert len(mac["source"]["shapeKeySetSha256"]) == 64
    assert len(mac_session.requests) == 1
    assert mac_session.requests[0][1].endswith("bls.ics")
    assert "private release" not in json.dumps(mac, sort_keys=True)

    mac_fallback_session = FakeSession(ical_status=403)
    mac_fallback = collect_bls_calendar_mac_capability(
        session=mac_fallback_session,
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert mac_fallback["status"] == "BLS_CALENDAR_HTML_FALLBACK_PASS"
    assert mac_fallback["requestCounts"]["blsIcal"] == 1
    assert mac_fallback["requestCounts"]["blsHtmlFallback"] == 1
    assert len(mac_fallback_session.requests) == 2

    for blocked_session in (
        FakeSession(ical_status=429),
        FakeSession(ical_status=500),
        FakeSession(ical_exception=requests.Timeout("private")),
    ):
        blocked_mac = collect_bls_calendar_mac_capability(
            session=blocked_session,
            retrieved_at="2026-08-21T12:00:00Z",
        )
        assert blocked_mac["requestCounts"]["blsHtmlFallback"] == 0
        assert len(blocked_session.requests) == 1

    rendered = json.dumps(fallback, sort_keys=True)
    for forbidden in (
        "private-bea-key",
        "private release one",
        "private release two",
        "PRIVATE_DATASET",
        "<html>",
    ):
        assert forbidden not in rendered

    for status in (429, 500):
        failed_session = FakeSession(ical_status=status)
        failed = _collect(failed_session)
        assert failed["requestCounts"]["blsHtmlFallback"] == 0
        assert len(failed_session.requests) == 3

    timeout_session = FakeSession(ical_exception=requests.Timeout("private"))
    timeout = _collect(timeout_session)
    assert timeout["requestCounts"]["blsHtmlFallback"] == 0
    assert len(timeout_session.requests) == 3

    invalid_html = _collect(FakeSession(ical_status=403, html_valid=False))
    assert invalid_html["sources"][0]["status"] == "BLS_CALENDAR_CONTRACT_INVALID"

    documented = _collect(FakeSession(bea_error_code="1"))
    assert documented["sources"][1]["primaryBlocker"] == "BEA_API_USER_ID_REJECTED"
    undocumented = _collect(FakeSession(bea_error_code="private"))
    assert undocumented["sources"][1]["primaryBlocker"] == "BEA_API_ERROR_SUBTYPE_UNVERIFIED"

    repeated = _collect(FakeSession(ical_status=403))
    assert repeated == fallback

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        session = FakeSession(ical_status=403)
        result = run_macro_event_clock_blocker_reproof(
            session=session,
            environment=_environment(),
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-21T12:00:00Z",
            approval=BLOCKER_ONLY_APPROVAL,
            bea_activation_confirmed=True,
        )
        assert result["status"] == BLOCKER_ONLY_PASS_STATUS
        initial_request_count = len(session.requests)
        try:
            run_macro_event_clock_blocker_reproof(
                session=session,
                environment=_environment(),
                output_path=root / "result.json",
                sentinel_path=root / "sentinel.json",
                retrieved_at="2026-08-21T12:01:00Z",
                approval=BLOCKER_ONLY_APPROVAL,
                bea_activation_confirmed=True,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate blocker reproof must fail before network")
        assert len(session.requests) == initial_request_count

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        session = FakeSession(ical_status=403)
        result = run_bls_calendar_mac_capability_probe(
            session=session,
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-21T12:00:00Z",
            approval=BLS_CALENDAR_MAC_APPROVAL,
        )
        assert result["status"] == "BLS_CALENDAR_HTML_FALLBACK_PASS"
        initial_request_count = len(session.requests)
        try:
            run_bls_calendar_mac_capability_probe(
                session=session,
                output_path=root / "result.json",
                sentinel_path=root / "sentinel.json",
                retrieved_at="2026-08-21T12:01:00Z",
                approval=BLS_CALENDAR_MAC_APPROVAL,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate Mac probe must fail before network")
        assert len(session.requests) == initial_request_count

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sentinel = root / "sentinel.json"
        sentinel.write_text("{}\n", encoding="utf-8")
        session = FakeSession()
        try:
            run_bls_calendar_mac_capability_probe(
                session=session,
                output_path=root / "result.json",
                sentinel_path=sentinel,
                retrieved_at="2026-08-21T12:00:00Z",
                approval=BLS_CALENDAR_MAC_APPROVAL,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("existing Mac sentinel must block before network")
        assert session.requests == []

    workflow = (
        ROOT / ".github/workflows/macro-event-clock-blocker-reproof.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert BLOCKER_ONLY_APPROVAL in workflow
    assert "BEA_ACTIVATION_CONFIRMED" in workflow
    assert 'test "$BEA_ACTIVATION_CONFIRMED" = "true"' in workflow
    assert "secrets.BEA_API_KEY" in workflow
    assert "secrets.FRED_API_KEY" not in workflow
    assert "schedule:" not in workflow
    assert "pull_request:" not in workflow

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "BLS_CALENDAR_MAC_TOPOLOGY_READY" in readme
    assert "--bls-calendar-mac-only" in readme
    assert BLS_CALENDAR_MAC_APPROVAL in readme
    assert "No recurring `launchd` job" in readme

    print(
        "[MACRO_BLOCKER_REPROOF] PASS "
        "fallback=bounded activationGate=fail_closed externalRequests=0"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
