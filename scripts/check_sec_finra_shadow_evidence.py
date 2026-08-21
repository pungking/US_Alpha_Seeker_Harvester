from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sec_finra_shadow_evidence import (
    build_sec_finra_shadow_not_run_result,
    collect_sec_finra_shadow_evidence,
    sec_finra_shadow_runtime_decision,
)
from run_sec_finra_shadow_reproof import run_sec_finra_shadow_reproof


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
        }
        self.is_redirect = False
        self.is_permanent_redirect = False

    def json(self) -> Any:
        if isinstance(self._payload, bytes):
            raise ValueError("not json")
        return self._payload


def _atom(cik: str, accession: str) -> bytes:
    digits = accession.replace("-", "")
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry><link href="'
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{digits}/"
        f'{accession}-index.htm"/></entry></feed>'
    ).encode("utf-8")


def _submissions(accession: str, form: str) -> dict[str, Any]:
    return {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": [form],
                "filingDate": ["2026-08-21"],
                "acceptanceDateTime": ["20260821115500"],
            }
        }
    }


def _raw_filing(accession: str, form: str, root: str) -> bytes:
    return f"""<SEC-DOCUMENT>
ACCESSION NUMBER: {accession}
CONFORMED SUBMISSION TYPE: {form}
<DOCUMENT><TYPE>{form}
<FILENAME>evidence.xml
<TEXT><XML><?xml version=\"1.0\"?><{root}><issuerCik>0000000001</issuerCik></{root}></XML></TEXT>
</DOCUMENT></SEC-DOCUMENT>""".encode("utf-8")


