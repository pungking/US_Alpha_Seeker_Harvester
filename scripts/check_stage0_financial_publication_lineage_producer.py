from __future__ import annotations

import copy
import io
import json
import tempfile
import zipfile
from pathlib import Path
import string

from official_shadow_runtime import canonical_sha256
from stage0_financial_publication_lineage_producer import (
    PRODUCER_APPROVAL,
    VERIFIED_STATUSES,
    build_financial_input_rows,
    build_lineage_artifact,
    classify_financial_lineage,
    reserve_producer,
    run_bounded_producer,
    safe_aggregate,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/stage0-sec-financial-lineage-producer.yml"
CI_WORKFLOW = ROOT / ".github/workflows/telegram-routing-ci.yml"
SHA = "a" * 64


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
    def __init__(self, responses: list[Response]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> Response:
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


def _identity(*, alias: bool = False) -> dict[str, object]:
    return {
        "effectiveSymbol": "RENAMED" if alias else "SYNTH",
        "sourceSymbol": "SYNTH",
        "identifierLineageStatus": (
            "IDENTIFIER_ALIAS_RESOLVED" if alias else "IDENTIFIER_LINEAGE_VERIFIED"
        ),
    }


def _record(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "identity": _identity(),
        "financialMetricBasis": "NET_INCOME",
        "sourceMetricLabel": "Net Income",
        "value": 100,
        "fiscalPeriod": "2026-06-30",
        "sourceDailyFile": "S_stocks_daily.json",
        "sourceDailyFileSha256": SHA,
        "sourceHistoryFile": "S_stocks_history.json",
        "sourceHistoryFileSha256": "b" * 64,
    }
    row.update(overrides)
    return row


def _fact(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "start": "2026-04-01",
        "end": "2026-06-30",
        "val": 100,
        "accn": "0000000123-26-000001",
        "form": "10-Q",
    }
    row.update(overrides)
    return row


def _companyfacts(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "cik": 123,
        "facts": {
            "us-gaap": {
                "NetIncomeLoss": {"units": {"USD": list(rows or [_fact()])}},
                "NetIncomeLossAvailableToCommonStockholdersBasic": {
                    "units": {"USD": []}
                },
            }
        },
    }


def _submissions(
    *,
    accession: str = "0000000123-26-000001",
    form: str = "10-Q",
    acceptance: str = "20260801160000",
) -> dict[str, object]:
    return {
        "cik": "0000000123",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": [form],
                "filingDate": ["2026-08-01"],
                "reportDate": ["2026-06-30"],
                "acceptanceDateTime": [acceptance],
            }
        },
    }


def _submissions_many() -> dict[str, object]:
    return {
        "cik": "0000000123",
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000000123-26-000001",
                    "0000000123-26-000002",
                ],
                "form": ["10-Q", "10-Q/A"],
                "filingDate": ["2026-08-01", "2026-08-02"],
                "reportDate": ["2026-06-30", "2026-06-30"],
                "acceptanceDateTime": ["20260801160000", "20260801180000"],
            }
        },
    }


def _classify(
    *,
    record: dict[str, object] | None = None,
    facts: dict[str, object] | None = None,
    submissions: dict[str, object] | None = None,
) -> dict[str, object]:
    return classify_financial_lineage(
        record=record or _record(),
        ticker_cik_index={"SYNTH": ("0000000123",), "RENAMED": ("0000000123",)},
        companyfacts=facts or _companyfacts(_fact()),
        submissions=submissions or _submissions(),
        retrieved_at="2026-08-02T00:00:00Z",
        source_response_hashes={
            "secCompanyTickerMap": "c" * 64,
            "secCompanyfactsBulk": "d" * 64,
            "secSubmissionsBulk": "e" * 64,
        },
    )


def _assert_redacted(payload: object) -> None:
    text = json.dumps(payload, sort_keys=True)
    for forbidden in ("SYNTH", "RENAMED", "0000000123", "0000000123-26-000001"):
        assert forbidden not in text


