from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
import string

from official_shadow_runtime import canonical_sha256
from stage0_financial_publication_lineage_producer import (
    PRODUCER_APPROVAL,
    PRODUCER_RECURRING_APPROVAL,
    VERIFIED_STATUSES,
    build_financial_input_rows,
    build_lineage_artifact,
    classify_financial_lineage,
    recurring_collection_key,
    reserve_producer,
    run_bounded_producer,
    safe_aggregate,
)


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/stage0-sec-financial-lineage-producer.yml"
RECOVERY_WORKFLOW = ROOT / ".github/workflows/stage0-sec-financial-lineage-current-window-recovery.yml"
MAIN_WORKFLOW = ROOT / ".github/workflows/main.yml"
CI_WORKFLOW = ROOT / ".github/workflows/telegram-routing-ci.yml"
SHA = "a" * 64
RECOVERY_APPROVAL = (
    "AUTHORIZE STAGE0 SEC FINANCIAL LINEAGE CURRENT-WINDOW RECOVERY ONE-SHOT"
)
RECOVERY_TRIGGER_MODE = "MANUAL_CURRENT_WINDOW_RECOVERY"


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


def _raw_json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _raw_sha256(payload: object) -> str:
    return hashlib.sha256(_raw_json_bytes(payload)).hexdigest()


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
    assert info_rows[0]["inputStatus"] == "READY_FOR_EXACT_SEC_LINEAGE"
    assert info_rows[0]["value"] == 100
    assert info_rows[0]["fiscalPeriod"] == "2026-06-30"
    assert info_rows[0]["sourceMetricLabel"] == "Net Income"
    assert canonical_sha256(info_rows) == canonical_sha256(copy.deepcopy(info_rows))

    info_without_history = build_financial_input_rows(
        identity_map=identity_map,
        identity_map_sha256="f" * 64,
        daily_files=[info],
        history_files=[{**history, "payload": {"SYNTH": {"financials": []}}}],
    )
    assert info_without_history[0]["inputStatus"] == "FINANCIAL_LINEAGE_NOT_APPLICABLE"

    info_with_history_evidence = copy.deepcopy(info)
    info_with_history_evidence["payload"]["SYNTH"].update(
        netIncome=999,
        netIncomeEvidenceValue=100,
        netIncomeEvidenceAsOf="2026-06-30",
        netIncomeEvidenceSource="HISTORY",
    )
    evidence_rows = build_financial_input_rows(
        identity_map=identity_map,
        identity_map_sha256="f" * 64,
        daily_files=[info_with_history_evidence],
        history_files=[history],
    )
    assert evidence_rows[0]["inputStatus"] == "READY_FOR_EXACT_SEC_LINEAGE"
    assert evidence_rows[0]["value"] == 100
    assert evidence_rows[0]["fiscalPeriod"] == "2026-06-30"
    assert evidence_rows[0]["sourceMetricLabel"] == "Net Income"


