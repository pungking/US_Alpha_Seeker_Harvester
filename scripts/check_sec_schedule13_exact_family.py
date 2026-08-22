from __future__ import annotations

import importlib.util
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

import official_shadow_runtime as runtime
import sec_finra_shadow_evidence as source


ROOT = Path(__file__).resolve().parents[1]
APPROVAL = "AUTHORIZE SEC SCHEDULE 13 EXACT-FAMILY POST-FIX REPROOF"
ALLOWED_FORMS = {"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}


class FakeResponse:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = (
            payload
            if isinstance(payload, bytes)
            else json.dumps(payload, sort_keys=True).encode("utf-8")
        )
        self.headers = {"Content-Type": "application/json"}
        self.is_redirect = False
        self.is_permanent_redirect = False

    def json(self) -> Any:
        if isinstance(self._payload, bytes):
            raise ValueError("not json")
        return self._payload


def _atom_entry(
    cik: str,
    accession: str,
    form: str | None,
    *,
    valid_reference: bool = True,
    title: str | None = None,
) -> str:
    digits = accession.replace("-", "")
    href = (
        f"https://www.sec.gov/Archives/edgar/data/{cik}/{digits}/"
        f"{accession}-index.htm"
        if valid_reference
        else "https://www.sec.gov/invalid-reference"
    )
    category = f'<category term="{form}" />' if form is not None else ""
    title_node = f"<title>{title}</title>" if title is not None else ""
    return f'<entry>{category}{title_node}<link href="{href}" /></entry>'


def _atom(*entries: str) -> bytes:
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        + "".join(entries)
        + "</feed>"
    ).encode("utf-8")


def _submissions(accession: str, form: str) -> dict[str, Any]:
    return {
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "form": [form],
                "filingDate": ["2026-08-22"],
                "acceptanceDateTime": ["20260822115500"],
            }
        }
    }


def _raw_filing(
    accession: str,
    form: str,
    *,
    parseable_xml: bool = True,
) -> bytes:
    xml = (
        "<?xml version=\"1.0\"?><ownershipDocument>"
        "<issuerCik>0000000001</issuerCik></ownershipDocument>"
        if parseable_xml
        else "<ownershipDocument>"
    )
    return f"""<SEC-DOCUMENT>
ACCESSION NUMBER: {accession}
CONFORMED SUBMISSION TYPE: {form}
<DOCUMENT><TYPE>{form}
<FILENAME>evidence.xml
<TEXT><XML>{xml}</XML></TEXT>
</DOCUMENT></SEC-DOCUMENT>""".encode("utf-8")