class FakeSession:
    def __init__(self, *, oauth_status: int = 200) -> None:
        self.oauth_status = oauth_status
        self.requests: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url))
        if "browse-edgar" in url:
            form_filter = parse_qs(urlparse(url).query).get("type", [""])[0]
            if form_filter == "4":
                return FakeResponse(
                    200,
                    _atom("123", "0000000123-26-000001"),
                    content_type="application/atom+xml",
                )
            if form_filter == "SC 13":
                return FakeResponse(
                    200,
                    b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
                    content_type="application/atom+xml",
                )
            return FakeResponse(
                200,
                _atom("456", "0000000456-26-000002"),
                content_type="application/atom+xml",
            )
        if "submissions/CIK0000000123" in url:
            return FakeResponse(200, _submissions("0000000123-26-000001", "4"))
        if "submissions/CIK0000000456" in url:
            return FakeResponse(200, _submissions("0000000456-26-000002", "13F-HR"))
        if url.endswith("0000000123-26-000001.txt"):
            return FakeResponse(
                200,
                _raw_filing("0000000123-26-000001", "4", "ownershipDocument"),
                content_type="text/plain",
            )
        if url.endswith("0000000456-26-000002.txt"):
            return FakeResponse(
                200,
                _raw_filing("0000000456-26-000002", "13F-HR", "edgarSubmission"),
                content_type="text/plain",
            )
        if "oauth2/access_token" in url:
            return FakeResponse(
                self.oauth_status,
                {"access_token": "private-token", "token_type": "Bearer"}
                if self.oauth_status == 200
                else {"error": "invalid_client"},
            )
        if "/metadata/" in url:
            return FakeResponse(200, {"datasetName": "consolidatedShortInterest"})
        if url.endswith("/consolidatedShortInterest"):
            return FakeResponse(
                200,
                [{
                    "settlementDate": "2026-08-14",
                    "currentShortPositionQuantity": 1,
                    "symbolCode": "PRIVATE1",
                }],
            )
        if url.endswith("/regShoDaily"):
            return FakeResponse(
                200,
                [{
                    "tradeReportDate": "2026-08-20",
                    "shortParQuantity": 1,
                    "totalParQuantity": 2,
                    "securitiesInformationProcessorSymbolIdentifier": "PRIVATE2",
                }],
            )
        if url.endswith("/thresholdList"):
            return FakeResponse(
                200,
                [{
                    "tradeDate": "2026-08-20",
                    "regShoThresholdFlag": "Y",
                    "thresholdListFlag": "Y",
                    "issueSymbolIdentifier": "PRIVATE3",
                }],
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def main() -> int:
    assert sec_finra_shadow_runtime_decision({}) == (
        False,
        "shadow_provider_disabled",
    )
    enabled, reason = sec_finra_shadow_runtime_decision(
        {
            "SEC_FINRA_SHADOW_PROVIDER_ENABLED": "true",
            "SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test",
            "FINRA_CLIENT_ID": "client-id",
            "FINRA_CLIENT_SECRET": "client-secret",
        }
    )
    assert enabled is True and reason == "server_side_shadow_enabled"

    disabled = build_sec_finra_shadow_not_run_result("shadow_provider_disabled")
    assert disabled["externalRequestCount"] == 0
    assert disabled["canonicalSourceChanged"] is False
    assert disabled["policyImpact"] == "NONE_REPORT_ONLY"

    session = FakeSession()
    result = collect_sec_finra_shadow_evidence(
        session=session,
        sec_user_agent="US Alpha Seeker contact@invalid.test",
        finra_client_id="client-id",
        finra_client_secret="client-secret",
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert result["schemaVersion"] == "sec-finra-shadow-evidence-v1"
    assert result["status"] == "SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"
    assert result["requestCounts"] == {
        "secDiscovery": 3,
        "secSubmissions": 2,
        "secRawFiling": 2,
        "finraOauth": 1,
        "finraMetadata": 1,
        "finraData": 3,
    }
    assert result["externalRequestCount"] == 12
    assert [row["status"] for row in result["secFamilies"]] == [
        "SOURCE_OBSERVATION_PASS",
        "NO_CURRENT_FILING_DISCOVERED",
        "SOURCE_OBSERVATION_PASS",
    ]
    assert result["finra"]["status"] == "SOURCE_OBSERVATION_PASS"
    assert result["exchangeListedRegShoStatus"] == "EXCLUDED_SOURCE_MATRIX_INCOMPLETE"
    assert result["analysisEligible"] is False
    assert result["canonicalSourceChanged"] is False
    assert result["policyImpact"] == "NONE_REPORT_ONLY"
    assert result["stage4To7Impact"] == "NONE"
    assert result["rawResponseStored"] is False
    assert result["unknownOrUnclassifiedRows"] == 0
    assert len(result["evidenceSha256"]) == 64
    assert result["evidenceHashBasis"] == "CANONICAL_JSON_WITHOUT_EVIDENCE_HASH"
    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (
        "PRIVATE1",
        "PRIVATE2",
        "PRIVATE3",
        "private-token",
        "client-id",
        "client-secret",
        "contact@invalid.test",
    ):
        assert forbidden not in rendered

    blocked = collect_sec_finra_shadow_evidence(
        session=FakeSession(oauth_status=403),
        sec_user_agent="US Alpha Seeker contact@invalid.test",
        finra_client_id="client-id",
        finra_client_secret="client-secret",
        retrieved_at="2026-08-21T12:00:00Z",
    )
    assert blocked["status"] == "SEC_FINRA_SHADOW_PARTIAL"
    assert blocked["finra"]["status"] == "AUTH_OR_ENTITLEMENT_BLOCKED"
    assert blocked["analysisContinued"] is True
    assert blocked["unknownOrUnclassifiedRows"] == 0

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reproof_session = FakeSession()
        reproof = run_sec_finra_shadow_reproof(
            session=reproof_session,
            environment={
                "SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test",
                "FINRA_CLIENT_ID": "client-id",
                "FINRA_CLIENT_SECRET": "client-secret",
            },
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-21T12:00:00Z",
        )
        assert reproof["status"] == "SEC_FINRA_SHADOW_PASS_APPROVED_SCOPE"
        assert reproof["externalRequestCount"] == 12
        assert reproof["reproofMode"] == "BOUNDED_MANUAL_READ_ONLY"
        assert reproof["canonicalSourceChanged"] is False
        assert reproof["policyImpact"] == "NONE_REPORT_ONLY"
        assert reproof["rawResponseStored"] is False
        assert (root / "result.json").exists()
        sentinel = json.loads((root / "sentinel.json").read_text(encoding="utf-8"))
        assert sentinel["status"] == "COMPLETE"
        assert sentinel["requestCounts"] == reproof["requestCounts"]
        assert len(sentinel["resultSha256"]) == 64

        request_count = len(reproof_session.requests)
        try:
            run_sec_finra_shadow_reproof(
                session=reproof_session,
                environment={
                    "SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test",
                    "FINRA_CLIENT_ID": "client-id",
                    "FINRA_CLIENT_SECRET": "client-secret",
                },
                output_path=root / "result.json",
                sentinel_path=root / "sentinel.json",
                retrieved_at="2026-08-21T12:01:00Z",
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate reproof must fail before network")
        assert len(reproof_session.requests) == request_count

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        blocked_session = FakeSession()
        blocked_reproof = run_sec_finra_shadow_reproof(
            session=blocked_session,
            environment={},
            output_path=root / "result.json",
            sentinel_path=root / "sentinel.json",
            retrieved_at="2026-08-21T12:02:00Z",
        )
        assert blocked_reproof["status"] == "SEC_FINRA_SHADOW_REPROOF_CONFIG_BLOCKED"
        assert blocked_reproof["externalRequestCount"] == 0
        assert blocked_session.requests == []

    harvester_source = (ROOT / "harvester.py").read_text(encoding="utf-8")
    workflow_source = (ROOT / ".github/workflows/main.yml").read_text(
        encoding="utf-8"
    )
    reproof_workflow = (
        ROOT / ".github/workflows/sec-finra-shadow-reproof.yml"
    ).read_text(encoding="utf-8")
    for required in (
        "SEC_FINRA_SHADOW_EVIDENCE_FILENAME",
        "ensure_sec_finra_shadow_evidence",
        '"secFinraShadow": sec_finra_shadow',
        '"SEC_FINRA_SHADOW_PROVIDER_ENABLED", False',
    ):
        assert required in harvester_source
    for required in (
        "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}",
        "FINRA_CLIENT_ID: ${{ secrets.FINRA_CLIENT_ID }}",
        "FINRA_CLIENT_SECRET: ${{ secrets.FINRA_CLIENT_SECRET }}",
        "SEC_FINRA_SHADOW_PROVIDER_ENABLED: 'false'",
        "state/sec-finra-shadow-evidence.json",
    ):
        assert required in workflow_source
    for required in (
        "workflow_dispatch:",
        "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}",
        "FINRA_CLIENT_ID: ${{ secrets.FINRA_CLIENT_ID }}",
        "FINRA_CLIENT_SECRET: ${{ secrets.FINRA_CLIENT_SECRET }}",
        "python scripts/run_sec_finra_shadow_reproof.py",
        "SEC_FINRA_SHADOW_PROVIDER_ENABLED: 'false'",
    ):
        assert required in reproof_workflow
    job_prelude, steps = reproof_workflow.split("    steps:", 1)
    probe_step = steps.split(
        "      - name: Run bounded read-only reproof", 1
    )[1].split("      - name:", 1)[0]
    for secret in ("SEC_USER_AGENT", "FINRA_CLIENT_ID", "FINRA_CLIENT_SECRET"):
        assert f"secrets.{secret}" not in job_prelude
        assert f"secrets.{secret}" in probe_step

    print(
        "[SEC_FINRA_SHADOW_EVIDENCE] PASS "
        "approvedScopeRequests=12 rawStored=false policyImpact=NONE_REPORT_ONLY"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
