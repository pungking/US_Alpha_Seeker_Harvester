from __future__ import annotations

import io
import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from run_financial_publication_lineage_bulk_probe import (
    APPROVAL,
    PASS_STATUS,
    build_ticker_cik_index,
    match_exact_financial_lineage,
    reserve_bulk_probe,
    run_bulk_probe,
)
from official_shadow_runtime import canonical_sha256


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/sec-financial-publication-lineage-bulk-probe.yml"
CI_WORKFLOW = ROOT / ".github/workflows/telegram-routing-ci.yml"


class Response:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self.content = body
        self.headers = {"date": "Fri, 28 Aug 2026 01:00:00 GMT"}
        self.is_redirect = False
        self.is_permanent_redirect = False

    def iter_content(self, chunk_size: int) -> list[bytes]:
        return [
            self.content[index : index + chunk_size]
            for index in range(0, len(self.content), chunk_size)
        ]

    def close(self) -> None:
        return None


class Session:
    def __init__(self, responses: list[Response], before_request: Any = None) -> None:
        self.responses = list(responses)
        self.before_request = before_request
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        if self.before_request is not None:
            self.before_request()
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


def _zip_bytes(files: dict[str, object]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in files.items():
            archive.writestr(name, json.dumps(payload, sort_keys=True))
    return output.getvalue()


def _ticker_map() -> dict[str, object]:
    return {"0": {"cik_str": 123, "ticker": "SYNTH", "title": "Synthetic"}}


def _companyfacts() -> dict[str, object]:
    return {
        "cik": 123,
        "entityName": "Synthetic",
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            {
                                "start": "2026-04-01",
                                "end": "2026-06-30",
                                "val": 100,
                                "accn": "0000000123-26-000001",
                                "fy": 2026,
                                "fp": "Q2",
                                "form": "10-Q",
                                "filed": "2026-08-01",
                            }
                        ]
                    }
                }
            }
        },
    }


def _submissions(*, acceptance: str = "20260801160000") -> dict[str, object]:
    return {
        "cik": "0000000123",
        "filings": {
            "recent": {
                "accessionNumber": ["0000000123-26-000001"],
                "form": ["10-Q"],
                "filingDate": ["2026-08-01"],
                "reportDate": ["2026-06-30"],
                "acceptanceDateTime": [acceptance],
            }
        },
    }


def _responses(*, acceptance: str = "20260801160000") -> list[Response]:
    return [
        Response(200, json.dumps(_ticker_map()).encode()),
        Response(200, _zip_bytes({"CIK0000000123.json": _companyfacts()})),
        Response(200, _zip_bytes({"CIK0000000123.json": _submissions(acceptance=acceptance)})),
    ]


def _assert_redacted(payload: object) -> None:
    text = json.dumps(payload, sort_keys=True)
    for forbidden in ("SYNTH", "0000000123", "0000000123-26-000001"):
        assert forbidden not in text