class Schedule13Session:
    def __init__(
        self,
        entries: list[tuple[str, str, str | None, bool]],
        *,
        entry_title: str | None = None,
        submissions_form: str | None = None,
        raw_form: str | None = None,
        parseable_xml: bool = True,
        discovery_status: int = 200,
    ) -> None:
        self.entries = entries
        self.entry_title = entry_title
        self.submissions_form = submissions_form
        self.raw_form = raw_form
        self.parseable_xml = parseable_xml
        self.discovery_status = discovery_status
        self.requests: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        del kwargs
        self.requests.append((method, url))
        if "browse-edgar" in url:
            body = _atom(
                *(
                    _atom_entry(
                        cik,
                        accession,
                        form,
                        valid_reference=valid,
                        title=self.entry_title,
                    )
                    for cik, accession, form, valid in self.entries
                )
            )
            return FakeResponse(self.discovery_status, body)
        if "submissions/" in url:
            cik, accession, form, _ = next(
                row for row in self.entries if row[2] in ALLOWED_FORMS
            )
            del cik
            return FakeResponse(
                200,
                _submissions(accession, self.submissions_form or str(form)),
            )
        if url.endswith(".txt"):
            _, accession, form, _ = next(
                row for row in self.entries if row[2] in ALLOWED_FORMS
            )
            return FakeResponse(
                200,
                _raw_filing(
                    accession,
                    self.raw_form or str(form),
                    parseable_xml=self.parseable_xml,
                ),
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def _collect(session: Schedule13Session) -> dict[str, Any]:
    collector = getattr(source, "collect_sec_schedule13_exact_family_evidence")
    return collector(
        session=session,
        sec_user_agent="US Alpha Seeker contact@invalid.test",
        retrieved_at="2026-08-22T16:00:00Z",
    )


def _schedule_request_count(session: Schedule13Session, marker: str) -> int:
    return sum(marker in url for _, url in session.requests)


def _load_runner() -> Any:
    path = ROOT / "scripts" / "run_sec_schedule13_exact_family_reproof.py"
    assert path.exists()
    spec = importlib.util.spec_from_file_location("schedule13_runner", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _test_exact_family_selection() -> None:
    assert hasattr(source, "collect_sec_schedule13_exact_family_evidence")
    sibling = ("100", "0000000100-26-000001", "SC 13E3/A", True)
    allowed = ("200", "0000000200-26-000002", "SC 13D", True)
    session = Schedule13Session([sibling, allowed])
    result = _collect(session)
    discovery = result["schedule13"]["discovery"]
    assert result["status"] == "SEC_SCHEDULE13_EXACT_FAMILY_PASS"
    assert result["requestCounts"]["secSchedule13Discovery"] == 1
    assert result["requestCounts"]["secSchedule13Submissions"] == 1
    assert result["requestCounts"]["secSchedule13RawFiling"] == 1
    assert discovery["discoveryEntryRows"] == 2
    assert discovery["machineReadableFormRows"] == 2
    assert discovery["allowedFormRows"] == 1
    assert discovery["rejectedSiblingFormRows"] == 1
    assert discovery["selectedAllowedFormStatus"] == "EXACT_ALLOWED_FORM_SELECTED"
    assert discovery["discoveryCountLimit"] == 40
    assert discovery["paginationUsed"] is False
    discovery_url = next(url for _, url in session.requests if "browse-edgar" in url)
    query = parse_qs(urlparse(discovery_url).query)
    assert query["count"] == ["40"]
    assert query["start"] == ["0"]
    assert result["exactFamilyFilterApplied"] is True
    assert result["titleOrSummaryHeuristicUsed"] is False


def _test_allowed_form_variants() -> None:
    for index, form in enumerate(sorted(ALLOWED_FORMS), start=1):
        accession = f"0000000{index}00-26-000001"
        session = Schedule13Session(
            [(str(index * 100), accession, form, True)]
        )
        result = _collect(session)
        assert result["status"] == "SEC_SCHEDULE13_EXACT_FAMILY_PASS"
        assert result["schedule13"]["observedForm"] == form


def _test_empty_and_sibling_only() -> None:
    empty_session = Schedule13Session([])
    empty = _collect(empty_session)
    assert empty["status"] == "SEC_SCHEDULE13_NO_CURRENT_ALLOWED_FORM_PASS"
    assert empty["schedule13"]["status"] == "NO_CURRENT_FILING_DISCOVERED"
    assert len(empty_session.requests) == 1

    sibling_session = Schedule13Session(
        [("100", "0000000100-26-000001", "SC 13E3/A", True)]
    )
    sibling = _collect(sibling_session)
    assert sibling["status"] == "SEC_SCHEDULE13_NO_CURRENT_ALLOWED_FORM_PASS"
    assert sibling["schedule13"]["status"] == "NO_CURRENT_ALLOWED_FORM_DISCOVERED"
    assert sibling["schedule13"]["discovery"]["rejectedSiblingFormRows"] == 1
    assert len(sibling_session.requests) == 1


def _test_discovery_contract_failures() -> None:
    reference, status, shape = source._atom_reference(b"<feed", ALLOWED_FORMS)
    assert reference is None
    assert status == "DISCOVERY_XML_INVALID"
    assert shape["discoveryEntryRows"] == 0

    missing_session = Schedule13Session(
        [("100", "0000000100-26-000001", None, True)],
        entry_title="SC 13D should not be inferred from a title",
    )
    missing = _collect(missing_session)
    assert missing["status"] == "SEC_SCHEDULE13_DISCOVERY_METADATA_INVALID"
    assert missing["schedule13"]["status"] == (
        "DISCOVERY_FORM_METADATA_MISSING_OR_INVALID"
    )
    assert len(missing_session.requests) == 1

    invalid_link_session = Schedule13Session(
        [("100", "0000000100-26-000001", "SC 13G", False)]
    )
    invalid_link = _collect(invalid_link_session)
    assert invalid_link["status"] == "SEC_SCHEDULE13_DISCOVERY_METADATA_INVALID"
    assert invalid_link["schedule13"]["status"] == (
        "DISCOVERY_ALLOWED_FORM_REFERENCE_INVALID"
    )
    assert len(invalid_link_session.requests) == 1


def _test_deterministic_first_allowed_and_lineage_failures() -> None:
    first = ("100", "0000000100-26-000001", "SC 13G", True)
    second = ("200", "0000000200-26-000002", "SC 13D", True)
    deterministic = _collect(Schedule13Session([first, second]))
    assert deterministic == _collect(Schedule13Session([first, second]))
    assert deterministic["schedule13"]["observedForm"] == "SC 13G"
    assert deterministic["schedule13"]["discovery"]["allowedFormRows"] == 2

    submissions_mismatch = _collect(
        Schedule13Session([first], submissions_form="SC 13E3/A")
    )
    assert submissions_mismatch["status"] == (
        "SEC_SCHEDULE13_SUBMISSIONS_LINEAGE_INVALID"
    )
    assert submissions_mismatch["requestCounts"]["secSchedule13RawFiling"] == 0

    raw_mismatch = _collect(Schedule13Session([first], raw_form="SC 13E3/A"))
    assert raw_mismatch["status"] == "SEC_SCHEDULE13_RAW_FILING_CONTRACT_INVALID"

    raw_invalid = _collect(Schedule13Session([first], parseable_xml=False))
    assert raw_invalid["status"] == "SEC_SCHEDULE13_RAW_FILING_CONTRACT_INVALID"


def _test_http_failure_and_no_retry() -> None:
    expected = {
        400: "SEC_SCHEDULE13_DISCOVERY_METADATA_INVALID",
        403: "SEC_SCHEDULE13_AUTH_OR_NETWORK_BLOCKED",
        429: "SEC_SCHEDULE13_RATE_LIMITED",
        500: "SEC_SCHEDULE13_TRANSIENT_FAILURE",
    }
    for http_status, verdict in expected.items():
        session = Schedule13Session([], discovery_status=http_status)
        result = _collect(session)
        assert result["status"] == verdict
        assert len(session.requests) == 1
        assert result["retryCount"] == 0
        assert result["paginationUsed"] is False


def _test_sentinel_hash_basis() -> None:
    contract = runtime.build_collection_contract(
        source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
        schema_version="sec-finra-shadow-evidence-v1",
        source_ids=["SEC_SCHEDULES_13D_13G"],
        source_window_bases={
            "SEC_SCHEDULES_13D_13G": "SEC_CURRENT_FILINGS_PUBLICATION_DATE_ET"
        },
        retrieved_at="2026-08-22T16:00:00Z",
    )
    durable = runtime.build_durable_collection_sentinel(
        contract,
        status="FAILED",
        reserved_at="2026-08-22T16:00:00Z",
        completed_at="2026-08-22T16:01:00Z",
        artifact_sha256="a" * 64,
        request_counts={"secDiscovery": 1},
    )
    assert durable["artifactHashBasis"] == (
        "PRE_PERSISTENCE_COLLECTION_EVIDENCE"
    )
    assert runtime.classify_durable_collection_sentinel(durable, contract) == (
        "EXISTING_FAILED"
    )
    with tempfile.TemporaryDirectory() as directory:
        reservation = runtime.reserve_collection_sentinel(
            Path(directory),
            source_family="SEC_FINRA_OFFICIAL_EVIDENCE",
            collection_key=contract["collectionKey"],
            reserved_at="2026-08-22T16:00:00Z",
        )
        runtime.finish_collection_sentinel(
            Path(reservation["path"]),
            status="FAILED",
            completed_at="2026-08-22T16:01:00Z",
            artifact_sha256="b" * 64,
            request_counts={"secDiscovery": 1},
        )
        terminal = json.loads(Path(reservation["path"]).read_text())
        assert terminal["artifactHashBasis"] == (
            "PRE_PERSISTENCE_COLLECTION_EVIDENCE"
        )


def _test_targeted_runner_and_duplicate_block() -> None:
    runner = _load_runner()
    session = Schedule13Session(
        [("100", "0000000100-26-000001", "SC 13D", True)],
        entry_title="private fixture filing title",
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "result.json"
        sentinel = root / "sentinel.json"
        preserved = root / "baseline-failed-sentinel.json"
        preserved.write_text('{"status":"FAILED"}\n', encoding="utf-8")
        before = preserved.read_bytes()
        result = runner.run_sec_schedule13_exact_family_reproof(
            session=session,
            environment={"SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test"},
            output_path=output,
            sentinel_path=sentinel,
            retrieved_at="2026-08-22T16:00:00Z",
            approval=APPROVAL,
        )
        terminal = json.loads(sentinel.read_text())
        assert result["status"] == "SEC_SCHEDULE13_EXACT_FAMILY_PASS"
        assert result["preservedBaselineRunId"] == 32580233030
        assert result["googleDrivePublished"] is False
        assert result["recurringActivationAuthorized"] is False
        assert result["unknownOrUnclassifiedRows"] == 0
        assert result["canonicalSourceChanged"] is False
        assert result["policyImpact"] == "NONE_REPORT_ONLY"
        assert result["brokerOrSidecarStateMutation"] is False
        assert result["telegramActualSend"] is False
        assert all(
            result["requestCounts"][key] == 0
            for key in (
                "section16",
                "form13F",
                "finraOauth",
                "finraMetadata",
                "finraData",
                "federalReserve",
                "fred",
                "bea",
                "bls",
                "blsCalendar",
                "toss",
            )
        )
        assert terminal["status"] == "COMPLETE"
        assert terminal["artifactHashBasis"] == (
            "FINAL_TARGETED_REPROOF_ARTIFACT_BYTES"
        )
        assert terminal["resultSha256"] == hashlib.sha256(
            output.read_bytes()
        ).hexdigest()
        assert preserved.read_bytes() == before
        request_count = len(session.requests)
        try:
            runner.run_sec_schedule13_exact_family_reproof(
                session=session,
                environment={
                    "SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test"
                },
                output_path=output,
                sentinel_path=sentinel,
                retrieved_at="2026-08-22T16:00:00Z",
                approval=APPROVAL,
            )
        except FileExistsError:
            pass
        else:
            raise AssertionError("duplicate targeted reproof was not blocked")
        assert len(session.requests) == request_count

        wrong_approval_session = Schedule13Session(
            [("300", "0000000300-26-000003", "SC 13G", True)]
        )
        try:
            runner.run_sec_schedule13_exact_family_reproof(
                session=wrong_approval_session,
                environment={
                    "SEC_USER_AGENT": "US Alpha Seeker contact@invalid.test"
                },
                output_path=root / "wrong-result.json",
                sentinel_path=root / "wrong-sentinel.json",
                retrieved_at="2026-08-22T16:00:00Z",
                approval="not approved",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid approval was accepted")
        assert wrong_approval_session.requests == []
        assert not (root / "wrong-sentinel.json").exists()


def _test_static_workflow_and_redaction() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "sec-schedule13-exact-family-reproof.yml"
    ).read_text(encoding="utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "schedule:" not in workflow
    assert APPROVAL in workflow
    assert "SEC_USER_AGENT" in workflow
    for forbidden in (
        "FINRA_CLIENT_ID",
        "FINRA_CLIENT_SECRET",
        "BEA_API_KEY",
        "BLS_API_KEY",
        "FRED_API_KEY",
        "GDRIVE_REFRESH_TOKEN",
        "TELEGRAM_TOKEN",
    ):
        assert forbidden not in workflow

    session = Schedule13Session(
        [("100", "0000000100-26-000001", "SC 13D", True)]
    )
    rendered = json.dumps(_collect(session), sort_keys=True)
    for forbidden in (
        "0000000100-26-000001",
        "0000000100",
        "contact@invalid.test",
        "private fixture filing title",
        "<feed",
        "<SEC-DOCUMENT>",
    ):
        assert forbidden not in rendered


def main() -> int:
    _test_exact_family_selection()
    _test_allowed_form_variants()
    _test_empty_and_sibling_only()
    _test_discovery_contract_failures()
    _test_deterministic_first_allowed_and_lineage_failures()
    _test_http_failure_and_no_retry()
    _test_sentinel_hash_basis()
    _test_targeted_runner_and_duplicate_block()
    _test_static_workflow_and_redaction()
    print(
        "[SEC_SCHEDULE13_EXACT_FAMILY] PASS externalRequests=0 "
        "pagination=0 retry=0 rawStored=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