def _test_input_rows() -> None:
    identity_map = {
        "SYNTH": {
            "symbol": "SYNTH",
            "sourceSymbol": "SYNTH",
            "analysisEligible": True,
        },
        "_meta": {"schemaVersion": "fixture"},
    }
    daily = {
        "fileName": "S_stocks_daily.json",
        "contentSha256": SHA,
        "payload": {
            "SYNTH": {
                "symbol": "SYNTH",
                "netIncome": 100,
                "netIncomeSource": "HISTORY",
                "netIncomeAsOf": "2026-06-30",
            }
        },
    }
    history = {
        "fileName": "S_stocks_history.json",
        "contentSha256": "b" * 64,
        "payload": {
            "SYNTH": {
                "financials": [
                    {
                        "date": "2026-06-30",
                        "_periodType": "QUARTERLY",
                        "Net Income": 100,
                    }
                ]
            }
        },
    }
    rows = build_financial_input_rows(
        identity_map=identity_map,
        identity_map_sha256="f" * 64,
        daily_files=[daily],
        history_files=[history],
    )
    assert len(rows) == 1
    assert rows[0]["identity"]["identifierLineageStatus"] == "IDENTIFIER_LINEAGE_VERIFIED"
    assert rows[0]["sourceMetricLabel"] == "Net Income"
    assert rows[0]["fiscalPeriod"] == "2026-06-30"

    alias_map = copy.deepcopy(identity_map)
    alias_map["SYNTH"]["symbol"] = "RENAMED"
    alias_rows = build_financial_input_rows(
        identity_map=alias_map,
        identity_map_sha256="f" * 64,
        daily_files=[daily],
        history_files=[history],
    )
    assert alias_rows[0]["identity"]["identifierLineageStatus"] == "IDENTIFIER_ALIAS_RESOLVED"
    assert alias_rows[0]["identity"]["effectiveSymbol"] == "RENAMED"

    info = copy.deepcopy(daily)
    info["payload"]["SYNTH"]["netIncomeSource"] = "INFO"
    info_rows = build_financial_input_rows(
        identity_map=identity_map,
        identity_map_sha256="f" * 64,
        daily_files=[info],
        history_files=[history],
    )
    assert info_rows[0]["inputStatus"] == "FINANCIAL_LINEAGE_NOT_APPLICABLE"


def _test_classification() -> None:
    original = _classify()
    assert original["classification"] == "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL"
    assert original["financialPublishedAt"] == "2026-08-01T20:00:00Z"
    assert original["fiscalPeriod"] == {
        "start": "2026-04-01",
        "end": "2026-06-30",
    }
    assert original["financialSourceRecordSha256"] == canonical_sha256(
        original["financialSourceRecordHashBasis"]
    )
    alias = _classify(record=_record(identity=_identity(alias=True)))
    assert alias["classification"] == "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL"

    amended_fact = _fact(form="10-Q/A", accn="0000000123-26-000002")
    amended = _classify(
        facts=_companyfacts(amended_fact),
        submissions=_submissions(
            accession="0000000123-26-000002", form="10-Q/A"
        ),
    )
    assert amended["classification"] == "FINANCIAL_LINEAGE_VERIFIED_AMENDMENT"

    duplicate = _classify(facts=_companyfacts(_fact(), copy.deepcopy(_fact())))
    assert duplicate["classification"] == (
        "FINANCIAL_LINEAGE_DUPLICATE_SAME_ACCESSION_COLLAPSED"
    )
    assert duplicate["collapsedDuplicateRows"] == 1

    ambiguous = _classify(
        facts=_companyfacts(
            _fact(),
            _fact(accn="0000000123-26-000002", form="10-Q/A"),
        ),
        submissions=_submissions_many(),
    )
    assert ambiguous["classification"] == (
        "FINANCIAL_LINEAGE_MULTIPLE_ACCESSIONS_AMBIGUOUS"
    )
    assert "financialPublishedAt" not in ambiguous
    assert [row["amendmentStatus"] for row in ambiguous["candidateLineage"]] == [
        "ORIGINAL",
        "AMENDMENT",
    ]
    assert all(row["financialPublishedAt"] for row in ambiguous["candidateLineage"])

    cases = [
        (
            _classify(submissions={"cik": "0000000123", "filings": {"recent": {}}}),
            "FINANCIAL_LINEAGE_SUBMISSION_MISSING",
        ),
        (
            _classify(submissions=_submissions(form="8-K")),
            "FINANCIAL_LINEAGE_FORM_MISMATCH",
        ),
        (
            _classify(submissions=_submissions(acceptance="20260803160000")),
            "FINANCIAL_LINEAGE_PUBLICATION_AFTER_RETRIEVAL_REJECTED",
        ),
        (
            _classify(record=_record(sourceMetricLabel="Unmapped Metric")),
            "FINANCIAL_LINEAGE_FACT_NOT_FOUND",
        ),
        (
            _classify(record=_record(identity={"effectiveSymbol": "SYNTH"})),
            "FINANCIAL_LINEAGE_IDENTITY_INVALID",
        ),
        (
            _classify(record=_record(inputStatus="FINANCIAL_LINEAGE_NOT_APPLICABLE")),
            "FINANCIAL_LINEAGE_NOT_APPLICABLE",
        ),
    ]
    for result, expected in cases:
        assert result["classification"] == expected, (expected, result)