def _test_exact_lineage() -> None:
    index, shape = build_ticker_cik_index(_ticker_map())
    assert shape == {
        "tickerRows": 1,
        "validTickerCikRows": 1,
        "invalidTickerCikRows": 0,
        "ambiguousTickerRows": 0,
        "uniqueCikRows": 1,
    }
    identity = {
        "effectiveSymbol": "SYNTH",
        "identifierLineageStatus": "EFFECTIVE_ALIAS_RESOLVED",
    }
    history = {
        "value": 100,
        "fiscalPeriod": "2026-06-30",
    }
    verified = match_exact_financial_lineage(
        identity=identity,
        history_record=history,
        ticker_cik_index=index,
        companyfacts=_companyfacts(),
        submissions=_submissions(),
        retrieved_at="2026-08-02T00:00:00Z",
    )
    assert verified["status"] == "FINANCIAL_LINEAGE_VERIFIED"
    assert verified["financialSource"] == "YFINANCE_HISTORY_SEC_EDGAR_EXACT_LINEAGE"
    assert verified["fiscalPeriod"] == "2026-06-30"
    assert verified["financialPublishedAt"] == "2026-08-01T20:00:00Z"
    assert verified["financialRetrievedAt"] == "2026-08-02T00:00:00Z"
    assert len(str(verified["sourceRecordSha256"])) == 64
    _assert_redacted(verified)

    cases = [
        (
            {"effectiveSymbol": "SYNTH"},
            history,
            _companyfacts(),
            _submissions(),
            "IDENTIFIER_LINEAGE_INCOMPLETE",
        ),
        (identity, {"value": 100}, _companyfacts(), _submissions(), "FISCAL_PERIOD_MISSING"),
        (
            identity,
            {"value": 101, "fiscalPeriod": "2026-06-30"},
            _companyfacts(),
            _submissions(),
            "EXACT_FACT_NOT_FOUND",
        ),
        (
            identity,
            history,
            {
                **_companyfacts(),
                "facts": {
                    "us-gaap": {
                        "NetIncomeLoss": {
                            "units": {
                                "USD": (
                                    _companyfacts()["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
                                    * 2
                                )
                            }
                        }
                    }
                },
            },
            _submissions(),
            "FACT_ACCESSION_AMBIGUOUS",
        ),
        (
            identity,
            history,
            _companyfacts(),
            {"cik": "0000000123", "filings": {"recent": {}}},
            "SUBMISSIONS_ACCESSION_MISSING",
        ),
        (identity, history, _companyfacts(), _submissions(acceptance=""), "ACCEPTANCE_TIMESTAMP_MISSING"),
        (
            identity,
            history,
            _companyfacts(),
            _submissions(acceptance="20260803160000"),
            "PUBLICATION_AFTER_RETRIEVAL_REJECTED",
        ),
    ]
    for case_identity, case_history, facts, submissions, expected in cases:
        result = match_exact_financial_lineage(
            identity=case_identity,
            history_record=case_history,
            ticker_cik_index=index,
            companyfacts=facts,
            submissions=submissions,
            retrieved_at="2026-08-02T00:00:00Z",
        )
        assert result["status"] == expected, (expected, result)
        _assert_redacted(result)


def _test_runner() -> None:
    first_result: dict[str, Any]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "safe.json"
        sentinel_dir = root / "sentinel"
        raw_dir = root / "raw"
        sentinel_path = reserve_bulk_probe(
            sentinel_dir=sentinel_dir,
            reserved_at="2026-08-02T00:00:00Z",
        )
        session = Session(
            _responses(),
            before_request=lambda: (
                sentinel_path.exists() or (_ for _ in ()).throw(AssertionError("sentinel missing"))
            ),
        )
        result = run_bulk_probe(
            session=session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            output_path=output,
            sentinel_dir=sentinel_dir,
            raw_temp_dir=raw_dir,
            retrieved_at="2026-08-02T00:00:00Z",
            approval=APPROVAL,
        )
        assert result["status"] == PASS_STATUS
        assert result["requestCounts"] == {
            "secCompanyTickerMap": 1,
            "secCompanyfactsBulk": 1,
            "secSubmissionsBulk": 1,
        }
        assert result["externalRequestCount"] == 3
        assert result["requestBudgetCompliant"] is True
        assert len(session.requests) == 3
        assert all(call["kwargs"].get("allow_redirects") is False for call in session.requests)
        assert all("params" not in call["kwargs"] for call in session.requests)
        assert result["temporaryArchivesDeleted"] is True
        assert result["rawResponseStored"] is False
        assert result["unknownOrUnclassifiedRows"] == 0
        assert not raw_dir.exists()
        assert output.exists()
        terminal = json.loads(sentinel_path.read_text())
        assert terminal["status"] == "COMPLETE"
        assert terminal["requestCounts"] == result["requestCounts"]
        hashed = dict(result)
        evidence_sha256 = hashed.pop("evidenceSha256")
        assert canonical_sha256(hashed) == evidence_sha256
        assert terminal["artifactSha256"] == evidence_sha256
        assert terminal["artifactHashBasis"] == (
            "PRE_PERSISTENCE_COLLECTION_EVIDENCE"
        )
        _assert_redacted(result)
        _assert_redacted(terminal)
        first_result = result

        duplicate = Session([])
        try:
            run_bulk_probe(
                session=duplicate,
                environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
                output_path=output,
                sentinel_dir=sentinel_dir,
                raw_temp_dir=raw_dir,
                retrieved_at="2026-08-02T00:00:00Z",
                approval=APPROVAL,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate probe was accepted")
        assert duplicate.requests == []

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        session = Session([])
        try:
            reserve_bulk_probe(root / "sentinel", "2026-08-02T00:00:00Z")
            run_bulk_probe(
                session=session,
                environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
                output_path=root / "safe.json",
                sentinel_dir=root / "sentinel",
                raw_temp_dir=root / "raw",
                retrieved_at="2026-08-02T00:00:00Z",
                approval="not approved",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid approval was accepted")
        assert session.requests == []

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reserve_bulk_probe(root / "sentinel", "2026-08-02T00:00:00Z")
        rerun = run_bulk_probe(
            session=Session(_responses()),
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            output_path=root / "safe.json",
            sentinel_dir=root / "sentinel",
            raw_temp_dir=root / "raw",
            retrieved_at="2026-08-02T00:00:00Z",
            approval=APPROVAL,
        )
        assert rerun == first_result, (first_result, rerun)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sentinel = reserve_bulk_probe(
            root / "sentinel", "2026-08-02T00:00:00Z"
        )
        failed = run_bulk_probe(
            session=Session([Response(403, b"safe synthetic failure")]),
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            output_path=root / "safe.json",
            sentinel_dir=root / "sentinel",
            raw_temp_dir=root / "raw",
            retrieved_at="2026-08-02T00:00:00Z",
            approval=APPROVAL,
        )
        assert failed["status"] == "SEC_COMPANY_TICKER_MAP_HTTP_FAILURE"
        assert failed["externalRequestCount"] == 1
        assert failed["temporaryArchivesDeleted"] is True
        assert json.loads(sentinel.read_text())["status"] == "FAILED"
        _assert_redacted(failed)

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        reserve_bulk_probe(root / "sentinel", "2026-08-02T00:00:00Z")
        session = Session([])
        try:
            run_bulk_probe(
                session=session,
                environment={},
                output_path=root / "safe.json",
                sentinel_dir=root / "sentinel",
                raw_temp_dir=root / "raw",
                retrieved_at="2026-08-02T00:00:00Z",
                approval=APPROVAL,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("missing SEC contact was accepted")
        assert session.requests == []


def _test_workflow() -> None:
    workflow = WORKFLOW.read_text()
    assert "workflow_dispatch:" in workflow
    for forbidden in ("schedule:", "pull_request:", "push:"):
        assert forbidden not in workflow
    assert APPROVAL in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "lookup-only: true" in workflow
    assert "restore-keys:" not in workflow
    assert workflow.index("actions/cache/save@v4") < workflow.index("Run bounded SEC financial lineage probe")
    assert "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}" in workflow
    upload = workflow.split("Upload safe aggregate evidence", 1)[1]
    assert "sec-financial-lineage-private" not in upload
    assert "companyfacts.zip" not in upload and "submissions.zip" not in upload
    assert "actions/upload-artifact@v5" in workflow
    assert "check_financial_publication_lineage_bulk_probe.py" in CI_WORKFLOW.read_text()


def main() -> int:
    _test_exact_lineage()
    _test_runner()
    _test_workflow()
    print(
        "[FINANCIAL_PUBLICATION_LINEAGE_BULK_PROBE] PASS "
        "requests=3 retry=0 pagination=0 rawPersistent=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
