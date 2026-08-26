from __future__ import annotations

import datetime
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.toss_shadow_market_data import (
    build_toss_shadow_blocked_result,
    collect_toss_shadow_market_data,
    dispatch_toss_shadow_alert,
    toss_shadow_runtime_decision,
)


RATE_HEADERS = {
    "X-RateLimit-Limit": "15",
    "X-RateLimit-Remaining": "14",
    "X-RateLimit-Reset": "0.1",
    "Date": "Wed, 12 Aug 2026 00:00:01 GMT",
}
RETRIEVED_AT = "2026-08-12T00:00:00Z"
CAPABILITY_SHA = "a" * 64
REQUEST_SOURCE_ARTIFACT_BASE = {
    "file": "STAGE3_FUNDAMENTAL_FULL_FIXTURE.json",
    "sha256": "b" * 64,
    "hashBasis": "CANONICAL_JSON",
    "generatedAt": "2026-08-11T23:59:00Z",
}


def _symbol_scope_sha256(symbols: list[str]) -> str:
    normalized = sorted({symbol.strip().upper() for symbol in symbols})
    return hashlib.sha256("\n".join(normalized).encode("utf-8")).hexdigest()


def _request_source_artifact(symbols: list[str]) -> dict[str, Any]:
    return {
        **REQUEST_SOURCE_ARTIFACT_BASE,
        "requestScopeSha256": _symbol_scope_sha256(symbols),
    }


class _Response:
    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload, sort_keys=True).encode("utf-8")

    def json(self) -> Any:
        return self._payload


def _calendar_payload() -> dict[str, Any]:
    def session(start: str, end: str) -> dict[str, str]:
        return {"startTime": start, "endTime": end}

    return {
        "result": {
            "today": {
                "date": "2026-08-11",
                "dayMarket": session(
                    "2026-08-11T09:00:00+09:00", "2026-08-11T16:50:00+09:00"
                ),
                "preMarket": session(
                    "2026-08-11T17:00:00+09:00", "2026-08-11T22:30:00+09:00"
                ),
                "regularMarket": session(
                    "2026-08-11T22:30:00+09:00", "2026-08-12T05:00:00+09:00"
                ),
                "afterMarket": session(
                    "2026-08-12T05:00:00+09:00", "2026-08-12T07:00:00+09:00"
                ),
            },
            "previousBusinessDay": {
                "date": "2026-08-10",
                "dayMarket": None,
                "preMarket": session(
                    "2026-08-10T17:00:00+09:00", "2026-08-10T22:30:00+09:00"
                ),
                "regularMarket": session(
                    "2026-08-10T22:30:00+09:00", "2026-08-11T05:00:00+09:00"
                ),
                "afterMarket": session(
                    "2026-08-11T05:00:00+09:00", "2026-08-11T07:00:00+09:00"
                ),
            },
            "nextBusinessDay": {
                "date": "2026-08-12",
                "dayMarket": None,
                "preMarket": session(
                    "2026-08-12T17:00:00+09:00", "2026-08-12T22:30:00+09:00"
                ),
                "regularMarket": session(
                    "2026-08-12T22:30:00+09:00", "2026-08-13T05:00:00+09:00"
                ),
                "afterMarket": session(
                    "2026-08-13T05:00:00+09:00", "2026-08-13T07:00:00+09:00"
                ),
            },
        }
    }


def _price_row(
    symbol: str,
    *,
    timestamp: str | None = "2026-08-11T22:30:00+09:00",
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "lastPrice": "101.25",
        "currency": "USD",
    }


def _price_row_without_timestamp(symbol: str) -> dict[str, Any]:
    row = _price_row(symbol)
    row.pop("timestamp")
    return row


class _Session:
    def __init__(
        self,
        *,
        token: _Response | Exception | None = None,
        calendar: _Response | Exception | None = None,
        prices: Callable[[list[str], int], _Response | Exception] | None = None,
    ) -> None:
        self.token = token or _Response(
            200,
            {"access_token": "never-persist-this", "token_type": "Bearer", "expires_in": 86400},
            headers=RATE_HEADERS,
        )
        self.calendar = calendar or _Response(200, _calendar_payload(), headers=RATE_HEADERS)
        self.prices = prices or self._success_prices
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.get_calls: list[tuple[str, dict[str, Any]]] = []
        self.price_call_count = 0

    @staticmethod
    def _success_prices(symbols: list[str], _: int) -> _Response:
        return _Response(200, {"result": [_price_row(symbol) for symbol in symbols]}, headers=RATE_HEADERS)

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.post_calls.append((url, kwargs))
        if isinstance(self.token, Exception):
            raise self.token
        return self.token

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.get_calls.append((url, kwargs))
        if url.endswith("/market-calendar/US"):
            if isinstance(self.calendar, Exception):
                raise self.calendar
            return self.calendar
        symbols = str((kwargs.get("params") or {}).get("symbols") or "").split(",")
        self.price_call_count += 1
        response = self.prices(symbols, self.price_call_count)
        if isinstance(response, Exception):
            raise response
        return response


def _collect(session: _Session, symbols: list[str] | None = None) -> dict[str, Any]:
    requested_symbols = symbols or ["SYNTH"]
    return collect_toss_shadow_market_data(
        session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=requested_symbols,
        retrieved_at=RETRIEVED_AT,
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        request_source_artifact=_request_source_artifact(requested_symbols),
        max_price_requests=2,
        clock=lambda: datetime.datetime.fromisoformat(
            "2026-08-12T00:00:01+00:00"
        ),
        monotonic_clock=lambda: 1.0,
    )