def _test_artifact() -> None:
    records = [_record(), _record(inputStatus="FINANCIAL_LINEAGE_NOT_APPLICABLE")]
    kwargs = {
        "records": records,
        "ticker_cik_index": {"SYNTH": ("0000000123",)},
        "companyfacts_by_cik": {"0000000123": _companyfacts(_fact())},
        "submissions_by_cik": {"0000000123": _submissions()},
        "retrieved_at": "2026-08-02T00:00:00Z",
        "source_files": [
            {"fileName": "S_stocks_daily.json", "sourceKind": "DAILY", "contentSha256": SHA},
            {"fileName": "S_stocks_history.json", "sourceKind": "HISTORY", "contentSha256": "b" * 64},
        ],
        "identity_map_sha256": "f" * 64,
        "source_response_hashes": {
            "secCompanyTickerMap": "c" * 64,
            "secCompanyfactsBulk": "d" * 64,
            "secSubmissionsBulk": "e" * 64,
        },
        "request_counts": {
            "secCompanyTickerMap": 1,
            "secCompanyfactsBulk": 1,
            "secSubmissionsBulk": 1,
        },
    }
    first = build_lineage_artifact(**kwargs)
    second = build_lineage_artifact(**kwargs)
    assert first == second
    assert first["verifiedRows"] == 1
    assert first["classificationCounts"] == {
        "FINANCIAL_LINEAGE_NOT_APPLICABLE": 1,
        "FINANCIAL_LINEAGE_VERIFIED_ORIGINAL": 1,
    }
    assert first["unknownOrUnclassifiedRows"] == 0
    assert first["rawResponseStored"] is False
    assert first["requestBudgetExact"] is True
    assert first["Stage1To7PolicyChanged"] is False
    assert first["inputHash"] == second["inputHash"]
    assert first["outputHash"] == second["outputHash"]
    assert set(VERIFIED_STATUSES).issuperset(
        {"FINANCIAL_LINEAGE_VERIFIED_ORIGINAL"}
    )

    public = safe_aggregate(first)
    assert public["publicationLineageRows"] == 2
    assert public["privatePublicationLineageRowsStored"] is False
    assert public["privateArtifactSha256"] == canonical_sha256(first)
    _assert_redacted(public)


