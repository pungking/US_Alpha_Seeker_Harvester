from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harvester as harvester_module  # noqa: E402
from harvester import (  # noqa: E402
    _extract_ohlcv_payload,
    build_corporate_action_lineage,
    build_corporate_action_runtime_audit,
)


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")


def main() -> int:
    fixture = json.loads(
        Path("fixtures/corporate_action_lineage_contract.json").read_text(encoding="utf-8")
    )
    frame = _frame(fixture["baseRows"])

    verified = build_corporate_action_lineage(
        frame,
        record_symbol="SYNTH",
        source_symbol="SYNTH",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["verifiedListingEvidence"],
        observation_count=fixture["observationCount"],
    )
    assert verified["vendor"] == "YFINANCE_YAHOO"
    assert verified["sourceAsOf"] == "2026-07-20"
    assert verified["marketTimezone"] == "America/New_York"
    assert verified["adjustmentType"] == "YFINANCE_AUTO_ADJUSTED_OHLC"
    assert verified["splitAdjustmentStatus"] == "VERIFIED_YFINANCE_AUTO_ADJUSTED"
    assert verified["dividendAdjustmentStatus"] == "VERIFIED_YFINANCE_AUTO_ADJUSTED"
    assert verified["corporateActionStatus"] == "VERIFIED_SPLIT_DIVIDEND_EVENTS_IN_WINDOW"
    assert verified["symbolChangeStatus"] == "VERIFIED_NO_SYMBOL_CHANGE_AS_OF_SOURCE"
    assert verified["delistingStatus"] == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
    assert verified["suspensionStatus"] == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"
    assert verified["survivorshipBiasStatus"] == "VERIFIED_CORPORATE_ACTION_LINEAGE"
    assert verified["lineageVerifiedForComparison"] is True
    assert verified["returnBasis"] == "DIVIDEND_AND_SPLIT_ADJUSTED_PRICE_RETURN"
    assert verified["splitEvents"] == [{"eventEffectiveAt": "2026-07-20", "ratio": 2.0}]
    assert verified["dividendEvents"] == [{"eventEffectiveAt": "2026-07-20", "amount": 0.25}]

    unverified = build_corporate_action_lineage(
        frame.assign(**{"Dividends": 0.0, "Stock Splits": 0.0}),
        record_symbol="RENAMED",
        source_symbol="RENAMED",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["unverifiedListingEvidence"],
    )
    assert unverified["corporateActionStatus"] == "VERIFIED_NO_SPLIT_OR_DIVIDEND_EVENT_IN_WINDOW"
    assert unverified["symbolChangeStatus"].startswith("UNVERIFIED")
    assert unverified["suspensionStatus"].startswith("UNVERIFIED")
    assert unverified["survivorshipBiasStatus"].startswith("UNVERIFIED")

    stale = build_corporate_action_lineage(
        frame,
        record_symbol="STALE",
        source_symbol="STALE",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        expected_market_date="2026-07-21",
        listing_evidence=fixture["verifiedListingEvidence"],
    )
    assert stale["sourceFreshnessStatus"] == "STALE_OR_UNVERIFIED"
    assert stale["lineageVerifiedForComparison"] is False

    stored_tail_trimmed = build_corporate_action_lineage(
        frame,
        record_symbol="TRIMMED",
        source_symbol="TRIMMED",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        expected_market_date="2026-07-20",
        listing_evidence=fixture["verifiedListingEvidence"],
        previous_lineage={"lookbackStart": "2021-07-20", "lookbackEnd": "2026-07-20"},
        stored_rows=[{"date": "2026-07-17"}],
    )
    assert stored_tail_trimmed["sourceAsOf"] == "2026-07-17"
    assert stored_tail_trimmed["sourceFreshnessStatus"] == "STALE_OR_UNVERIFIED"

    partial = build_corporate_action_lineage(
        frame.iloc[:1],
        record_symbol="PARTIAL",
        source_symbol="PARTIAL",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["verifiedListingEvidence"],
    )
    assert partial["historyCoverageStatus"] == "UNVERIFIED_PARTIAL_HISTORY"
    assert partial["lineageVerifiedForComparison"] is False

    incremental_with_full_history = build_corporate_action_lineage(
        frame.iloc[:1],
        record_symbol="INCREMENTAL",
        source_symbol="INCREMENTAL",
        requested_period="7d",
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["verifiedListingEvidence"],
        observation_count=200,
    )
    assert incremental_with_full_history["historyCoverageStatus"] == "VERIFIED_OBSERVED_HISTORY"
    assert incremental_with_full_history["observationCount"] == 200

    explicit_symbol_change = dict(fixture["verifiedListingEvidence"])
    explicit_symbol_change["symbolChangeEvidence"] = {
        "status": "VERIFIED_SYMBOL_CHANGE",
        "oldSymbol": "SYNTH-OLD",
        "newSymbol": "SYNTH",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "2026-07-21T11:00:00Z",
        "eventEffectiveAt": "2026-07-01",
    }
    changed = build_corporate_action_lineage(
        frame,
        record_symbol="SYNTH",
        source_symbol="SYNTH",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=explicit_symbol_change,
    )
    assert changed["symbolChangeStatus"] == "VERIFIED_SYMBOL_CHANGE"
    assert changed["symbolChangeEvidence"]["oldSymbol"] == "SYNTH-OLD"
    assert changed["symbolChangeEvidence"]["newSymbol"] == "SYNTH"
    assert changed["lineageVerifiedForComparison"] is False

    missing_action_columns = build_corporate_action_lineage(
        frame.drop(columns=["Dividends", "Stock Splits"]),
        record_symbol="MISSING-ACTIONS",
        source_symbol="MISSING-ACTIONS",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["verifiedListingEvidence"],
    )
    assert missing_action_columns["corporateActionStatus"] == "UNVERIFIED_ACTION_COLUMNS_INCOMPLETE"
    assert missing_action_columns["lineageVerifiedForComparison"] is False

    explicit_terminal = dict(fixture["verifiedListingEvidence"])
    explicit_terminal["delistingEvidence"] = {
        "status": "VERIFIED_DELISTED",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "2026-07-21T11:00:00Z",
        "eventEffectiveAt": "2026-07-18",
    }
    explicit_terminal["suspensionEvidence"] = {
        "status": "VERIFIED_SUSPENDED",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "2026-07-21T11:00:00Z",
        "eventEffectiveAt": "2026-07-17",
    }
    terminal = build_corporate_action_lineage(
        frame,
        record_symbol="TERMINAL",
        source_symbol="TERMINAL",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=explicit_terminal,
    )
    assert terminal["delistingStatus"] == "VERIFIED_DELISTED"
    assert terminal["suspensionStatus"] == "VERIFIED_SUSPENDED"
    assert terminal["delistingEvidence"]["eventEffectiveAt"] == "2026-07-18"
    assert terminal["suspensionEvidence"]["eventEffectiveAt"] == "2026-07-17"
    assert terminal["lineageVerifiedForComparison"] is False

    garbage_evidence = dict(fixture["verifiedListingEvidence"])
    garbage_evidence["symbolChangeEvidence"] = {
        "status": "VERIFIED_GARBAGE",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "2026-07-21T11:00:00Z",
    }
    garbage = build_corporate_action_lineage(
        frame,
        record_symbol="GARBAGE",
        source_symbol="GARBAGE",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=garbage_evidence,
    )
    assert garbage["symbolChangeStatus"].startswith("UNVERIFIED")
    assert garbage["lineageVerifiedForComparison"] is False

    future_evidence = dict(fixture["verifiedListingEvidence"])
    future_evidence["delistingEvidence"] = {
        "status": "VERIFIED_NOT_DELISTED_AS_OF_SOURCE",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "2026-07-22T11:00:00Z",
    }
    future = build_corporate_action_lineage(
        frame,
        record_symbol="FUTURE",
        source_symbol="FUTURE",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=future_evidence,
        observation_count=fixture["observationCount"],
    )
    assert future["lineageVerifiedForComparison"] is False

    malformed_evidence = dict(fixture["verifiedListingEvidence"])
    malformed_evidence["suspensionEvidence"] = {
        "status": "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE",
        "source": "FIXTURE_AUTHORITATIVE_SOURCE",
        "sourceAsOf": "not-an-iso-timestamp",
    }
    malformed = build_corporate_action_lineage(
        frame,
        record_symbol="MALFORMED",
        source_symbol="MALFORMED",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=malformed_evidence,
        observation_count=fixture["observationCount"],
    )
    assert malformed["suspensionStatus"].startswith("UNVERIFIED")
    assert malformed["lineageVerifiedForComparison"] is False

    legacy_rows, legacy_lineage = _extract_ohlcv_payload([{"date": "2026-07-20"}])
    assert len(legacy_rows) == 1 and legacy_lineage is None
    wrapped_rows, wrapped_lineage = _extract_ohlcv_payload(
        {"data": [{"date": "2026-07-20"}], "lineage": verified}
    )
    assert len(wrapped_rows) == 1 and wrapped_lineage["symbol"] == "SYNTH"

    audit = build_corporate_action_runtime_audit(
        [verified, unverified, {"symbol": "MISSING", "lineageStatus": "REJECTED_VENDOR_MISSING"}],
        trigger_file="STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
        expected_symbols=["SYNTH", "RENAMED", "MISSING"],
        generated_at=fixture["retrievedAt"],
    )
    assert audit["summary"] == {
        "targetRows": 3,
        "lineageRows": 2,
        "verifiedForComparisonRows": 1,
        "unverifiedRows": 1,
        "rejectedRows": 1,
        "missingRows": 0,
        "duplicateRows": 0,
        "lineageCoveragePct": 100.0,
        "comparisonCoverageStatus": "verified_rows_available",
    }
    assert audit["overall"] == "pass"
    assert audit["sourceTimestamps"]["latestRetrievedAt"] == fixture["retrievedAt"]

    uploads: list[tuple[str, object, str]] = []

    class _Ticker:
        def __init__(self, _symbol: str) -> None:
            pass

        def history(self, **kwargs):
            assert kwargs["actions"] is True
            assert kwargs["auto_adjust"] is True
            return frame

    original_find = harvester_module.find_file_id
    original_download = harvester_module.download_json
    original_upload = harvester_module.upload_json
    original_ticker = harvester_module.yf.Ticker
    original_expected = harvester_module.get_expected_market_date_str
    try:
        harvester_module.find_file_id = lambda *_args: "fixture-file-id"
        harvester_module.download_json = lambda *_args: [
            {
                "symbol": "SYNTH",
                "date": "2026-07-17",
                "open": 10.0,
                "high": 11.0,
                "low": 9.5,
                "close": 10.5,
                "volume": 100000,
            }
        ]
        harvester_module.upload_json = lambda name, payload, parent: uploads.append((name, payload, parent))
        harvester_module.yf.Ticker = _Ticker
        harvester_module.get_expected_market_date_str = lambda: "2026-07-20"
        sink: list[dict] = []
        sync_status = harvester_module.sync_ohlcv_incremental(
            "SYNTH",
            "fixture-folder",
            listing_evidence=fixture["verifiedListingEvidence"],
            lineage_sink=sink,
        )
    finally:
        harvester_module.find_file_id = original_find
        harvester_module.download_json = original_download
        harvester_module.upload_json = original_upload
        harvester_module.yf.Ticker = original_ticker
        harvester_module.get_expected_market_date_str = original_expected
    assert sync_status == "UPDATED"
    assert len(uploads) == 1 and isinstance(uploads[0][1], dict)
    assert uploads[0][1]["schemaVersion"] == "ohlcv-lineage-v1"
    assert isinstance(uploads[0][1]["data"], list)
    assert uploads[0][1]["lineage"]["lineageStatus"] == "PRESENT"
    assert sink[0]["symbol"] == "SYNTH"

    # Existing adjusted history must be fully rebased after an incremental action event.
    history_calls: list[str] = []

    class _RebasingTicker:
        def __init__(self, _symbol: str) -> None:
            pass

        def history(self, **kwargs):
            history_calls.append(kwargs["period"])
            return frame.iloc[-1:] if kwargs["period"] == "7d" else frame

    old_payload = {
        "schemaVersion": "ohlcv-lineage-v1",
        "data": [
            {
                "symbol": "SYNTH",
                "date": "2026-07-17",
                "open": 20.0,
                "high": 22.0,
                "low": 19.0,
                "close": 21.0,
                "volume": 100000,
            }
        ],
        "lineage": {**verified, "sourceAsOf": "2026-07-17", "lookbackEnd": "2026-07-17"},
    }
    rebased_uploads: list[tuple[str, object, str]] = []
    try:
        harvester_module.find_file_id = lambda *_args: "fixture-file-id"
        harvester_module.download_json = lambda *_args: old_payload
        harvester_module.upload_json = lambda name, payload, parent: rebased_uploads.append((name, payload, parent))
        harvester_module.yf.Ticker = _RebasingTicker
        harvester_module.get_expected_market_date_str = lambda: "2026-07-20"
        rebased_status = harvester_module.sync_ohlcv_incremental(
            "SYNTH",
            "fixture-folder",
            listing_evidence=fixture["verifiedListingEvidence"],
            lineage_sink=[],
        )
    finally:
        harvester_module.find_file_id = original_find
        harvester_module.download_json = original_download
        harvester_module.upload_json = original_upload
        harvester_module.yf.Ticker = original_ticker
        harvester_module.get_expected_market_date_str = original_expected
    assert rebased_status == "UPDATED"
    assert history_calls == ["7d", fixture["period"]]
    assert rebased_uploads[0][1]["data"][0]["open"] == 10.0

    # A known event still present in the incremental window must not trigger a
    # repeated five-year download when the adjusted overlap is unchanged.
    known_event_calls: list[str] = []

    class _KnownEventTicker:
        def __init__(self, _symbol: str) -> None:
            pass

        def history(self, **kwargs):
            known_event_calls.append(kwargs["period"])
            return frame.iloc[-1:] if kwargs["period"] == "7d" else frame

    known_event_uploads: list[tuple[str, object, str]] = []
    try:
        harvester_module.find_file_id = lambda *_args: "fixture-file-id"
        harvester_module.download_json = lambda *_args: uploads[0][1]
        harvester_module.upload_json = lambda name, payload, parent: known_event_uploads.append(
            (name, payload, parent)
        )
        harvester_module.yf.Ticker = _KnownEventTicker
        harvester_module.get_expected_market_date_str = lambda: "2026-07-21"
        known_event_status = harvester_module.sync_ohlcv_incremental(
            "SYNTH",
            "fixture-folder",
            listing_evidence=fixture["verifiedListingEvidence"],
            lineage_sink=[],
        )
    finally:
        harvester_module.find_file_id = original_find
        harvester_module.download_json = original_download
        harvester_module.upload_json = original_upload
        harvester_module.yf.Ticker = original_ticker
        harvester_module.get_expected_market_date_str = original_expected
    assert known_event_status == "UPDATED"
    assert known_event_calls == ["7d"]
    assert len(known_event_uploads) == 1

    incomplete_audit = build_corporate_action_runtime_audit(
        [verified],
        trigger_file="STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
        expected_symbols=["SYNTH", "ABSENT"],
        generated_at=fixture["retrievedAt"],
    )
    assert incomplete_audit["overall"] == "warn_coverage_mismatch"
    assert incomplete_audit["missingSymbols"] == ["ABSENT"]

    original_listing_fetch = harvester_module.fetch_authoritative_listing_rows
    try:
        harvester_module.fetch_authoritative_listing_rows = lambda: (
            {
                "SYNTH": {
                    "symbol": "SYNTH",
                    "sourceSymbol": "SYNTH",
                    "name": "Synthetic Common Stock",
                    "group": "S",
                    "exchange": "NASDAQ",
                    "listingSource": "fixture_directory",
                    "listingStatus": "ACTIVE",
                    "instrumentType": "common",
                    "analysisEligible": True,
                }
            },
            [{"name": "fixture_directory", "status": "ok", "rowCount": 1}],
        )
        refreshed_mapping, _ = harvester_module.refresh_ticker_mapping_from_authoritative_sources(
            {"SYNTH": {"group": "S", **fixture["verifiedListingEvidence"]}},
            "2026-07-21",
        )
    finally:
        harvester_module.fetch_authoritative_listing_rows = original_listing_fetch
    assert refreshed_mapping["SYNTH"]["symbolChangeEvidence"]["status"] == "VERIFIED_NO_SYMBOL_CHANGE_AS_OF_SOURCE"
    assert refreshed_mapping["SYNTH"]["delistingEvidence"]["status"] == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
    assert refreshed_mapping["SYNTH"]["suspensionEvidence"]["status"] == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"

    renamed = build_corporate_action_lineage(
        frame,
        record_symbol="RENAMED-SYNTH",
        source_symbol="RENAMED-SYNTH",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=fixture["verifiedListingEvidence"],
        observation_count=fixture["observationCount"],
    )
    invariant_keys = {
        "adjustmentType",
        "splitAdjustmentStatus",
        "dividendAdjustmentStatus",
        "corporateActionStatus",
        "symbolChangeStatus",
        "delistingStatus",
        "suspensionStatus",
        "survivorshipBiasStatus",
        "returnBasis",
    }
    assert {key: verified[key] for key in invariant_keys} == {
        key: renamed[key] for key in invariant_keys
    }

    workflow = Path(".github/workflows/main.yml").read_text(encoding="utf-8")
    assert "python scripts/check_corporate_action_lineage_contract.py" in workflow
    assert "HARVESTER_CORPORATE_ACTION_RUNTIME_AUDIT_PATH" in workflow
    assert "state/corporate-action-lineage-runtime-audit.json" in workflow
    harvester_source = Path("harvester.py").read_text(encoding="utf-8")
    assert '"CONTRACT_READY"' in harvester_source
    assert '"COVERAGE_MISMATCH"' in harvester_source

    print("[CORPORATE_ACTION_LINEAGE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