def _aggregate(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result["status"],
        "requested": result["summary"]["requestedRows"],
        "matched": result["summary"]["matchedRows"],
        "missing": result["summary"]["missingRows"],
        "eligible": result["eligible"],
        "canonicalChanged": result["canonicalSourceChanged"],
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    harvester_source = (root / "harvester.py").read_text(encoding="utf-8")
    workflow_source = (root / ".github/workflows/main.yml").read_text(encoding="utf-8")
    shadow_source = (root / "scripts/toss_shadow_market_data.py").read_text(encoding="utf-8")
    readme_source = (root / "README.md").read_text(encoding="utf-8")
    policy_review_source = (root / "TOSS_PRICE_TIMESTAMP_POLICY_REVIEW.md").read_text(
        encoding="utf-8"
    )
    assert "collect_toss_shadow_market_data" in harvester_source
    assert "dispatch_toss_shadow_alert" in harvester_source
    assert "toss_shadow_runtime_decision" in harvester_source
    assert "TOSS_MARKET_DATA_SHADOW.json" in harvester_source
    assert "ensure_toss_shadow_market_data" in harvester_source
    assert "load_stage3_shadow_handoff_scope" in harvester_source
    assert "request_source_artifact" in harvester_source
    assert "TOSS_SHADOW_PROVIDER_ENABLED: 'false'" in workflow_source
    assert "TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED: 'false'" in workflow_source
    assert "state/toss-market-data-shadow.json" in workflow_source
    assert "X-Tossinvest-Account" not in shadow_source
    assert "/api/v1/orders" not in shadow_source
    assert "HTTP Date is diagnostic-only" in readme_source
    assert "MAC_CLOCK_SYNC_UNVERIFIED" in readme_source
    assert "No timestamp tolerance is authorized" in readme_source
    assert "HTTP 2xx timestamp omissions never use auth or egress guidance" in (
        readme_source
    )
    assert "TOSS_NULLABLE_VALID_SLICE_STATIC_READY" in (
        policy_review_source
    )
    assert "No policy migration is approved here" in policy_review_source

    capability_blocked = build_toss_shadow_blocked_result(
        status="TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED",
        safe_error_category="phase1_capability_not_pass",
        capability_artifact_sha256=CAPABILITY_SHA,
    )
    assert capability_blocked["requestCounts"] == {
        "oauth": 0,
        "marketCalendar": 0,
        "prices": 0,
    }
    assert capability_blocked["eligible"] is False
    assert capability_blocked["analysisContinued"] is True

    invalid_source_session = _Session()
    invalid_source = collect_toss_shadow_market_data(
        invalid_source_session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=["SYNTH"],
        retrieved_at=RETRIEVED_AT,
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        request_source_artifact={"file": "missing-lineage.json"},
        max_price_requests=2,
    )
    assert invalid_source["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert invalid_source["safeErrorCategory"] == (
        "request_source_lineage_invalid"
    )
    assert invalid_source["requestCounts"] == {
        "oauth": 0,
        "marketCalendar": 0,
        "prices": 0,
    }
    assert invalid_source_session.post_calls == []
    assert invalid_source_session.get_calls == []

    mismatched_scope_session = _Session()
    mismatched_scope = collect_toss_shadow_market_data(
        mismatched_scope_session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=["SYNTH"],
        retrieved_at=RETRIEVED_AT,
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        request_source_artifact=_request_source_artifact(["RENAMED"]),
        max_price_requests=2,
    )
    assert mismatched_scope["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert mismatched_scope["safeErrorCategory"] == (
        "request_scope_hash_mismatch"
    )
    assert mismatched_scope["requestCounts"] == {
        "oauth": 0,
        "marketCalendar": 0,
        "prices": 0,
    }
    assert mismatched_scope_session.post_calls == []
    assert mismatched_scope_session.get_calls == []

    symbols = [f"S{index:03d}" for index in range(300)]
    success_session = _Session()
    success = _collect(success_session, symbols)
    assert success["schemaVersion"] == "toss-market-data-shadow-v1"
    assert success["status"] == "TOSS_SHADOW_PASS"
    assert success["mode"] == "SHADOW_ONLY"
    request_lineage = success["requestLineage"]
    assert request_lineage["status"] == "VERIFIED_STAGE3_REQUEST_SCOPE"
    assert request_lineage["requestSourceArtifact"] == _request_source_artifact(
        symbols
    )
    assert request_lineage["requestScopeSha256"] == _symbol_scope_sha256(symbols)
    assert [row["requestedCount"] for row in request_lineage["batches"]] == [
        200,
        100,
    ]
    assert [row["returnedCount"] for row in request_lineage["batches"]] == [
        200,
        100,
    ]
    assert all(row["missingSymbolSha256"] == [] for row in request_lineage["batches"])
    assert all(
        len(row["batchRequestScopeSha256"]) == 64
        and len(row["batchReturnedScopeSha256"]) == 64
        for row in request_lineage["batches"]
    )
    assert success["provider"] == "TOSS_OPEN_API"
    assert success["adjustedPriceSemantics"] == "NOT_APPLICABLE_TO_PRICES_ENDPOINT"
    assert success["eligible"] is True
    assert success["analysisContinued"] is True
    assert success["canonicalSourceChanged"] is False
    assert success["policyImpact"] == "NONE_REPORT_ONLY"
    assert success["requestCounts"] == {"oauth": 1, "marketCalendar": 1, "prices": 2}
    assert success["httpStatusCategories"] == {
        "oauth": "2xx",
        "marketCalendar": "2xx",
        "prices": ["2xx", "2xx"],
    }
    assert success["summary"] == {
        "requestedRows": 300,
        "matchedRows": 300,
        "missingRows": 0,
        "invalidRows": 0,
        "duplicateRows": 0,
        "responseSha256Rows": 3,
    }
    assert success["calendar"]["schemaValid"] is True
    assert success["calendar"]["marketTimezone"] == "Asia/Seoul"
    assert success["rateLimitHeadersComplete"] is True
    assert success["accountHeaderUsed"] is False
    assert success["orderEndpointUsed"] is False
    clock_evidence = success["clockDomainEvidence"]
    assert clock_evidence["httpDatePrecision"] == "SECONDS"
    assert clock_evidence["summary"]["responseBatchCount"] == 4
    assert clock_evidence["summary"]["httpDatePresentResponses"] == 4
    assert clock_evidence["summary"]["httpDateValidResponses"] == 4
    assert clock_evidence["summary"]["unknownOrUnclassifiedRows"] == 0
    assert clock_evidence["summary"]["rootCauseCountMatches"] is True
    assert clock_evidence["summary"]["clockReferenceStatus"] == (
        "HTTP_DATE_REFERENCE_VALID"
    )
    first_price_clock = clock_evidence["responses"]["prices"][0]
    assert first_price_clock["requestStartedAt"] == "2026-08-12T00:00:01Z"
    assert first_price_clock["localResponseReceivedAt"] == (
        "2026-08-12T00:00:01Z"
    )
    assert first_price_clock["httpDateHeaderPresent"] is True
    assert first_price_clock["httpDateParseStatus"] == "VALID"
    assert first_price_clock["parsedHttpDateAt"] == "2026-08-12T00:00:01Z"
    assert first_price_clock["requestDurationMs"] >= 0
    assert len(success["prices"]) == 300
    success_timestamp_diagnostics = success["diagnostics"]
    assert success_timestamp_diagnostics["timestampFieldPresentRows"] == 300
    assert success_timestamp_diagnostics["timestampFieldAbsentRows"] == 0
    assert success_timestamp_diagnostics["timestampNullRows"] == 0
    assert success_timestamp_diagnostics["timestampBlankRows"] == 0
    assert success_timestamp_diagnostics["timestampParseableRows"] == 300
    assert success_timestamp_diagnostics["timestampUnparseableRows"] == 0
    assert success_timestamp_diagnostics["timestampCategoryCounts"] == {
        "BLANK": 0,
        "NULL": 0,
        "OPTIONAL_ABSENT": 0,
        "PRESENT_VALID": 300,
        "UNPARSEABLE": 0,
    }
    assert sum(
        success_timestamp_diagnostics["timestampCategoryCounts"].values()
    ) == sum(success_timestamp_diagnostics["returnedRowsByBatch"])
    assert success_timestamp_diagnostics["timestampTypeCounts"] == {
        "string": 300
    }
    assert success_timestamp_diagnostics["returnedRowsByBatch"] == [200, 100]
    assert success_timestamp_diagnostics["missingTimestampRowsByBatch"] == [
        0,
        0,
    ]
    assert success_timestamp_diagnostics["validTimestampRowsByBatch"] == [
        200,
        100,
    ]
    assert success_timestamp_diagnostics["timestampDiagnosticStatus"] == (
        "COMPLETE"
    )
    assert success_timestamp_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "TIMESTAMP_PRESENT_VALID"
    )
    assert success_timestamp_diagnostics["timestampDiagnosticCountMatches"] is True
    assert success_timestamp_diagnostics["timestampUnknownOrUnclassifiedRows"] == 0
    assert success_timestamp_diagnostics["timestampSliceCounts"] == {
        "TOSS_TIMESTAMP_VALID_REPORT_ONLY": 300,
        "TOSS_TIMESTAMP_DOCUMENTED_NULL_EXCLUDED": 0,
        "TOSS_TIMESTAMP_OPTIONAL_ABSENT_EXCLUDED": 0,
        "TOSS_TIMESTAMP_BLANK_EXCLUDED": 0,
        "TOSS_TIMESTAMP_UNPARSEABLE_EXCLUDED": 0,
        "TOSS_TIMESTAMP_LOCAL_CLOCK_REFERENCE_EXCLUDED": 0,
        "TOSS_TIMESTAMP_PROVIDER_CLOCK_VIOLATION_EXCLUDED": 0,
    }
    assert success_timestamp_diagnostics["timestampSliceRows"] == 300
    assert success_timestamp_diagnostics["timestampSliceCountMatches"] is True
    assert success_timestamp_diagnostics[
        "timestampSliceUnknownOrUnclassifiedRows"
    ] == 0
    assert sum(
        success_timestamp_diagnostics["responseShapeFingerprintCounts"].values()
    ) == 300
    assert all(
        len(fingerprint) == 64
        for fingerprint in success_timestamp_diagnostics[
            "responseShapeFingerprintCounts"
        ]
    )
    assert len(success_session.post_calls) == 1
    assert len(success_session.get_calls) == 3
    assert all(
        "X-Tossinvest-Account" not in (kwargs.get("headers") or {})
        for _, kwargs in success_session.post_calls + success_session.get_calls
    )
    assert all("/orders" not in url and "/accounts" not in url for url, _ in success_session.get_calls)
    serialized = json.dumps(success, sort_keys=True)
    assert "never-persist-this" not in serialized
    assert "client-secret" not in serialized
    assert "Wed, 12 Aug 2026" not in serialized

    nullable_slice_symbols = [f"N{index:03d}" for index in range(300)]

    def nullable_slice_prices(symbols: list[str], _: int) -> _Response:
        rows = []
        for symbol in symbols:
            index = int(symbol[1:])
            if index < 43:
                rows.append(_price_row(symbol, timestamp=None))
            elif index < 49:
                rows.append(_price_row(symbol, timestamp="2026-08-12T00:00:02Z"))
            else:
                rows.append(_price_row(symbol))
        return _Response(
            200,
            {"result": rows},
            headers={
                **RATE_HEADERS,
                "Date": "Wed, 12 Aug 2026 00:00:03 GMT",
            },
        )

    nullable_slice = _collect(
        _Session(prices=nullable_slice_prices), nullable_slice_symbols
    )
    nullable_slice_diagnostics = nullable_slice["diagnostics"]
    assert nullable_slice["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert nullable_slice["safeErrorCategory"] == "price_timestamp_missing"
    assert nullable_slice["summary"]["requestedRows"] == 300
    assert nullable_slice["summary"]["matchedRows"] == 251
    assert nullable_slice["summary"]["missingRows"] == 49
    assert nullable_slice_diagnostics["timestampCategoryCounts"] == {
        "BLANK": 0,
        "NULL": 43,
        "OPTIONAL_ABSENT": 0,
        "PRESENT_VALID": 257,
        "UNPARSEABLE": 0,
    }
    assert nullable_slice_diagnostics["futureTimestampRows"] == 6
    assert nullable_slice_diagnostics["timestampSliceCounts"] == {
        "TOSS_TIMESTAMP_VALID_REPORT_ONLY": 251,
        "TOSS_TIMESTAMP_DOCUMENTED_NULL_EXCLUDED": 43,
        "TOSS_TIMESTAMP_OPTIONAL_ABSENT_EXCLUDED": 0,
        "TOSS_TIMESTAMP_BLANK_EXCLUDED": 0,
        "TOSS_TIMESTAMP_UNPARSEABLE_EXCLUDED": 0,
        "TOSS_TIMESTAMP_LOCAL_CLOCK_REFERENCE_EXCLUDED": 6,
        "TOSS_TIMESTAMP_PROVIDER_CLOCK_VIOLATION_EXCLUDED": 0,
    }
    assert nullable_slice_diagnostics["timestampSliceRows"] == 300
    assert nullable_slice_diagnostics["timestampSliceCountMatches"] is True
    assert nullable_slice_diagnostics[
        "timestampSliceUnknownOrUnclassifiedRows"
    ] == 0
    assert nullable_slice_diagnostics["validReportOnlyRows"] == 251
    assert nullable_slice_diagnostics["documentedNullableExcludedRows"] == 43
    assert nullable_slice_diagnostics["localClockReferenceExcludedRows"] == 6
    assert nullable_slice_diagnostics["timestampFallbackUsed"] is False
    assert nullable_slice_diagnostics["clockToleranceApplied"] is False
    assert nullable_slice_diagnostics["macClockSyncStatus"] == (
        "MAC_CLOCK_SYNC_UNVERIFIED"
    )
    assert nullable_slice["eligible"] is False
    assert nullable_slice["tossEvidenceExcluded"] is True
    assert nullable_slice["canonicalSourceChanged"] is False
    assert nullable_slice["policyImpact"] == "NONE_REPORT_ONLY"
    assert nullable_slice["sourceAsOf"] is None
    assert nullable_slice["prices"] == []
    nullable_slice_serialized = json.dumps(nullable_slice, sort_keys=True)
    assert all(symbol not in nullable_slice_serialized for symbol in nullable_slice_symbols)

    provider_format = _collect(_Session(), ["CLASS.A", "CLASS-B"])
    assert provider_format["status"] == "TOSS_SHADOW_PASS"
    assert provider_format["summary"]["requestedRows"] == 2
    assert provider_format["requestLineage"]["requestScopeSha256"] == (
        _symbol_scope_sha256(["CLASS.A", "CLASS-B"])
    )

    normalized_duplicate = _collect(_Session(), ["synth", "SYNTH"])
    assert normalized_duplicate["status"] == "TOSS_SHADOW_PASS"
    assert normalized_duplicate["summary"]["requestedRows"] == 1
    assert normalized_duplicate["requestLineage"]["requestScopeSha256"] == (
        _symbol_scope_sha256(["SYNTH"])
    )

    blocked = _collect(
        _Session(token=_Response(403, {"error": "access_denied"}, headers=RATE_HEADERS))
    )
    assert blocked["status"] == "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED"
    assert blocked["safeErrorCategory"] == "oauth_http_403"
    assert blocked["requestCounts"] == {"oauth": 1, "marketCalendar": 0, "prices": 0}
    assert blocked["eligible"] is False
    assert blocked["analysisContinued"] is True
    assert blocked["circuitBreaker"] == "OPEN_FOR_RUN"
    assert blocked["clockDomainEvidence"]["summary"]["responseBatchCount"] == 1
    assert blocked["clockDomainEvidence"]["summary"][
        "httpDateValidResponses"
    ] == 1

    unauthorized = _collect(
        _Session(token=_Response(401, {"error": "invalid_client"}, headers=RATE_HEADERS))
    )
    assert unauthorized["status"] == "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED"
    assert unauthorized["safeErrorCategory"] == "oauth_http_401"

    limited = _collect(
        _Session(
            calendar=_Response(
                429,
                {"error": {"code": "rate-limit-exceeded"}},
                headers={**RATE_HEADERS, "Retry-After": "2"},
            )
        )
    )
    assert limited["status"] == "TOSS_SHADOW_RATE_LIMITED"
    assert limited["requestCounts"] == {"oauth": 1, "marketCalendar": 1, "prices": 0}
    assert limited["rateLimitHeaders"]["marketCalendar"]["retryAfter"] == "2"

    transient = _collect(_Session(token=TimeoutError("raw transient detail")))
    assert transient["status"] == "TOSS_SHADOW_TRANSIENT_FAILURE"
    assert transient["safeErrorCategory"] == "TimeoutError"
    assert "raw transient detail" not in json.dumps(transient)

    server_error = _collect(
        _Session(calendar=_Response(503, {"error": {"code": "unavailable"}}, headers=RATE_HEADERS))
    )
    assert server_error["status"] == "TOSS_SHADOW_TRANSIENT_FAILURE"
    assert server_error["requestCounts"] == {"oauth": 1, "marketCalendar": 1, "prices": 0}

    bad_calendar = _calendar_payload()
    bad_calendar["result"]["today"]["date"] = "2026-08-09"
    invalid_calendar = _collect(
        _Session(calendar=_Response(200, bad_calendar, headers=RATE_HEADERS))
    )
    assert invalid_calendar["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert invalid_calendar["requestCounts"] == {
        "oauth": 1,
        "marketCalendar": 1,
        "prices": 0,
    }

    invalid = _collect(
        _Session(prices=lambda symbols, _: _Response(200, {"result": [{"symbol": symbols[0]}]}, headers=RATE_HEADERS))
    )
    assert invalid["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert invalid["prices"] == []

    future = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {"result": [_price_row(symbol, timestamp="2026-08-12T00:00:02Z") for symbol in symbols]},
                headers=RATE_HEADERS,
            )
        )
    )
    assert future["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert future["safeErrorCategory"] == "price_timestamp_after_response"
    assert {
        key: future["diagnostics"][key]
        for key in (
            "timestampMissingRows",
            "futureTimestampRows",
            "outOfCalendarDateRows",
            "unreturnedSymbolRows",
            "staleOrFutureRows",
            "currencyConflictRows",
            "minFutureSkewMs",
            "maxFutureSkewMs",
        )
    } == {
        "timestampMissingRows": 0,
        "futureTimestampRows": 1,
        "outOfCalendarDateRows": 0,
        "unreturnedSymbolRows": 0,
        "staleOrFutureRows": 1,
        "currencyConflictRows": 0,
        "minFutureSkewMs": 1000,
        "maxFutureSkewMs": 1000,
    }
    assert future["clockDomainEvidence"]["summary"]["primaryRootCause"] == (
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
    )
    assert future["clockDomainEvidence"]["summary"]["rootCauseCounts"] == {
        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE": 0,
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES": 1,
        "HTTP_DATE_REFERENCE_MISSING_OR_INVALID": 0,
        "PAYLOAD_TIMESTAMP_MISSING": 0,
        "PARTIAL_SYMBOL_RESPONSE": 0,
        "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT": 0,
    }
    assert future["prices"] == []
    assert future["diagnostics"]["timestampSliceCounts"][
        "TOSS_TIMESTAMP_PROVIDER_CLOCK_VIOLATION_EXCLUDED"
    ] == 1

    local_clock_behind = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp="2026-08-12T00:00:02Z")
                        for symbol in symbols
                    ]
                },
                headers={
                    **RATE_HEADERS,
                    "Date": "Wed, 12 Aug 2026 00:00:03 GMT",
                },
            )
        )
    )
    local_clock_summary = local_clock_behind["clockDomainEvidence"]["summary"]
    assert local_clock_behind["diagnostics"]["timestampSliceCounts"][
        "TOSS_TIMESTAMP_LOCAL_CLOCK_REFERENCE_EXCLUDED"
    ] == 1
    assert local_clock_summary["clockReferenceStatus"] == (
        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"
    )
    assert local_clock_summary["primaryRootCause"] == (
        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"
    )
    assert local_clock_summary["rootCauseCounts"][
        "LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE"
    ] == 1
    assert local_clock_summary["payloadAfterLocalReceiptRows"] == 1
    assert local_clock_summary["payloadAfterHttpDateRows"] == 0
    assert local_clock_summary["payloadToLocalReceiptMinOffsetMs"] == 1000
    assert local_clock_summary["payloadToHttpDateMaxOffsetMs"] == -1000
    assert local_clock_summary["unknownOrUnclassifiedRows"] == 0
    assert local_clock_behind["eligible"] is False
    assert local_clock_behind["sourceAsOf"] is None

    future_without_http_date = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp="2026-08-12T00:00:02Z")
                        for symbol in symbols
                    ]
                },
                headers={
                    key: value for key, value in RATE_HEADERS.items() if key != "Date"
                },
            )
        )
    )
    missing_http_summary = future_without_http_date["clockDomainEvidence"][
        "summary"
    ]
    assert missing_http_summary["primaryRootCause"] == (
        "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"
    )
    assert missing_http_summary["rootCauseCounts"][
        "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"
    ] == 1
    assert missing_http_summary["payloadComparedToHttpDateRows"] == 0
    assert missing_http_summary["unknownOrUnclassifiedRows"] == 0

    future_with_invalid_http_date = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp="2026-08-12T00:00:02Z")
                        for symbol in symbols
                    ]
                },
                headers={**RATE_HEADERS, "Date": "not-an-http-date"},
            )
        )
    )
    invalid_http_price_clock = future_with_invalid_http_date[
        "clockDomainEvidence"
    ]["responses"]["prices"][0]
    assert invalid_http_price_clock["httpDateHeaderPresent"] is True
    assert invalid_http_price_clock["httpDateParseStatus"] == "INVALID"
    assert invalid_http_price_clock["parsedHttpDateAt"] is None
    assert future_with_invalid_http_date["clockDomainEvidence"]["summary"][
        "primaryRootCause"
    ] == "HTTP_DATE_REFERENCE_MISSING_OR_INVALID"

    precision_boundary = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(
                            symbol,
                            timestamp="2026-08-12T00:00:02.500000Z",
                        )
                        for symbol in symbols
                    ]
                },
                headers={
                    **RATE_HEADERS,
                    "Date": "Wed, 12 Aug 2026 00:00:02 GMT",
                },
            )
        )
    )
    precision_summary = precision_boundary["clockDomainEvidence"]["summary"]
    assert precision_summary["primaryRootCause"] == (
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
    )
    assert precision_summary["payloadToHttpDateMinOffsetMs"] == 500
    assert precision_boundary["eligible"] is False

    missing_timestamp = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {"result": [_price_row(symbol, timestamp=None) for symbol in symbols]},
                headers=RATE_HEADERS,
            )
        )
    )
    assert missing_timestamp["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert missing_timestamp["safeErrorCategory"] == "price_timestamp_missing"
    assert missing_timestamp["diagnostics"]["timestampMissingRows"] == 1
    assert missing_timestamp["diagnostics"]["futureTimestampRows"] == 0
    assert missing_timestamp["diagnostics"]["outOfCalendarDateRows"] == 0
    assert missing_timestamp["diagnostics"]["unreturnedSymbolRows"] == 0
    assert missing_timestamp["clockDomainEvidence"]["summary"][
        "primaryRootCause"
    ] == "PAYLOAD_TIMESTAMP_MISSING"
    assert missing_timestamp["clockDomainEvidence"]["summary"][
        "unknownOrUnclassifiedRows"
    ] == 0
    missing_timestamp_diagnostics = missing_timestamp["diagnostics"]
    assert missing_timestamp_diagnostics["timestampFieldPresentRows"] == 1
    assert missing_timestamp_diagnostics["timestampFieldAbsentRows"] == 0
    assert missing_timestamp_diagnostics["timestampNullRows"] == 1
    assert missing_timestamp_diagnostics["timestampBlankRows"] == 0
    assert missing_timestamp_diagnostics["timestampParseableRows"] == 0
    assert missing_timestamp_diagnostics["timestampUnparseableRows"] == 0
    assert missing_timestamp_diagnostics["timestampCategoryCounts"] == {
        "BLANK": 0,
        "NULL": 1,
        "OPTIONAL_ABSENT": 0,
        "PRESENT_VALID": 0,
        "UNPARSEABLE": 0,
    }
    assert missing_timestamp_diagnostics["timestampTypeCounts"] == {"null": 1}
    assert missing_timestamp_diagnostics["returnedRowsByBatch"] == [1]
    assert missing_timestamp_diagnostics["missingTimestampRowsByBatch"] == [1]
    assert missing_timestamp_diagnostics["validTimestampRowsByBatch"] == [0]
    assert missing_timestamp_diagnostics[
        "lastPricePresentWithoutTimestampRows"
    ] == 1
    assert missing_timestamp_diagnostics[
        "currencyPresentWithoutTimestampRows"
    ] == 1
    assert missing_timestamp_diagnostics["timestampDiagnosticStatus"] == (
        "CLASSIFIED"
    )
    assert missing_timestamp_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "DOCUMENTED_NULLABLE_TIMESTAMP"
    )
    assert missing_timestamp_diagnostics["timestampDiagnosticCountMatches"] is True
    assert missing_timestamp_diagnostics["timestampUnknownOrUnclassifiedRows"] == 0
    assert missing_timestamp_diagnostics["timestampSliceCounts"][
        "TOSS_TIMESTAMP_DOCUMENTED_NULL_EXCLUDED"
    ] == 1
    assert missing_timestamp["sourceAsOf"] is None
    assert missing_timestamp["eligible"] is False

    timestamp_absent = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row_without_timestamp(symbol) for symbol in symbols
                    ]
                },
                headers=RATE_HEADERS,
            )
        )
    )
    absent_diagnostics = timestamp_absent["diagnostics"]
    assert timestamp_absent["safeErrorCategory"] == "price_timestamp_missing"
    assert absent_diagnostics["timestampFieldPresentRows"] == 0
    assert absent_diagnostics["timestampFieldAbsentRows"] == 1
    assert absent_diagnostics["timestampTypeCounts"] == {"absent": 1}
    assert absent_diagnostics["timestampCategoryCounts"] == {
        "BLANK": 0,
        "NULL": 0,
        "OPTIONAL_ABSENT": 1,
        "PRESENT_VALID": 0,
        "UNPARSEABLE": 0,
    }
    assert absent_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "TIMESTAMP_KEY_ABSENT"
    )
    assert absent_diagnostics["timestampSliceCounts"][
        "TOSS_TIMESTAMP_OPTIONAL_ABSENT_EXCLUDED"
    ] == 1

    timestamp_blank = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp=" ") for symbol in symbols
                    ]
                },
                headers=RATE_HEADERS,
            )
        )
    )
    blank_diagnostics = timestamp_blank["diagnostics"]
    assert blank_diagnostics["timestampBlankRows"] == 1
    assert blank_diagnostics["timestampUnparseableRows"] == 0
    assert blank_diagnostics["timestampCategoryCounts"]["BLANK"] == 1
    assert blank_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "TIMESTAMP_NULL_OR_BLANK"
    )
    assert blank_diagnostics["timestampSliceCounts"][
        "TOSS_TIMESTAMP_BLANK_EXCLUDED"
    ] == 1

    timestamp_unparseable = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp="not-a-timestamp")
                        for symbol in symbols
                    ]
                },
                headers=RATE_HEADERS,
            )
        )
    )
    unparseable_diagnostics = timestamp_unparseable["diagnostics"]
    assert unparseable_diagnostics["timestampUnparseableRows"] == 1
    assert unparseable_diagnostics["timestampCategoryCounts"]["UNPARSEABLE"] == 1
    assert unparseable_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "TIMESTAMP_FORMAT_UNPARSEABLE"
    )
    assert unparseable_diagnostics["timestampSliceCounts"][
        "TOSS_TIMESTAMP_UNPARSEABLE_EXCLUDED"
    ] == 1

    def mixed_timestamp_shapes(symbols: list[str], _: int) -> _Response:
        rows = [_price_row(symbol) for symbol in symbols]
        rows[0].pop("timestamp")
        rows[1]["timestamp"] = None
        rows[2]["timestamp"] = " "
        rows[3]["timestamp"] = "not-a-timestamp"
        return _Response(200, {"result": rows}, headers=RATE_HEADERS)

    mixed_timestamp_result = _collect(
        _Session(prices=mixed_timestamp_shapes),
        [f"S{index:03d}" for index in range(300)],
    )
    mixed_timestamp_diagnostics = mixed_timestamp_result["diagnostics"]
    assert mixed_timestamp_result["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert mixed_timestamp_result["summary"]["requestedRows"] == 300
    assert mixed_timestamp_result["summary"]["matchedRows"] == 292
    assert mixed_timestamp_result["summary"]["missingRows"] == 8
    assert mixed_timestamp_diagnostics["timestampMissingRows"] == 8
    assert mixed_timestamp_diagnostics["timestampFieldPresentRows"] == 298
    assert mixed_timestamp_diagnostics["timestampFieldAbsentRows"] == 2
    assert mixed_timestamp_diagnostics["timestampNullRows"] == 2
    assert mixed_timestamp_diagnostics["timestampBlankRows"] == 2
    assert mixed_timestamp_diagnostics["timestampParseableRows"] == 292
    assert mixed_timestamp_diagnostics["timestampUnparseableRows"] == 2
    assert mixed_timestamp_diagnostics["timestampCategoryCounts"] == {
        "BLANK": 2,
        "NULL": 2,
        "OPTIONAL_ABSENT": 2,
        "PRESENT_VALID": 292,
        "UNPARSEABLE": 2,
    }
    assert mixed_timestamp_diagnostics["timestampTypeCounts"] == {
        "absent": 2,
        "null": 2,
        "string": 296,
    }
    assert mixed_timestamp_diagnostics["returnedRowsByBatch"] == [200, 100]
    assert mixed_timestamp_diagnostics["missingTimestampRowsByBatch"] == [4, 4]
    assert mixed_timestamp_diagnostics["validTimestampRowsByBatch"] == [196, 96]
    assert mixed_timestamp_diagnostics["timestampDiagnosticPrimaryCause"] == (
        "MIXED_RESPONSE_SCHEMA_VARIANT"
    )
    assert mixed_timestamp_diagnostics["timestampDiagnosticCountMatches"] is True
    assert mixed_timestamp_diagnostics["timestampUnknownOrUnclassifiedRows"] == 0
    assert sum(
        mixed_timestamp_diagnostics["responseShapeFingerprintCounts"].values()
    ) == 300
    mixed_serialized = json.dumps(mixed_timestamp_diagnostics, sort_keys=True)
    assert all(symbol not in mixed_serialized for symbol in ["S000", "S299"])

    outside_calendar = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(symbol, timestamp="2026-08-09T22:30:00+09:00")
                        for symbol in symbols
                    ]
                },
                headers=RATE_HEADERS,
            )
        )
    )
    assert outside_calendar["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert outside_calendar["safeErrorCategory"] == "price_timestamp_outside_calendar"
    assert outside_calendar["diagnostics"]["timestampMissingRows"] == 0
    assert outside_calendar["diagnostics"]["futureTimestampRows"] == 0
    assert outside_calendar["diagnostics"]["timestampSliceCounts"][
        "TOSS_TIMESTAMP_PROVIDER_CLOCK_VIOLATION_EXCLUDED"
    ] == 1
    assert outside_calendar["diagnostics"]["outOfCalendarDateRows"] == 1
    assert outside_calendar["diagnostics"]["unreturnedSymbolRows"] == 0
    assert outside_calendar["clockDomainEvidence"]["summary"][
        "primaryRootCause"
    ] == "CLOCK_DOMAIN_EVIDENCE_INSUFFICIENT"
    assert outside_calendar["clockDomainEvidence"]["summary"][
        "unknownOrUnclassifiedRows"
    ] == 0

    partial = _collect(
        _Session(prices=lambda symbols, _: _Response(200, {"result": [_price_row(symbols[0])]}, headers=RATE_HEADERS)),
        ["SYNTH", "RENAMED"],
    )
    assert partial["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert partial["safeErrorCategory"] == "partial_symbol_response"
    assert partial["summary"]["missingRows"] == 1
    assert partial["diagnostics"]["unreturnedSymbolRows"] == 1
    assert partial["diagnostics"]["timestampMissingRows"] == 0
    assert partial["diagnostics"]["futureTimestampRows"] == 0
    assert partial["diagnostics"]["outOfCalendarDateRows"] == 0
    assert partial["clockDomainEvidence"]["summary"]["primaryRootCause"] == (
        "PARTIAL_SYMBOL_RESPONSE"
    )
    assert partial["clockDomainEvidence"]["summary"]["rootCauseCounts"][
        "PARTIAL_SYMBOL_RESPONSE"
    ] == 1
    assert partial["clockDomainEvidence"]["summary"][
        "unknownOrUnclassifiedRows"
    ] == 0
    assert partial["prices"] == []
    partial_lineage = partial["requestLineage"]
    assert partial_lineage["status"] == "VERIFIED_STAGE3_REQUEST_SCOPE"
    assert partial_lineage["requestSourceArtifact"] == _request_source_artifact(
        ["SYNTH", "RENAMED"]
    )
    assert partial_lineage["requestScopeSha256"] == _symbol_scope_sha256(
        ["SYNTH", "RENAMED"]
    )
    assert len(partial_lineage["batches"]) == 1
    assert partial_lineage["batches"][0]["requestedCount"] == 2
    assert partial_lineage["batches"][0]["returnedCount"] == 1
    assert partial_lineage["batches"][0]["missingSymbolSha256"] == [
        hashlib.sha256(b"SYNTH").hexdigest()
    ]
    assert partial_lineage["batches"][0]["batchRequestScopeSha256"] == (
        _symbol_scope_sha256(["SYNTH", "RENAMED"])
    )
    assert partial_lineage["batches"][0]["batchReturnedScopeSha256"] == (
        _symbol_scope_sha256(["RENAMED"])
    )
    assert "SYNTH" not in json.dumps(partial_lineage, sort_keys=True)
    assert "RENAMED" not in json.dumps(partial_lineage, sort_keys=True)

    alias_session = _Session(
        prices=lambda symbols, _: _Response(
            200,
            {
                "result": [
                    _price_row(symbols[0].replace("-", "."))
                ]
            },
            headers=RATE_HEADERS,
        )
    )
    alias_result = _collect(alias_session, ["CLASS-A"])
    assert alias_result["status"] == "TOSS_SHADOW_PASS"
    assert alias_result["summary"]["matchedRows"] == 1
    assert alias_result["summary"]["missingRows"] == 0
    assert alias_result["prices"][0]["symbol"] == "CLASS-A"
    assert alias_session.get_calls[-1][1]["params"]["symbols"] == "CLASS.A"
    alias_lineage = alias_result["requestLineage"]
    assert alias_lineage["providerSymbolMappingStatus"] == (
        "VERIFIED_DOT_HYPHEN_ALIAS"
    )
    assert alias_lineage["providerMappedRows"] == 1
    assert alias_lineage["providerRequestScopeSha256"] == (
        _symbol_scope_sha256(["CLASS.A"])
    )
    assert alias_lineage["batches"][0]["batchRequestScopeSha256"] == (
        _symbol_scope_sha256(["CLASS-A"])
    )
    assert alias_lineage["batches"][0]["batchProviderRequestScopeSha256"] == (
        _symbol_scope_sha256(["CLASS.A"])
    )
    assert alias_lineage["batches"][0]["batchReturnedScopeSha256"] == (
        _symbol_scope_sha256(["CLASS.A"])
    )
    assert alias_lineage["batches"][0]["batchCanonicalReturnedScopeSha256"] == (
        _symbol_scope_sha256(["CLASS-A"])
    )
    assert alias_lineage["batches"][0]["missingSymbolSha256"] == []
    assert "CLASS-A" not in json.dumps(alias_lineage, sort_keys=True)
    assert "CLASS.A" not in json.dumps(alias_lineage, sort_keys=True)

    collision_session = _Session()
    collision_mapping = _collect(collision_session, ["CLASS-A", "CLASS.A"])
    assert collision_mapping["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert collision_mapping["safeErrorCategory"] == (
        "provider_symbol_mapping_collision"
    )
    assert collision_mapping["requestCounts"] == {
        "oauth": 0,
        "marketCalendar": 0,
        "prices": 0,
    }
    assert collision_session.post_calls == []
    assert collision_session.get_calls == []

    mixed_clock_and_partial = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {
                    "result": [
                        _price_row(
                            symbols[0],
                            timestamp="2026-08-12T00:00:02Z",
                        )
                    ]
                },
                headers=RATE_HEADERS,
            )
        ),
        ["SYNTH", "RENAMED"],
    )
    mixed_summary = mixed_clock_and_partial["clockDomainEvidence"]["summary"]
    assert mixed_summary["primaryRootCause"] == (
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
    )
    assert mixed_summary["rootCauseCounts"][
        "PAYLOAD_TIMESTAMP_AHEAD_OF_ALL_REFERENCES"
    ] == 1
    assert mixed_summary["rootCauseCounts"]["PARTIAL_SYMBOL_RESPONSE"] == 1
    assert mixed_summary["unknownOrUnclassifiedRows"] == 0
    assert mixed_summary["classifiableFailureRows"] == 2
    assert mixed_summary["classifiedRootCauseRows"] == 2
    assert mixed_summary["rootCauseCountMatches"] is True

    conflict = _collect(
        _Session(
            prices=lambda symbols, _: _Response(
                200,
                {"result": [_price_row(symbols[0]), {**_price_row(symbols[0]), "lastPrice": "102.00"}]},
                headers=RATE_HEADERS,
            )
        )
    )
    assert conflict["status"] == "TOSS_SHADOW_SOURCE_CONFLICT"
    assert conflict["summary"]["duplicateRows"] == 1
    assert conflict["prices"] == []

    oversized_session = _Session()
    oversized = _collect(oversized_session, [f"S{index:03d}" for index in range(401)])
    assert oversized["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert oversized["requestCounts"] == {"oauth": 0, "marketCalendar": 0, "prices": 0}
    assert oversized_session.post_calls == []
    assert oversized_session.get_calls == []

    during_request_session = _Session(
        prices=lambda symbols, _: _Response(
            200,
            {
                "result": [
                    _price_row(
                        symbol,
                        timestamp="2026-08-11T22:30:00+09:00",
                    )
                    for symbol in symbols
                ]
            },
            headers=RATE_HEADERS,
        )
    )
    during_request = collect_toss_shadow_market_data(
        during_request_session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=["SYNTH"],
        retrieved_at="2026-08-11T13:29:59Z",
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        request_source_artifact=_request_source_artifact(["SYNTH"]),
        max_price_requests=2,
        clock=lambda: datetime.datetime.fromisoformat(
            "2026-08-11T13:30:01+00:00"
        ),
    )
    assert during_request["status"] == "TOSS_SHADOW_PASS"
    assert during_request["collectionStartedAt"] == "2026-08-11T13:29:59Z"
    assert during_request["retrievedAt"] == "2026-08-11T13:30:01Z"
    assert datetime.datetime.fromisoformat(
        during_request["sourceAsOf"]
    ) <= datetime.datetime.fromisoformat(
        during_request["retrievedAt"].replace("Z", "+00:00")
    )

    reversal_clock_values = iter(
        [
            datetime.datetime.fromisoformat("2026-08-12T00:00:01+00:00"),
            datetime.datetime.fromisoformat("2026-08-12T00:00:00+00:00"),
        ]
    )
    reversal_session = _Session()
    reversal = collect_toss_shadow_market_data(
        reversal_session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=["SYNTH"],
        retrieved_at=RETRIEVED_AT,
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        request_source_artifact=_request_source_artifact(["SYNTH"]),
        max_price_requests=2,
        clock=lambda: next(reversal_clock_values),
        monotonic_clock=lambda: 1.0,
    )
    assert reversal["status"] == "TOSS_SHADOW_SCHEMA_INVALID"
    assert reversal["safeErrorCategory"] == "oauth_received_at_invalid"
    assert reversal["eligible"] is False

    enabled, reason = toss_shadow_runtime_decision(
        {
            "TOSS_SHADOW_PROVIDER_ENABLED": "true",
            "TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED": "true",
            "GITHUB_ACTIONS": "true",
        }
    )
    assert enabled is False
    assert reason == "github_hosted_runner_disabled"
    enabled, reason = toss_shadow_runtime_decision(
        {
            "TOSS_SHADOW_PROVIDER_ENABLED": "true",
            "TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED": "true",
            "GITHUB_ACTIONS": "false",
        }
    )
    assert enabled is True
    assert reason == "registered_server_runtime_enabled"

    sent: list[str] = []

    def delivered(message: str, *, channel: str) -> dict[str, Any]:
        assert channel == "alert"
        sent.append(message)
        return {"attempted": True, "delivered": True, "safeErrorCategory": None}

    fingerprints: set[str] = set()
    alert = dispatch_toss_shadow_alert(
        blocked,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=fingerprints,
        sender=delivered,
    )
    assert alert["status"] == "ALERT_DELIVERED"
    assert alert["attempted"] is True
    assert alert["delivered"] is True
    assert alert["duplicateSuppressed"] is False
    assert len(sent) == 1
    assert "canonical analysis continued=true" in sent[0]
    assert "HTTP: `4xx`" in sent[0]
    assert "SYNTH" not in sent[0]

    partial_alert_messages: list[str] = []

    def partial_delivered(message: str, *, channel: str) -> dict[str, Any]:
        assert channel == "alert"
        partial_alert_messages.append(message)
        return {"attempted": True, "delivered": True, "safeErrorCategory": None}

    partial_alert = dispatch_toss_shadow_alert(
        partial,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=partial_delivered,
    )
    assert partial_alert["status"] == "ALERT_DELIVERED"
    assert len(partial_alert_messages) == 1
    assert "verify hashed missing-symbol lineage and provider symbol mapping" in (
        partial_alert_messages[0]
    )
    assert "registered egress, credentials, rate limit" not in (
        partial_alert_messages[0]
    )

    nullable_alert_messages: list[str] = []

    def nullable_delivered(message: str, *, channel: str) -> dict[str, Any]:
        assert channel == "alert"
        nullable_alert_messages.append(message)
        return {"attempted": True, "delivered": True, "safeErrorCategory": None}

    nullable_alert = dispatch_toss_shadow_alert(
        missing_timestamp,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=nullable_delivered,
    )
    assert nullable_alert["status"] == "ALERT_DELIVERED"
    assert len(nullable_alert_messages) == 1
    assert "TimestampCause: `DOCUMENTED_NULLABLE_TIMESTAMP`" in (
        nullable_alert_messages[0]
    )
    assert "TimestampSlice: `DOCUMENTED_NULLABLE_ONLY`" in (
        nullable_alert_messages[0]
    )
    assert "review documented optional/nullable timestamp policy" in (
        nullable_alert_messages[0]
    )
    assert "registered egress, credentials, rate limit" not in (
        nullable_alert_messages[0]
    )

    nullable_clock_alert_messages: list[str] = []
    dispatch_toss_shadow_alert(
        nullable_slice,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=lambda message, *, channel: (
            nullable_clock_alert_messages.append(message)
            or {"attempted": True, "delivered": True, "safeErrorCategory": None}
        ),
    )
    assert "TimestampSlice: `DOCUMENTED_NULLABLE_WITH_LOCAL_CLOCK_REFERENCE`" in (
        nullable_clock_alert_messages[0]
    )
    assert "verify Mac clock synchronization without compensation" in (
        nullable_clock_alert_messages[0]
    )
    assert "registered egress, credentials, rate limit" not in (
        nullable_clock_alert_messages[0]
    )

    absent_alert_messages: list[str] = []
    dispatch_toss_shadow_alert(
        timestamp_absent,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=lambda message, *, channel: (
            absent_alert_messages.append(message)
            or {"attempted": True, "delivered": True, "safeErrorCategory": None}
        ),
    )
    assert "TimestampCause: `TIMESTAMP_KEY_ABSENT`" in absent_alert_messages[0]
    assert "review documented optional/nullable timestamp policy" in (
        absent_alert_messages[0]
    )
    assert "registered egress, credentials, rate limit" not in (
        absent_alert_messages[0]
    )

    format_alert_messages: list[str] = []
    dispatch_toss_shadow_alert(
        timestamp_unparseable,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=lambda message, *, channel: (
            format_alert_messages.append(message)
            or {"attempted": True, "delivered": True, "safeErrorCategory": None}
        ),
    )
    assert "TimestampCause: `TIMESTAMP_FORMAT_UNPARSEABLE`" in (
        format_alert_messages[0]
    )
    assert "review provider timestamp format contract" in format_alert_messages[0]
    assert "registered egress, credentials, rate limit" not in (
        format_alert_messages[0]
    )

    duplicate = dispatch_toss_shadow_alert(
        blocked,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=fingerprints,
        sender=delivered,
    )
    assert duplicate["status"] == "ALERT_SUPPRESSED_DUPLICATE"
    assert duplicate["attempted"] is False
    assert duplicate["delivered"] is False
    assert duplicate["duplicateSuppressed"] is True
    assert len(sent) == 1

    stage_a_alert = dispatch_toss_shadow_alert(
        {
            **blocked,
            "requestLineage": {
                "requestSourceArtifact": {"sha256": "a" * 64}
            },
        },
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=delivered,
    )
    stage_b_alert = dispatch_toss_shadow_alert(
        {
            **blocked,
            "requestLineage": {
                "requestSourceArtifact": {"sha256": "b" * 64}
            },
        },
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=delivered,
    )
    assert stage_a_alert["alertFingerprint"] != stage_b_alert["alertFingerprint"]

    clock_alert = dispatch_toss_shadow_alert(
        local_clock_behind,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=set(),
        sender=delivered,
    )
    assert clock_alert["status"] == "ALERT_DELIVERED"
    assert "ClockReference: `LOCAL_RECEIPT_CLOCK_BEHIND_SERVER_REFERENCE`" in sent[-1]
    assert "TimestampSlice: `LOCAL_CLOCK_REFERENCE_ANOMALY`" in sent[-1]
    assert "verify Mac clock synchronization without compensation" in sent[-1]
    assert "registered egress, credentials, rate limit" not in sent[-1]
    assert "AffectedRows: `1`" in sent[-1]
    assert "OffsetMs: `1000..1000`" in sent[-1]
    assert "SYNTH" not in sent[-1]

    recovery = dispatch_toss_shadow_alert(
        success,
        previous_status="TOSS_SHADOW_TRANSIENT_FAILURE",
        sent_fingerprints=set(),
        sender=delivered,
    )
    assert recovery["status"] == "ALERT_DELIVERED"
    assert recovery["alertType"] == "TOSS_SHADOW_RECOVERED"

    config_missing = dispatch_toss_shadow_alert(
        blocked,
        previous_status=None,
        sent_fingerprints=set(),
        sender=lambda *_args, **_kwargs: {
            "attempted": False,
            "delivered": False,
            "safeErrorCategory": "config_missing",
        },
    )
    assert config_missing["status"] == "ALERT_CONFIG_MISSING"
    assert config_missing["attempted"] is False
    assert config_missing["delivered"] is False
    assert config_missing["duplicateSuppressed"] is False

    delivery_failure = dispatch_toss_shadow_alert(
        blocked,
        previous_status=None,
        sent_fingerprints=set(),
        sender=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    assert delivery_failure["status"] == "ALERT_DELIVERY_FAILED"
    assert delivery_failure["attempted"] is True
    assert delivery_failure["delivered"] is False
    assert delivery_failure["duplicateSuppressed"] is False
    assert delivery_failure["safeErrorCategory"] == "RuntimeError"
    assert "secret detail" not in json.dumps(delivery_failure)

    renamed = _collect(_Session(), ["RENAMED"])
    base = _collect(_Session(), ["SYNTH"])
    assert _aggregate(renamed) == _aggregate(base)
    assert _collect(_Session(), ["SYNTH"]) == _collect(_Session(), ["SYNTH"])

    print("[TOSS_SHADOW_MARKET_DATA] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