def _test_harvester_history_evidence_wiring() -> None:
    source = (ROOT / "harvester.py").read_text(encoding="utf-8")
    fields = (
        "netIncomeEvidenceValue",
        "netIncomeEvidenceAsOf",
        "netIncomeEvidenceSource",
    )
    fundamental_keys = source.split("RAW_FUNDAMENTAL_OPTIONAL_KEYS = [", 1)[1].split("]", 1)[0]
    for field in fields:
        assert f'"{field}"' in fundamental_keys
    assert '"netIncomeEvidenceValue": history_net_income' in source
    assert '"netIncomeEvidenceAsOf": history_net_income_asof' in source
    assert '"netIncomeEvidenceSource":' in source


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
            {
                "fileName": "S_stocks_daily.json",
                "sourceKind": "DAILY",
                "contentSha256": SHA,
                "rawContentSha256": "1" * 64,
            },
            {
                "fileName": "S_stocks_history.json",
                "sourceKind": "HISTORY",
                "contentSha256": "b" * 64,
                "rawContentSha256": "2" * 64,
            },
        ],
        "identity_map_sha256": "f" * 64,
        "identity_map_content_sha256": "3" * 64,
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
    assert first["triggerMode"] == "BOUNDED_ONE_SHOT"
    assert first["sourceHashCoverage"] == 100
    assert first["rawSourceHashCoverage"] == 100
    assert first["identityMapContentSha256"] == "3" * 64
    assert all(
        row["rawHashBasis"] == "RAW_DRIVE_FILE_BYTES"
        for row in first["sourceFileHashes"]
    )
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

    recurring_window = "2026-08-28"
    recurring = build_lineage_artifact(
        **kwargs,
        collection_window=recurring_window,
        collection_key=recurring_collection_key(recurring_window),
        recurring_activation_authorized=True,
    )
    assert recurring["collectionWindow"] == recurring_window
    assert recurring["collectionKey"] == recurring_collection_key(recurring_window)
    assert recurring["recurringActivationAuthorized"] is True
    assert recurring["triggerMode"] == "SCHEDULED_SECOND_BATCH"

    recovery = build_lineage_artifact(
        **kwargs,
        collection_window=recurring_window,
        collection_key=recurring_collection_key(recurring_window),
        recurring_activation_authorized=True,
        trigger_mode=RECOVERY_TRIGGER_MODE,
    )
    assert recovery["collectionKey"] == recurring["collectionKey"]
    assert recovery["triggerMode"] == RECOVERY_TRIGGER_MODE
    assert safe_aggregate(recovery)["triggerMode"] == RECOVERY_TRIGGER_MODE