def _test_bounded_runner() -> None:
    identity_map = {
        "SYNTH": {
            "symbol": "SYNTH",
            "sourceSymbol": "SYNTH",
            "analysisEligible": True,
        }
    }
    payloads: dict[str, object] = {"identity": identity_map}
    daily_files: list[dict[str, str]] = []
    history_files: list[dict[str, str]] = []
    for letter in string.ascii_uppercase:
        daily_id = f"daily-{letter}"
        history_id = f"history-{letter}"
        daily_files.append({"id": daily_id, "name": f"{letter}_stocks_daily.json"})
        history_files.append({"id": history_id, "name": f"{letter}_stocks_history.json"})
        payloads[daily_id] = {}
        payloads[history_id] = {}
    payloads["daily-S"] = {
        "SYNTH": {
            "symbol": "SYNTH",
            "netIncome": 100,
            "netIncomeSource": "HISTORY",
            "netIncomeAsOf": "2026-06-30",
        }
    }
    payloads["history-S"] = {
        "SYNTH": {
            "financials": [
                {
                    "date": "2026-06-30",
                    "_periodType": "QUARTERLY",
                    "Net Income": 100,
                }
            ]
        }
    }
    ids = {
        ("US_Alpha_Seeker", None): "root",
        ("System_Identity_Maps", "root"): "system",
        ("Financial_Data_Daily", "system"): "daily-folder",
        ("Financial_Data_History_5Y", "system"): "history-folder",
        ("Ticker_ID_Mapping_Final.json", "system"): "identity",
    }
    uploads: list[tuple[str, dict[str, object], str]] = []
    responses = [
        Response(
            200,
            json.dumps({"0": {"cik_str": 123, "ticker": "SYNTH"}}).encode(),
        ),
        Response(200, _zip_bytes({"CIK0000000123.json": _companyfacts(_fact())})),
        Response(200, _zip_bytes({"CIK0000000123.json": _submissions()})),
    ]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sentinel = reserve_producer(root / "sentinel", "2026-08-02T00:00:00Z")
        session = Session(responses)
        result = run_bounded_producer(
            session=session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            safe_output_path=root / "safe.json",
            private_output_path=root / "private.json",
            sentinel_dir=root / "sentinel",
            raw_temp_dir=root / "raw",
            retrieved_at="2026-08-02T00:00:00Z",
            approval=PRODUCER_APPROVAL,
            find_file_id=lambda name, parent=None: ids.get((name, parent)),
            download_json=lambda file_id: copy.deepcopy(payloads[file_id]),
            list_files=lambda folder_id: (
                copy.deepcopy(daily_files)
                if folder_id == "daily-folder"
                else copy.deepcopy(history_files)
            ),
            upload_json=lambda name, payload, parent: uploads.append(
                (name, copy.deepcopy(payload), parent)
            ),
        )
        assert result["status"] == "STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_PASS"
        assert result["requestCounts"] == {
            "secCompanyTickerMap": 1,
            "secCompanyfactsBulk": 1,
            "secSubmissionsBulk": 1,
        }
        assert result["temporaryArchivesDeleted"] is True
        assert not (root / "raw").exists()
        assert (root / "private.json").exists()
        assert len(session.requests) == 3
        assert all(call["kwargs"].get("allow_redirects") is False for call in session.requests)
        assert len(uploads) == 2
        assert json.loads(sentinel.read_text())["status"] == "COMPLETE"
        _assert_redacted(result)

        duplicate = Session([])
        try:
            run_bounded_producer(
                session=duplicate,
                environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
                safe_output_path=root / "safe-duplicate.json",
                private_output_path=root / "private-duplicate.json",
                sentinel_dir=root / "sentinel",
                raw_temp_dir=root / "raw-duplicate",
                retrieved_at="2026-08-02T00:00:00Z",
                approval=PRODUCER_APPROVAL,
                find_file_id=lambda name, parent=None: ids.get((name, parent)),
                download_json=lambda file_id: copy.deepcopy(payloads[file_id]),
                list_files=lambda folder_id: [],
                upload_json=lambda name, payload, parent: None,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate producer run was accepted")
        assert duplicate.requests == []


def _test_workflow() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    for forbidden in ("schedule:", "pull_request:", "push:"):
        assert forbidden not in workflow
    assert PRODUCER_APPROVAL in workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in workflow
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in workflow
    assert "actions/cache/restore@v4" in workflow
    assert "actions/cache/save@v4" in workflow
    assert "lookup-only: true" in workflow
    assert "restore-keys:" not in workflow
    assert workflow.index("actions/cache/save@v4") < workflow.index(
        "Run bounded Stage0 SEC financial lineage producer"
    )
    assert "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}" in workflow
    assert "GDRIVE_REFRESH_TOKEN: ${{ secrets.GDRIVE_REFRESH_TOKEN }}" in workflow
    upload = workflow.split("Upload safe aggregate evidence", 1)[1]
    for forbidden in ("private.json", "companyfacts.zip", "submissions.zip"):
        assert forbidden not in upload
    assert "check_stage0_financial_publication_lineage_producer.py" in CI_WORKFLOW.read_text()


def main() -> int:
    _test_input_rows()
    _test_classification()
    _test_artifact()
    _test_bounded_runner()
    _test_workflow()
    print(
        "[STAGE0_FINANCIAL_PUBLICATION_LINEAGE_PRODUCER] PASS "
        "unknown=0 rawPersistent=false policyChanged=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
