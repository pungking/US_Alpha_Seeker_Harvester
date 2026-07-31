from __future__ import annotations

import json
import sys
import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harvester as harvester_module  # noqa: E402
from harvester import (  # noqa: E402
    _external_evidence_contract_valid,
    _external_evidence_time_valid,
    _extract_ohlcv_payload,
    _fetch_finnhub_symbol_change_coverage,
    _fetch_fmp_delisting_coverage,
    _fetch_nasdaq_suspension_coverage,
    _parse_fmp_delisted_rows,
    _parse_nasdaq_current_halt_rss,
    _parse_nasdaq_halt_rows,
    _refresh_dispatch_external_corporate_action_coverage,
    apply_external_corporate_action_coverage,
    build_corporate_action_lineage,
    build_corporate_action_runtime_audit,
    build_mapping_corporate_action_runtime_audit,
    refresh_corporate_action_lineage_evidence,
)


def _frame(rows: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date")


def _evidence_for_symbol(base: dict, symbol: str) -> dict:
    result = json.loads(json.dumps(base))
    for key in ("symbolChangeEvidence", "delistingEvidence", "suspensionEvidence"):
        evidence = result.get(key)
        if isinstance(evidence, dict):
            evidence["requestedSymbol"] = symbol
            if evidence.get("symbolMatchStatus") != "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE":
                evidence["matchedSymbol"] = symbol
    return result


def main() -> int:
    assert harvester_module.FINNHUB_SYMBOL_CHANGE_PREMIUM_ENABLED is False
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
    assert _external_evidence_contract_valid(
        verified["symbolChangeEvidence"], expected_symbol="SYNTH"
    )

    incomplete_query_proof = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "NO-PROOF"
    )
    incomplete_query_proof["delistingEvidence"].pop("responseSha256")
    incomplete = build_corporate_action_lineage(
        frame,
        record_symbol="NO-PROOF",
        source_symbol="NO-PROOF",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=incomplete_query_proof,
        observation_count=fixture["observationCount"],
    )
    assert incomplete["delistingStatus"] == "UNVERIFIED_DELISTING_EVENT_SOURCE_MISSING"
    assert incomplete["lineageVerifiedForComparison"] is False

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
        listing_evidence=_evidence_for_symbol(fixture["verifiedListingEvidence"], "STALE"),
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
        listing_evidence=_evidence_for_symbol(fixture["verifiedListingEvidence"], "TRIMMED"),
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
        listing_evidence=_evidence_for_symbol(fixture["verifiedListingEvidence"], "PARTIAL"),
    )
    assert partial["historyCoverageStatus"] == "UNVERIFIED_PARTIAL_HISTORY"
    assert partial["lineageVerifiedForComparison"] is False

    incremental_with_full_history = build_corporate_action_lineage(
        frame.iloc[:1],
        record_symbol="INCREMENTAL",
        source_symbol="INCREMENTAL",
        requested_period="7d",
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=_evidence_for_symbol(fixture["verifiedListingEvidence"], "INCREMENTAL"),
        observation_count=200,
    )
    assert incremental_with_full_history["historyCoverageStatus"] == "VERIFIED_OBSERVED_HISTORY"
    assert incremental_with_full_history["observationCount"] == 200

    explicit_symbol_change = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "SYNTH"
    )
    explicit_symbol_change["symbolChangeEvidence"].update(
        {
            "status": "VERIFIED_SYMBOL_CHANGE",
            "matchedSymbol": "SYNTH",
            "symbolMatchStatus": "EXACT_EVENT_MATCH",
            "oldSymbol": "SYNTH-OLD",
            "newSymbol": "SYNTH",
            "eventEffectiveAt": "2026-07-01",
        }
    )
    changed = build_corporate_action_lineage(
        frame,
        record_symbol="SYNTH",
        source_symbol="SYNTH",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=explicit_symbol_change,
        observation_count=fixture["observationCount"],
    )
    assert changed["symbolChangeStatus"] == "VERIFIED_SYMBOL_CHANGE"
    assert changed["symbolChangeEvidence"]["oldSymbol"] == "SYNTH-OLD"
    assert changed["symbolChangeEvidence"]["newSymbol"] == "SYNTH"
    assert changed["lineageVerifiedForComparison"] is True

    missing_action_columns = build_corporate_action_lineage(
        frame.drop(columns=["Dividends", "Stock Splits"]),
        record_symbol="MISSING-ACTIONS",
        source_symbol="MISSING-ACTIONS",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=_evidence_for_symbol(
            fixture["verifiedListingEvidence"], "MISSING-ACTIONS"
        ),
    )
    assert missing_action_columns["corporateActionStatus"] == "UNVERIFIED_ACTION_COLUMNS_INCOMPLETE"
    assert missing_action_columns["lineageVerifiedForComparison"] is False

    explicit_terminal = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "TERMINAL"
    )
    explicit_terminal["delistingEvidence"].update(
        {
            "status": "VERIFIED_DELISTED",
            "matchedSymbol": "TERMINAL",
            "symbolMatchStatus": "EXACT_EVENT_MATCH",
            "eventEffectiveAt": "2026-07-18",
        }
    )
    explicit_terminal["suspensionEvidence"].update(
        {
            "status": "VERIFIED_SUSPENDED",
            "matchedSymbol": "TERMINAL",
            "symbolMatchStatus": "EXACT_EVENT_MATCH",
            "eventEffectiveAt": "2026-07-17",
        }
    )
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

    garbage_evidence = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "GARBAGE"
    )
    garbage_evidence["symbolChangeEvidence"]["status"] = "VERIFIED_GARBAGE"
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

    future_evidence = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "FUTURE"
    )
    future_evidence["delistingEvidence"].update(
        {
            "sourceAsOf": "2026-07-22T11:00:00Z",
            "retrievedAt": "2026-07-22T11:00:00Z",
            "coverageEnd": "2026-07-22",
        }
    )
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

    failed_refresh_listing = _evidence_for_symbol(
        fixture["verifiedListingEvidence"],
        "SYNTH",
    )
    failed_refresh_listing["suspensionEvidence"].update(
        {
            "status": "UNVERIFIED_EXTERNAL_SOURCE_REFRESH_FAILED",
            "retrievedAt": "2026-07-22T11:00:00Z",
            "reason": "fixture_refresh_failed",
        }
    )
    refreshed_lineage = refresh_corporate_action_lineage_evidence(
        verified,
        failed_refresh_listing,
    )
    assert refreshed_lineage["retrievedAt"] == fixture["retrievedAt"]
    assert refreshed_lineage["lineageEvaluatedAt"] == "2026-07-22T11:00:00Z"
    assert refreshed_lineage["suspensionStatus"].startswith("UNVERIFIED")
    assert refreshed_lineage["lineageVerifiedForComparison"] is False

    malformed_evidence = _evidence_for_symbol(
        fixture["verifiedListingEvidence"], "MALFORMED"
    )
    malformed_evidence["suspensionEvidence"]["sourceAsOf"] = "not-an-iso-timestamp"
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
        "emptyScope": False,
        "structuralContractReady": True,
        "lineageCoveragePct": 100.0,
        "comparisonCoverageStatus": "verified_rows_available_partial",
    }
    assert audit["overall"] == "warn_lineage_rejected"
    assert audit["sourceTimestamps"]["latestRetrievedAt"] == fixture["retrievedAt"]

    external_fixture = fixture["externalSourceFixtures"]
    delisted_rows = _parse_fmp_delisted_rows(external_fixture["fmpDelistedRows"])
    assert delisted_rows == [
        {
            "symbol": "OLDX",
            "companyName": "Old Example Corp",
            "exchange": "NASDAQ",
            "ipoDate": "2018-01-02",
            "eventEffectiveAt": "2026-06-30",
        }
    ]
    halt_rows = _parse_nasdaq_halt_rows(external_fixture["nasdaqHaltHtml"])
    assert [row["symbol"] for row in halt_rows] == ["HALT", "OPEN"]
    assert halt_rows[0]["resumedAt"] == "2026-07-18T09:35:00-04:00"
    assert halt_rows[1]["resumedAt"] is None
    rss_valid, rss_published, rss_rows = _parse_nasdaq_current_halt_rss(
        external_fixture["nasdaqCurrentHaltRss"].encode("utf-8")
    )
    assert rss_valid is True
    assert rss_published == "Tue, 21 Jul 2026 11:00:00 GMT"
    assert [row["symbol"] for row in rss_rows] == ["OPEN", "FUTURE"]

    coverage = {
        "schemaVersion": "external-corporate-action-coverage-v1",
        "generatedAt": "2026-07-21T11:00:00Z",
        "overall": "blocked_external_source_contract",
        "sources": {
            "symbolChange": {
                "status": "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
                "source": "FMP_SYMBOL_CHANGE",
                "reason": "entitlement_required",
            },
            "delisting": {
                "status": "SUCCESS",
                "source": "FMP_DELISTED_COMPANIES",
                "sourceAsOf": "2026-07-21T11:00:00Z",
                "retrievedAt": "2026-07-21T11:00:00Z",
                "coverageStart": external_fixture["coverageStart"],
                "coverageEnd": external_fixture["coverageEnd"],
                "partialResponse": False,
                "responseSha256": "d" * 64,
                "queryScope": "FIXTURE_COMPLETE_SOURCE_WINDOW",
                "requestScopeSymbolsSha256": "1" * 64,
            },
            "suspension": {
                "status": "SUCCESS",
                "source": "NASDAQ_TRADER_HALT_HISTORY",
                "sourceAsOf": "2026-07-21T11:00:00Z",
                "retrievedAt": "2026-07-21T11:00:00Z",
                "coverageStart": external_fixture["coverageStart"],
                "coverageEnd": external_fixture["coverageEnd"],
                "partialResponse": False,
                "responseSha256": "e" * 64,
                "queryScope": "FIXTURE_COMPLETE_SOURCE_WINDOW",
                "requestScopeSymbolsSha256": "1" * 64,
            },
        },
        "events": {
            "symbolChanges": [],
            "delistings": delisted_rows,
            "suspensions": [*halt_rows, *rss_rows],
        },
    }
    legacy_dispatch_coverage = json.loads(json.dumps(coverage))
    legacy_dispatch_coverage["sources"]["symbolChange"] = {
        "status": "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
        "source": "FMP_OR_FINNHUB_SYMBOL_CHANGE",
        "reason": "entitlement_and_verified_response_fixture_required",
    }
    current_dispatch_coverage = json.loads(json.dumps(coverage))
    current_dispatch_coverage["sources"]["symbolChange"] = {
        "status": "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
        "source": "FINNHUB_SYMBOL_CHANGE",
        "reason": "entitlement_or_auth_http_403",
    }
    free_tier_dispatch_coverage = json.loads(json.dumps(coverage))
    free_tier_dispatch_coverage["sources"]["symbolChange"] = {
        "status": "BLOCKED_EXTERNAL_SOURCE_CONTRACT",
        "source": "FINNHUB_SYMBOL_CHANGE",
        "reason": "premium_source_disabled_free_tier",
        "requestCount": 0,
        "capabilityMode": "FREE_TIER",
    }
    original_external_fetch = harvester_module.fetch_external_corporate_action_coverage
    dispatch_refresh_calls = []
    try:
        harvester_module.fetch_external_corporate_action_coverage = (
            lambda symbols, **kwargs: (
                dispatch_refresh_calls.append(
                    {
                        "symbols": list(symbols),
                        "previous": kwargs.get("previous_coverage"),
                    }
                )
                or free_tier_dispatch_coverage
            )
        )
        refreshed_dispatch_coverage = _refresh_dispatch_external_corporate_action_coverage(
            ["SYNTH"],
            legacy_dispatch_coverage,
        )
        reused_dispatch_coverage = _refresh_dispatch_external_corporate_action_coverage(
            ["SYNTH"],
            current_dispatch_coverage,
        )
        stable_free_tier_coverage = _refresh_dispatch_external_corporate_action_coverage(
            ["SYNTH"],
            free_tier_dispatch_coverage,
        )
    finally:
        harvester_module.fetch_external_corporate_action_coverage = original_external_fetch
    assert len(dispatch_refresh_calls) == 2
    assert dispatch_refresh_calls[0]["symbols"] == ["SYNTH"]
    assert dispatch_refresh_calls[0]["previous"] == legacy_dispatch_coverage
    assert dispatch_refresh_calls[1]["previous"] == current_dispatch_coverage
    assert refreshed_dispatch_coverage == free_tier_dispatch_coverage
    assert reused_dispatch_coverage == free_tier_dispatch_coverage
    assert stable_free_tier_coverage == free_tier_dispatch_coverage

    blocked_comparison_audit = build_corporate_action_runtime_audit(
        [unverified],
        trigger_file="STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
        expected_symbols=["RENAMED"],
        generated_at=fixture["retrievedAt"],
        external_source_coverage=coverage,
    )
    assert blocked_comparison_audit["overall"] == "warn_comparison_lineage_unverified"
    assert (
        blocked_comparison_audit["summary"]["comparisonCoverageStatus"]
        == "blocked_external_source_contract"
    )
    assert "events" not in blocked_comparison_audit["externalSourceCoverage"]
    applied_map, applied_summary = apply_external_corporate_action_coverage(
        {
            symbol: {
                "symbol": symbol,
                "sourceSymbol": symbol,
                "group": symbol[0],
                "analysisEligible": True,
                "listingStatus": "ACTIVE",
            }
            for symbol in ("SYNTH", "OLDX", "OPEN", "HALT", "FUTURE", "VOL")
        },
        coverage,
    )
    assert "symbolChangeEvidence" not in applied_map["SYNTH"]
    assert (
        applied_map["SYNTH"]["delistingEvidence"]["status"]
        == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
    )
    assert applied_map["SYNTH"]["delistingEvidence"]["matchedSymbol"] is None
    assert (
        applied_map["SYNTH"]["delistingEvidence"]["symbolMatchStatus"]
        == "NO_EXACT_EVENT_MATCH_IN_COMPLETE_RESPONSE"
    )
    assert _external_evidence_contract_valid(
        applied_map["SYNTH"]["delistingEvidence"],
        expected_symbol="SYNTH",
    )
    assert (
        applied_map["SYNTH"]["suspensionEvidence"]["status"]
        == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"
    )
    assert (
        applied_map["FUTURE"]["suspensionEvidence"]["status"]
        == "VERIFIED_SUSPENDED"
    )
    assert (
        applied_map["OLDX"]["delistingEvidence"]["status"]
        == "UNVERIFIED_SOURCE_CONFLICT"
    )
    assert applied_map["OPEN"]["suspensionEvidence"]["status"] == "VERIFIED_SUSPENDED"
    assert applied_map["OPEN"]["suspensionEvidence"]["matchedSymbol"] == "OPEN"
    assert (
        applied_map["HALT"]["suspensionEvidence"]["status"]
        == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"
    )
    assert (
        applied_map["VOL"]["suspensionEvidence"]["status"]
        == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"
    )
    assert applied_summary["unknownRows"] == 0
    assert applied_summary["sourceConflictRows"] == 1
    assert applied_summary["symbolChangeBlockedRows"] == 6
    reapplied_map, reapplied_summary = apply_external_corporate_action_coverage(
        {
            symbol: {
                "symbol": symbol,
                "sourceSymbol": symbol,
                "group": symbol[0],
                "analysisEligible": True,
                "listingStatus": "ACTIVE",
            }
            for symbol in ("SYNTH", "OLDX", "OPEN", "HALT", "FUTURE", "VOL")
        },
        coverage,
    )
    assert reapplied_map == applied_map
    assert reapplied_summary == applied_summary

    partial_proof = dict(applied_map["SYNTH"]["delistingEvidence"])
    partial_proof["partialResponse"] = True
    assert not _external_evidence_contract_valid(partial_proof, expected_symbol="SYNTH")
    stale_proof = dict(applied_map["SYNTH"]["delistingEvidence"])
    stale_proof["coverageEnd"] = "2026-07-19"
    assert not _external_evidence_time_valid(
        stale_proof,
        "2026-07-20",
        fixture["retrievedAt"],
        expected_symbol="SYNTH",
        lookback_start="2026-07-17",
    )

    alias_coverage = json.loads(json.dumps(coverage))
    alias_coverage["sources"]["symbolChange"] = {
        "status": "SUCCESS",
        "source": "FIXTURE_SYMBOL_CHANGE_SOURCE",
        "sourceAsOf": "2026-07-21T11:00:00Z",
        "retrievedAt": "2026-07-21T11:00:00Z",
        "coverageStart": external_fixture["coverageStart"],
        "coverageEnd": external_fixture["coverageEnd"],
        "partialResponse": False,
        "responseSha256": "f" * 64,
        "queryScope": "FIXTURE_COMPLETE_SOURCE_WINDOW",
        "requestScopeSymbolsSha256": "1" * 64,
    }
    alias_coverage["events"]["symbolChanges"] = [
        {
            "oldSymbol": "OLD",
            "newSymbol": "MID",
            "eventEffectiveAt": "2025-01-02",
        },
        {
            "oldSymbol": "MID",
            "newSymbol": "NEW",
            "eventEffectiveAt": "2026-01-02",
        },
    ]
    alias_map, alias_summary = apply_external_corporate_action_coverage(
        {
            "NEW": {
                "symbol": "NEW",
                "sourceSymbol": "NEW",
                "group": "N",
                "analysisEligible": True,
                "listingStatus": "ACTIVE",
            }
        },
        alias_coverage,
    )
    assert alias_map["NEW"]["symbolChangeEvidence"]["status"] == "VERIFIED_SYMBOL_CHANGE"
    assert alias_map["NEW"]["symbolChangeEvidence"]["oldSymbol"] == "OLD"
    assert alias_map["NEW"]["symbolChangeEvidence"]["newSymbol"] == "NEW"
    assert len(alias_map["NEW"]["symbolChangeEvidence"]["events"]) == 2
    assert alias_summary["unknownRows"] == 0

    partial_source_coverage = json.loads(json.dumps(coverage))
    partial_source_coverage["sources"]["delisting"]["status"] = "UNVERIFIED_PARTIAL_RESPONSE"
    partial_map, _ = apply_external_corporate_action_coverage(
        {
            "PARTIAL": {
                "symbol": "PARTIAL",
                "sourceSymbol": "PARTIAL",
                "group": "P",
                "analysisEligible": True,
                "listingStatus": "ACTIVE",
            }
        },
        partial_source_coverage,
    )
    assert "delistingEvidence" not in partial_map["PARTIAL"]
    preserved_map, _ = apply_external_corporate_action_coverage(
        {
            "PRESERVED": {
                "symbol": "PRESERVED",
                "sourceSymbol": "PRESERVED",
                "group": "P",
                "analysisEligible": True,
                "listingStatus": "ACTIVE",
                "delistingEvidence": _evidence_for_symbol(
                    fixture["verifiedListingEvidence"],
                    "PRESERVED",
                )["delistingEvidence"],
            }
        },
        partial_source_coverage,
    )
    assert (
        preserved_map["PRESERVED"]["delistingEvidence"]["status"]
        == "UNVERIFIED_EXTERNAL_SOURCE_REFRESH_FAILED"
    )
    assert (
        preserved_map["PRESERVED"]["delistingEvidence"]["preservedStatus"]
        == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
    )

    class _TimeoutSession:
        def get(self, *_args, **_kwargs):
            raise harvester_module.requests.Timeout("fixture timeout")

    timeout_summary, timeout_rows = _fetch_nasdaq_suspension_coverage(
        _TimeoutSession(),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert timeout_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert timeout_rows == []

    class _FixtureResponse:
        def __init__(self, *, status_code=200, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload
            self.text = text
            self.content = (
                json.dumps(payload, sort_keys=True).encode("utf-8")
                if payload is not None
                else text.encode("utf-8")
            )

        def json(self):
            return self._payload

    class _FmpSession:
        def get(self, *_args, **_kwargs):
            return _FixtureResponse(
                payload=external_fixture["fmpDelistedRows"],
            )

    class _FinnhubSession:
        def __init__(self):
            self.request_count = 0

        def get(self, *_args, **kwargs):
            self.request_count += 1
            params = kwargs["params"]
            data = []
            if params["from"] <= "2026-07-01" <= params["to"]:
                data.append(
                    {
                        "atDate": "2026-07-01",
                        "oldSymbol": "SYNTH-OLD",
                        "newSymbol": "SYNTH",
                    }
                )
            return _FixtureResponse(
                payload={
                    "data": data,
                    "fromDate": params["from"],
                    "toDate": params["to"],
                }
            )

    enabled_finnhub_session = _FinnhubSession()
    symbol_summary, symbol_rows = _fetch_finnhub_symbol_change_coverage(
        enabled_finnhub_session,
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
        premium_enabled=True,
    )
    assert symbol_summary["status"] == "SUCCESS"
    assert symbol_summary["partialResponse"] is False
    assert symbol_summary["paginationComplete"] is True
    assert symbol_summary["coverageStart"] == "2021-07-20"
    assert symbol_summary["coverageEnd"] == "2026-07-21"
    assert symbol_rows == [
        {
            "oldSymbol": "SYNTH-OLD",
            "newSymbol": "SYNTH",
            "eventEffectiveAt": "2026-07-01",
        }
    ]
    assert enabled_finnhub_session.request_count > 0

    class _NoRequestFinnhubSession:
        def __init__(self):
            self.request_count = 0

        def get(self, *_args, **_kwargs):
            self.request_count += 1
            raise AssertionError("free-tier mode must not call the Premium endpoint")

    disabled_finnhub_session = _NoRequestFinnhubSession()
    disabled_previous = {
        "events": {
            "symbolChanges": [
                {
                    "oldSymbol": "PRESERVED-OLD",
                    "newSymbol": "PRESERVED",
                    "eventEffectiveAt": "2026-01-02",
                }
            ]
        }
    }
    disabled_symbol_summary, disabled_symbol_rows = (
        _fetch_finnhub_symbol_change_coverage(
            disabled_finnhub_session,
            api_key="fixture-key",
            coverage_start=datetime.date(2021, 7, 20),
            coverage_end=datetime.date(2026, 7, 21),
            retrieved_at="2026-07-21T11:00:00Z",
            previous_coverage=disabled_previous,
            premium_enabled=False,
        )
    )
    assert disabled_finnhub_session.request_count == 0
    assert disabled_symbol_summary["status"] == "BLOCKED_EXTERNAL_SOURCE_CONTRACT"
    assert disabled_symbol_summary["reason"] == "premium_source_disabled_free_tier"
    assert disabled_symbol_summary["requestCount"] == 0
    assert disabled_symbol_summary["capabilityMode"] == "FREE_TIER"
    assert disabled_symbol_rows == disabled_previous["events"]["symbolChanges"]
    repeated_disabled_summary, repeated_disabled_rows = (
        _fetch_finnhub_symbol_change_coverage(
            disabled_finnhub_session,
            api_key="fixture-key",
            coverage_start=datetime.date(2021, 7, 20),
            coverage_end=datetime.date(2026, 7, 21),
            retrieved_at="2026-07-21T11:00:00Z",
            previous_coverage=disabled_previous,
            premium_enabled=False,
        )
    )
    assert repeated_disabled_summary == disabled_symbol_summary
    assert repeated_disabled_rows == disabled_symbol_rows
    assert disabled_finnhub_session.request_count == 0

    class _BlockedFinnhubSession:
        def get(self, *_args, **_kwargs):
            return _FixtureResponse(status_code=403, payload={"error": "forbidden"})

    blocked_symbol_summary, blocked_symbol_rows = (
        _fetch_finnhub_symbol_change_coverage(
            _BlockedFinnhubSession(),
            api_key="fixture-key",
            coverage_start=datetime.date(2021, 7, 20),
            coverage_end=datetime.date(2026, 7, 21),
            retrieved_at="2026-07-21T11:00:00Z",
            previous_coverage={},
            premium_enabled=True,
        )
    )
    assert blocked_symbol_summary["status"] == "BLOCKED_EXTERNAL_SOURCE_CONTRACT"
    assert blocked_symbol_summary["reason"] == "entitlement_or_auth_http_403"
    assert blocked_symbol_rows == []

    class _StatusFinnhubSession:
        def __init__(self, status_code):
            self.status_code = status_code
            self.request_count = 0

        def get(self, *_args, **_kwargs):
            self.request_count += 1
            return _FixtureResponse(status_code=self.status_code, payload={})

    rate_limited_session = _StatusFinnhubSession(429)
    rate_limited_summary, _ = _fetch_finnhub_symbol_change_coverage(
        rate_limited_session,
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
        premium_enabled=True,
    )
    assert rate_limited_session.request_count == 3
    assert rate_limited_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert rate_limited_summary["reason"] == "http_429"

    server_error_summary, _ = _fetch_finnhub_symbol_change_coverage(
        _StatusFinnhubSession(503),
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
        premium_enabled=True,
    )
    assert server_error_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert server_error_summary["reason"] == "http_503"

    class _TimeoutFinnhubSession:
        def get(self, *_args, **_kwargs):
            raise harvester_module.requests.Timeout("fixture timeout")

    timeout_symbol_summary, timeout_symbol_rows = (
        _fetch_finnhub_symbol_change_coverage(
            _TimeoutFinnhubSession(),
            api_key="fixture-key",
            coverage_start=datetime.date(2021, 7, 20),
            coverage_end=datetime.date(2026, 7, 21),
            retrieved_at="2026-07-21T11:00:00Z",
            previous_coverage={
                "events": {
                    "symbolChanges": [
                        {
                            "oldSymbol": "PRESERVED-OLD",
                            "newSymbol": "PRESERVED",
                            "eventEffectiveAt": "2026-01-02",
                        }
                    ]
                }
            },
            premium_enabled=True,
        )
    )
    assert timeout_symbol_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert timeout_symbol_summary["reason"].startswith("Timeout:")
    assert timeout_symbol_rows == [
        {
            "oldSymbol": "PRESERVED-OLD",
            "newSymbol": "PRESERVED",
            "eventEffectiveAt": "2026-01-02",
        }
    ]

    fmp_summary, fmp_rows = _fetch_fmp_delisting_coverage(
        _FmpSession(),
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert fmp_summary["status"] == "SUCCESS"
    assert fmp_summary["partialResponse"] is False
    assert fmp_summary["sourceAsOfBasis"] == "RETRIEVAL_TIME_NO_VENDOR_TIMESTAMP"
    assert fmp_rows == delisted_rows

    class _MalformedFmpSession:
        def get(self, *_args, **_kwargs):
            return _FixtureResponse(payload=[{"unexpected": True}])

    malformed_fmp_summary, malformed_fmp_rows = _fetch_fmp_delisting_coverage(
        _MalformedFmpSession(),
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert malformed_fmp_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert malformed_fmp_summary["reason"] == "response_schema_invalid"
    assert malformed_fmp_rows == []

    partial_previous = {
        "sources": {
            "delisting": {
                "status": "UNVERIFIED_PARTIAL_RESPONSE",
                "partialResponse": True,
                "coverageStart": "2021-07-20",
                "coverageEnd": "2026-07-20",
            }
        },
        "events": {
            "delistings": [
                {
                    "symbol": "UNVERIFIED-OLD",
                    "eventEffectiveAt": "2022-01-01",
                }
            ]
        },
    }
    restarted_summary, restarted_rows = _fetch_fmp_delisting_coverage(
        _FmpSession(),
        api_key="fixture-key",
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage=partial_previous,
    )
    assert restarted_summary["status"] == "SUCCESS"
    assert restarted_rows == delisted_rows

    class _FullFmpPageSession:
        def get(self, *_args, **_kwargs):
            return _FixtureResponse(
                payload=[
                    {
                        "symbol": f"PART{index}",
                        "delistedDate": "2026-06-30",
                    }
                    for index in range(100)
                ]
            )

    original_page_cap = harvester_module.HARVESTER_FMP_DELISTED_MAX_PAGES
    try:
        harvester_module.HARVESTER_FMP_DELISTED_MAX_PAGES = 1
        partial_fmp_summary, partial_fmp_rows = _fetch_fmp_delisting_coverage(
            _FullFmpPageSession(),
            api_key="fixture-key",
            coverage_start=datetime.date(2021, 7, 20),
            coverage_end=datetime.date(2026, 7, 21),
            retrieved_at="2026-07-21T11:00:00Z",
            previous_coverage={
                "events": {
                    "delistings": [
                        {
                            "symbol": "PRESERVED",
                            "eventEffectiveAt": "2024-01-15",
                        }
                    ]
                }
            },
        )
    finally:
        harvester_module.HARVESTER_FMP_DELISTED_MAX_PAGES = original_page_cap
    assert partial_fmp_summary["status"] == "UNVERIFIED_PARTIAL_RESPONSE"
    assert partial_fmp_summary["partialObservedEventCount"] == 100
    assert partial_fmp_summary["preservedEventCount"] == 1
    assert partial_fmp_rows == [
        {
            "symbol": "PRESERVED",
            "eventEffectiveAt": "2024-01-15",
        }
    ]

    class _NasdaqSession:
        def __init__(self, *, rss_text=None, mutate_history_rows=None):
            self.request_payload = None
            self.rss_text = rss_text or external_fixture["nasdaqCurrentHaltRss"]
            self.mutate_history_rows = mutate_history_rows

        def get(self, url, *_args, **_kwargs):
            if "rss.aspx" in url:
                return _FixtureResponse(text=self.rss_text)
            return _FixtureResponse(text="halt search")

        def post(self, *_args, **kwargs):
            self.request_payload = json.loads(kwargs["data"])
            reason_code = json.loads(self.request_payload["params"])[1]
            matching_rows = [
                row
                for row in harvester_module._parse_html_table_rows(
                    external_fixture["nasdaqHaltHtml"]
                )
                if row.get("Reason Code") == reason_code
            ]
            if self.mutate_history_rows:
                matching_rows = self.mutate_history_rows(
                    reason_code,
                    matching_rows,
                )
            if not matching_rows:
                return _FixtureResponse(
                    payload={
                        "result": "No Data Found",
                        "id": self.request_payload["id"],
                        "version": "1.1",
                    }
                )
            headers = list(matching_rows[0])
            html_rows = "".join(
                "<tr>"
                + "".join(f"<td>{row.get(header, '')}</td>" for header in headers)
                + "</tr>"
                for row in matching_rows
            )
            html = (
                "<table><tr>"
                + "".join(f"<th>{header}</th>" for header in headers)
                + "</tr>"
                + html_rows
                + "</table>"
            )
            return _FixtureResponse(
                payload={
                    "result": html,
                    "id": self.request_payload["id"],
                    "version": "1.1",
                },
            )

    nasdaq_session = _NasdaqSession()
    nasdaq_summary, nasdaq_rows = _fetch_nasdaq_suspension_coverage(
        nasdaq_session,
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert nasdaq_summary["status"] == "SUCCESS"
    assert nasdaq_summary["partialResponse"] is False
    assert nasdaq_summary["sourceAsOfBasis"] == "NASDAQ_RSS_PUBLICATION_TIME"
    assert nasdaq_summary["sourceAsOf"] == "2026-07-21T11:00:00Z"
    assert nasdaq_summary["currentFeedAgeHours"] == 0
    assert [row["symbol"] for row in nasdaq_rows] == ["FUTURE", "HALT", "OPEN"]
    assert nasdaq_summary["coverageStart"] == "2025-07-21"
    assert nasdaq_summary["sourceCoverageLimit"] == "NASDAQ_HALT_SEARCH_LAST_YEAR"
    assert nasdaq_session.request_payload["version"] == "1.1"
    assert nasdaq_summary["requestCount"] == (
        len(harvester_module.NASDAQ_HISTORICAL_SUSPENSION_CODES) + 2
    )
    refreshed_halt_summary, refreshed_halt_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={
            "events": {
                "suspensions": [
                    {
                        "symbol": "HALT",
                        "eventEffectiveAt": "2026-07-17T09:31:00-04:00",
                        "resumedAt": None,
                        "reasonCode": "H10",
                    }
                ]
            }
        },
    )
    assert refreshed_halt_summary["status"] == "SUCCESS"
    refreshed_halt = next(row for row in refreshed_halt_rows if row["symbol"] == "HALT")
    assert refreshed_halt["resumedAt"] == "2026-07-18T09:35:00-04:00"
    current_open = next(row for row in refreshed_halt_rows if row["symbol"] == "OPEN")
    assert current_open["currentFeedObserved"] is True

    stale_rss = external_fixture["nasdaqCurrentHaltRss"].replace(
        "Tue, 21 Jul 2026 11:00:00 GMT",
        "Tue, 21 Jul 2020 11:00:00 GMT",
    )
    stale_rss_summary, stale_rss_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(rss_text=stale_rss),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert stale_rss_summary["status"] == "UNVERIFIED_STALE_SOURCE"
    assert stale_rss_summary["reason"] == "current_feed_published_at_stale"
    assert stale_rss_rows == []

    rth_stale_rss = external_fixture["nasdaqCurrentHaltRss"].replace(
        "Tue, 21 Jul 2026 11:00:00 GMT",
        "Tue, 21 Jul 2026 07:00:00 GMT",
    )
    rth_stale_summary, rth_stale_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(rss_text=rth_stale_rss),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T15:00:00Z",
        previous_coverage={},
    )
    assert rth_stale_summary["status"] == "UNVERIFIED_STALE_SOURCE"
    assert rth_stale_summary["reason"] == "current_feed_published_at_stale"
    assert (
        rth_stale_summary["currentFeedFreshnessMode"]
        == "WEEKDAY_REGULAR_SESSION_WINDOW"
    )
    assert rth_stale_rows == []

    future_rss = external_fixture["nasdaqCurrentHaltRss"].replace(
        "Tue, 21 Jul 2026 11:00:00 GMT",
        "Tue, 21 Jul 2026 12:00:00 GMT",
    )
    future_rss_summary, future_rss_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(rss_text=future_rss),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert future_rss_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert future_rss_summary["reason"] == "current_feed_published_at_future"
    assert future_rss_rows == []

    def invalidate_h10_symbol(reason_code, rows):
        if reason_code != "H10":
            return rows
        return [{**row, "Issue Symbol": ""} for row in rows]

    parse_loss_summary, parse_loss_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(mutate_history_rows=invalidate_h10_symbol),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert parse_loss_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert "halt_table_parse_loss" in parse_loss_summary["reason"]
    assert parse_loss_rows == []

    class _ShortRowNasdaqSession(_NasdaqSession):
        def post(self, *args, **kwargs):
            response = super().post(*args, **kwargs)
            payload = response._payload
            if (
                isinstance(payload, dict)
                and self.request_payload
                and json.loads(self.request_payload["params"])[1] == "H10"
                and payload.get("result") != "No Data Found"
            ):
                payload = dict(payload)
                payload["result"] = str(payload["result"]).replace(
                    "</table>",
                    "<tr><td>07/19/2026</td><td>09:30:00</td></tr></table>",
                )
                return _FixtureResponse(payload=payload)
            return response

    short_row_summary, short_row_rows = _fetch_nasdaq_suspension_coverage(
        _ShortRowNasdaqSession(),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={},
    )
    assert short_row_summary["status"] == "UNVERIFIED_SOURCE_RESPONSE"
    assert "halt_table_row_shape_invalid" in short_row_summary["reason"]
    assert short_row_rows == []

    retained_summary, retained_rows = _fetch_nasdaq_suspension_coverage(
        _NasdaqSession(),
        coverage_start=datetime.date(2021, 7, 20),
        coverage_end=datetime.date(2026, 7, 21),
        retrieved_at="2026-07-21T11:00:00Z",
        previous_coverage={
            "events": {
                "suspensions": [
                    {
                        "symbol": "OLDH",
                        "eventEffectiveAt": "2024-07-17T09:31:00-04:00",
                        "resumedAt": "2024-07-18T09:35:00-04:00",
                        "reasonCode": "H10",
                        "reason": "SEC trading suspension",
                    }
                ]
            }
        },
    )
    assert retained_summary["status"] == "SUCCESS"
    assert retained_summary["preservedHistoricalEventRows"] == 1
    retained_old = next(row for row in retained_rows if row["symbol"] == "OLDH")
    assert (
        retained_old["preservationStatus"]
        == "PRESERVED_POSITIVE_EVENT_OUTSIDE_CURRENT_QUERY_WINDOW"
    )
    assert retained_old["currentFeedObserved"] is False

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

    class _UnexpectedTicker:
        def __init__(self, _symbol: str) -> None:
            raise AssertionError("fresh OHLCV should not be fetched for lineage-only refresh")

    skip_uploads: list[tuple[str, object, str]] = []
    try:
        harvester_module.find_file_id = lambda *_args: "fixture-file-id"
        harvester_module.download_json = lambda *_args: uploads[0][1]
        harvester_module.upload_json = lambda name, payload, parent: skip_uploads.append(
            (name, payload, parent)
        )
        harvester_module.yf.Ticker = _UnexpectedTicker
        harvester_module.get_expected_market_date_str = lambda: "2026-07-20"
        skip_sink: list[dict] = []
        skipped_status = harvester_module.sync_ohlcv_incremental(
            "SYNTH",
            "fixture-folder",
            listing_evidence=failed_refresh_listing,
            lineage_sink=skip_sink,
        )
    finally:
        harvester_module.find_file_id = original_find
        harvester_module.download_json = original_download
        harvester_module.upload_json = original_upload
        harvester_module.yf.Ticker = original_ticker
        harvester_module.get_expected_market_date_str = original_expected
    assert skipped_status == "SKIPPED"
    assert len(skip_uploads) == 1
    assert skip_sink[0]["lineageVerifiedForComparison"] is False
    assert skip_uploads[0][1]["lineage"]["suspensionStatus"].startswith("UNVERIFIED")

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
    original_external_fetch = harvester_module.fetch_external_corporate_action_coverage
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
        harvester_module.fetch_external_corporate_action_coverage = (
            lambda *_args, **_kwargs: coverage
        )
        refreshed_mapping, _ = harvester_module.refresh_ticker_mapping_from_authoritative_sources(
            {"SYNTH": {"group": "S", **fixture["verifiedListingEvidence"]}},
            "2026-07-21",
        )
    finally:
        harvester_module.fetch_authoritative_listing_rows = original_listing_fetch
        harvester_module.fetch_external_corporate_action_coverage = original_external_fetch
    assert (
        refreshed_mapping["SYNTH"]["symbolChangeEvidence"]["status"]
        == "UNVERIFIED_EXTERNAL_SOURCE_REFRESH_FAILED"
    )
    assert refreshed_mapping["SYNTH"]["delistingEvidence"]["status"] == "VERIFIED_NOT_DELISTED_AS_OF_SOURCE"
    assert refreshed_mapping["SYNTH"]["suspensionEvidence"]["status"] == "VERIFIED_NOT_SUSPENDED_AS_OF_SOURCE"

    daily_audit = build_mapping_corporate_action_runtime_audit(
        refreshed_mapping,
        expected_symbols=["SYNTH"],
        external_source_coverage=coverage,
        generated_at=fixture["retrievedAt"],
    )
    assert daily_audit["auditScope"] == "MAPPING_SOURCE_ONLY"
    assert daily_audit["overall"] == "warn_lineage_rejected"
    assert daily_audit["summary"]["missingRows"] == 0
    assert daily_audit["summary"]["duplicateRows"] == 0
    assert daily_audit["sourceTimestamps"]["latestRetrievedAt"]
    assert daily_audit["rows"][0]["lineageStatus"] == "REJECTED_HISTORY_LINEAGE_NOT_EVALUATED"
    assert daily_audit["rows"][0]["lineageVerifiedForComparison"] is False

    empty_audit = build_corporate_action_runtime_audit(
        [],
        trigger_file=None,
        expected_symbols=[],
        generated_at=fixture["retrievedAt"],
    )
    assert empty_audit["overall"] == "warn_coverage_mismatch"
    assert empty_audit["summary"]["emptyScope"] is True
    assert empty_audit["summary"]["lineageCoveragePct"] == 0.0

    blocked_mapping_audit = build_mapping_corporate_action_runtime_audit(
        {
            "SYNTH": {
                "group": "S",
                "lastMappedAt": fixture["retrievedAt"],
                "listingStatus": "ACTIVE",
            }
        },
        expected_symbols=["SYNTH"],
        external_source_coverage={"overall": "blocked_external_source_contract"},
        generated_at=fixture["retrievedAt"],
    )
    blocked_row = blocked_mapping_audit["rows"][0]
    assert blocked_row["mappingEvaluatedAt"] == fixture["retrievedAt"]
    assert blocked_row.get("retrievedAt") is None
    assert blocked_row.get("sourceAsOf") is None

    rejected_dispatch_audit = build_corporate_action_runtime_audit(
        [
            {
                "symbol": "SYNTH",
                "lineageStatus": "REJECTED_VENDOR_MISSING",
                "retrievedAt": fixture["retrievedAt"],
                "sourceAsOf": fixture["verifiedListingEvidence"]["listingSourceAsOf"],
            }
        ],
        trigger_file="STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
        expected_symbols=["SYNTH"],
        generated_at=fixture["retrievedAt"],
    )
    assert rejected_dispatch_audit["sourceTimestamps"]["latestRetrievedAt"] is None
    assert rejected_dispatch_audit["sourceTimestamps"]["latestSourceAsOf"] is None

    renamed = build_corporate_action_lineage(
        frame,
        record_symbol="RENAMED-SYNTH",
        source_symbol="RENAMED-SYNTH",
        requested_period=fixture["period"],
        retrieved_at=fixture["retrievedAt"],
        listing_evidence=_evidence_for_symbol(
            fixture["verifiedListingEvidence"], "RENAMED-SYNTH"
        ),
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
    assert '"CONTRACT_READY_OOS_VERIFIED"' in harvester_source
    assert '"CONTRACT_READY_OOS_BLOCKED"' in harvester_source
    assert '"COVERAGE_MISMATCH"' in harvester_source
    assert '"dispatch_completed_without_primary_lineage_audit"' in harvester_source
    assert '"run_fatal_before_lineage_audit:' in harvester_source

    print("[CORPORATE_ACTION_LINEAGE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