def _test_recurring_reservation() -> None:
    try:
        recurring_collection_key("2026-02-30")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid recurring collection date was accepted")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        first_window = "2026-08-28"
        second_window = "2026-09-01"
        first = reserve_producer(
            root,
            "2026-08-28T03:30:00Z",
            collection_window=first_window,
            recurring_activation_authorized=True,
        )
        assert first.name.endswith(f"{recurring_collection_key(first_window)}.json")
        try:
            reserve_producer(
                root,
                "2026-08-28T03:31:00Z",
                collection_window=first_window,
                recurring_activation_authorized=True,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("same recurring collection window was accepted twice")
        second = reserve_producer(
            root,
            "2026-09-01T03:30:00Z",
            collection_window=second_window,
            recurring_activation_authorized=True,
        )
        assert first != second


def _test_bounded_runner() -> None:
    identity_map = {
        "SYNTH": {
            "symbol": "SYNTH",
            "sourceSymbol": "SYNTH",
            "analysisEligible": True,
            "displayName": "synthetic 테스트",
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
            "netIncome": 100.0,
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
        clock_request_counts: list[int] = []

        def utc_now() -> str:
            clock_request_counts.append(len(session.requests))
            return (
                "2026-08-02T00:00:00Z"
                if len(clock_request_counts) == 1
                else "2026-08-02T00:00:01Z"
            )

        result = run_bounded_producer(
            session=session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            safe_output_path=root / "safe.json",
            private_output_path=root / "private.json",
            sentinel_dir=root / "sentinel",
            raw_temp_dir=root / "raw",
            retrieved_at=None,
            utc_now=utc_now,
            approval=PRODUCER_APPROVAL,
            find_file_id=lambda name, parent=None: ids.get((name, parent)),
            download_json_with_bytes=lambda file_id: (
                copy.deepcopy(payloads[file_id]),
                _raw_json_bytes(payloads[file_id]),
            ),
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
        assert clock_request_counts[0] == 3
        assert result["generatedAt"] == "2026-08-02T00:00:00Z"
        assert result["rawSourceHashCoverage"] == 100
        private = json.loads((root / "private.json").read_text(encoding="utf-8"))
        assert private["identityMapContentSha256"] == _raw_sha256(identity_map)
        assert {
            row["rawContentSha256"] for row in private["sourceFileHashes"]
        } == {
            _raw_sha256(payloads[row["id"]])
            for row in daily_files + history_files
        }
        assert canonical_sha256(payloads["daily-S"]) != _raw_sha256(payloads["daily-S"])
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
                download_json_with_bytes=lambda file_id: (
                    copy.deepcopy(payloads[file_id]),
                    _raw_json_bytes(payloads[file_id]),
                ),
                list_files=lambda folder_id: [],
                upload_json=lambda name, payload, parent: None,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate producer run was accepted")
        assert duplicate.requests == []

        recurring_window = "2026-08-28"
        reserve_producer(
            root / "recurring-sentinel",
            "2026-08-29T03:13:00Z",
            collection_window=recurring_window,
            recurring_activation_authorized=True,
        )
        recurring_session = Session(
            [
                Response(200, json.dumps({"0": {"cik_str": 123, "ticker": "SYNTH"}}).encode()),
                Response(200, _zip_bytes({"CIK0000000123.json": _companyfacts(_fact())})),
                Response(200, _zip_bytes({"CIK0000000123.json": _submissions()})),
            ]
        )
        recurring_result = run_bounded_producer(
            session=recurring_session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            safe_output_path=root / "safe-recurring.json",
            private_output_path=root / "private-recurring.json",
            sentinel_dir=root / "recurring-sentinel",
            raw_temp_dir=root / "raw-recurring",
            retrieved_at="2026-08-29T03:13:00Z",
            approval=PRODUCER_RECURRING_APPROVAL,
            collection_window=recurring_window,
            recurring_activation_authorized=True,
            find_file_id=lambda name, parent=None: ids.get((name, parent)),
            download_json_with_bytes=lambda file_id: (
                copy.deepcopy(payloads[file_id]),
                _raw_json_bytes(payloads[file_id]),
            ),
            list_files=lambda folder_id: (
                copy.deepcopy(daily_files)
                if folder_id == "daily-folder"
                else copy.deepcopy(history_files)
            ),
            upload_json=lambda name, payload, parent: None,
        )
        assert recurring_result["recurringActivationAuthorized"] is True
        assert recurring_result["collectionWindow"] == recurring_window
        assert recurring_result["collectionKey"] == recurring_collection_key(recurring_window)

        reserve_producer(
            root / "recovery-sentinel",
            "2026-08-29T03:14:00Z",
            collection_window=recurring_window,
            recurring_activation_authorized=True,
        )
        recovery_session = Session(
            [
                Response(200, json.dumps({"0": {"cik_str": 123, "ticker": "SYNTH"}}).encode()),
                Response(200, _zip_bytes({"CIK0000000123.json": _companyfacts(_fact())})),
                Response(200, _zip_bytes({"CIK0000000123.json": _submissions()})),
            ]
        )
        recovery_result = run_bounded_producer(
            session=recovery_session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@example.test"},
            safe_output_path=root / "safe-recovery.json",
            private_output_path=root / "private-recovery.json",
            sentinel_dir=root / "recovery-sentinel",
            raw_temp_dir=root / "raw-recovery",
            retrieved_at="2026-08-29T03:14:00Z",
            approval=RECOVERY_APPROVAL,
            collection_window=recurring_window,
            recurring_activation_authorized=True,
            trigger_mode=RECOVERY_TRIGGER_MODE,
            find_file_id=lambda name, parent=None: ids.get((name, parent)),
            download_json_with_bytes=lambda file_id: (
                copy.deepcopy(payloads[file_id]),
                _raw_json_bytes(payloads[file_id]),
            ),
            list_files=lambda folder_id: (
                copy.deepcopy(daily_files)
                if folder_id == "daily-folder"
                else copy.deepcopy(history_files)
            ),
            upload_json=lambda name, payload, parent: None,
        )
        assert recovery_result["status"] == "STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_PASS"
        assert recovery_result["triggerMode"] == RECOVERY_TRIGGER_MODE
        assert recovery_result["collectionKey"] == recurring_result["collectionKey"]
        assert len(recovery_session.requests) == 3


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

    main_workflow = MAIN_WORKFLOW.read_text(encoding="utf-8")
    assert "vars.STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_ENABLED == 'true'" in main_workflow
    assert "github.event.schedule == '13 3 * * 2-6'" in main_workflow
    assert PRODUCER_RECURRING_APPROVAL in main_workflow
    assert "--recurring-activation" in main_workflow
    assert '--collection-window "$COLLECTION_WINDOW"' in main_workflow
    assert "actions/cache/restore@v4" in main_workflow
    assert "actions/cache/save@v4" in main_workflow
    assert "restore-keys:" not in main_workflow
    assert main_workflow.index("Run Master Harvester") < main_workflow.index(
        "Run scheduled Stage0 SEC financial lineage producer"
    )

    recovery_workflow = RECOVERY_WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in recovery_workflow
    for forbidden in ("schedule:", "pull_request:", "push:"):
        assert forbidden not in recovery_workflow
    assert RECOVERY_APPROVAL in recovery_workflow
    assert 'test "$GITHUB_REF" = "refs/heads/main"' in recovery_workflow
    assert 'test "$GITHUB_RUN_ATTEMPT" = "1"' in recovery_workflow
    assert "TZ=America/New_York date +%F" in recovery_workflow
    assert "recurring_collection_key" in recovery_workflow
    assert "actions/cache/restore@v4" in recovery_workflow
    assert "actions/cache/save@v4" in recovery_workflow
    assert "lookup-only: true" in recovery_workflow
    assert "restore-keys:" not in recovery_workflow
    assert "stage0-sec-financial-lineage-recovery-${{ steps.window.outputs.collection_key }}" in recovery_workflow
    assert "stage0-sec-financial-lineage-runtime-${{ steps.window.outputs.collection_key }}" not in recovery_workflow
    assert "stage0-sec-financial-lineage-recovery-sentinel" in recovery_workflow
    assert "STAGE0_CURRENT_WINDOW_ALREADY_RESERVED" in recovery_workflow
    assert "--recurring-activation" in recovery_workflow
    assert '--collection-window "$COLLECTION_WINDOW"' in recovery_workflow
    assert f'--trigger-mode "{RECOVERY_TRIGGER_MODE}"' in recovery_workflow
    assert recovery_workflow.index("actions/cache/save@v4") < recovery_workflow.index(
        "Run current-window Stage0 SEC financial lineage producer"
    )
    assert "python harvester.py" not in recovery_workflow
    assert "SEC_USER_AGENT: ${{ secrets.SEC_USER_AGENT }}" in recovery_workflow
    assert "GDRIVE_REFRESH_TOKEN: ${{ secrets.GDRIVE_REFRESH_TOKEN }}" in recovery_workflow
    recovery_upload = recovery_workflow.split("Upload safe aggregate evidence", 1)[1]
    for forbidden in ("private.json", "companyfacts.zip", "submissions.zip"):
        assert forbidden not in recovery_upload


def _test_cli_import_boundary() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["STAGE0_SEC_FINANCIAL_LINEAGE_PRODUCER_APPROVAL"] = ""
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/stage0_financial_publication_lineage_producer.py"),
                "--safe-output",
                str(root / "safe.json"),
                "--private-output",
                str(root / "private.json"),
                "--sentinel-dir",
                str(root / "sentinel"),
                "--raw-temp-dir",
                str(root / "raw"),
            ],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "ModuleNotFoundError" not in result.stderr
        assert "stage0_sec_financial_lineage_producer_approval_required" in result.stderr


def main() -> int:
    _test_input_rows()
    _test_harvester_history_evidence_wiring()
    _test_classification()
    _test_artifact()
    _test_recurring_reservation()
    _test_bounded_runner()
    _test_workflow()
    _test_cli_import_boundary()
    print(
        "[STAGE0_FINANCIAL_PUBLICATION_LINEAGE_PRODUCER] PASS "
        "unknown=0 rawPersistent=false policyChanged=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
