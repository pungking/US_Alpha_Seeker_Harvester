from __future__ import annotations

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
}
RETRIEVED_AT = "2026-08-12T00:00:00Z"
CAPABILITY_SHA = "a" * 64


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


def _price_row(symbol: str, *, timestamp: str = "2026-08-11T22:30:00+09:00") -> dict[str, str]:
    return {
        "symbol": symbol,
        "timestamp": timestamp,
        "lastPrice": "101.25",
        "currency": "USD",
    }


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
    return collect_toss_shadow_market_data(
        session,
        client_id="client-id",
        client_secret="client-secret",
        symbols=symbols or ["SYNTH"],
        retrieved_at=RETRIEVED_AT,
        calendar_date="2026-08-11",
        capability_artifact_sha256=CAPABILITY_SHA,
        max_price_requests=2,
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
    assert "collect_toss_shadow_market_data" in harvester_source
    assert "dispatch_toss_shadow_alert" in harvester_source
    assert "toss_shadow_runtime_decision" in harvester_source
    assert "TOSS_MARKET_DATA_SHADOW.json" in harvester_source
    assert "ensure_toss_shadow_market_data" in harvester_source
    assert "load_latest_stage3_shadow_symbols" in harvester_source
    assert "TOSS_SHADOW_PROVIDER_ENABLED: 'false'" in workflow_source
    assert "TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED: 'false'" in workflow_source
    assert "state/toss-market-data-shadow.json" in workflow_source
    assert "X-Tossinvest-Account" not in shadow_source
    assert "/api/v1/orders" not in shadow_source

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

    symbols = [f"S{index:03d}" for index in range(201)]
    success_session = _Session()
    success = _collect(success_session, symbols)
    assert success["schemaVersion"] == "toss-market-data-shadow-v1"
    assert success["status"] == "TOSS_SHADOW_PASS"
    assert success["mode"] == "SHADOW_ONLY"
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
        "requestedRows": 201,
        "matchedRows": 201,
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
    assert len(success["prices"]) == 201
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

    blocked = _collect(
        _Session(token=_Response(403, {"error": "access_denied"}, headers=RATE_HEADERS))
    )
    assert blocked["status"] == "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED"
    assert blocked["safeErrorCategory"] == "oauth_http_403"
    assert blocked["requestCounts"] == {"oauth": 1, "marketCalendar": 0, "prices": 0}
    assert blocked["eligible"] is False
    assert blocked["analysisContinued"] is True
    assert blocked["circuitBreaker"] == "OPEN_FOR_RUN"

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
                {"result": [_price_row(symbol, timestamp="2026-08-13T22:30:00+09:00") for symbol in symbols]},
                headers=RATE_HEADERS,
            )
        )
    )
    assert future["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert future["prices"] == []

    partial = _collect(
        _Session(prices=lambda symbols, _: _Response(200, {"result": [_price_row(symbols[0])]}, headers=RATE_HEADERS)),
        ["SYNTH", "RENAMED"],
    )
    assert partial["status"] == "TOSS_SHADOW_STALE_OR_PARTIAL"
    assert partial["summary"]["missingRows"] == 1
    assert partial["prices"] == []

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
    assert len(sent) == 1
    assert "canonical analysis continued=true" in sent[0]
    assert "HTTP: `4xx`" in sent[0]
    assert "SYNTH" not in sent[0]
    duplicate = dispatch_toss_shadow_alert(
        blocked,
        previous_status="TOSS_SHADOW_PASS",
        sent_fingerprints=fingerprints,
        sender=delivered,
    )
    assert duplicate["status"] == "ALERT_SUPPRESSED_DUPLICATE"
    assert len(sent) == 1

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

    delivery_failure = dispatch_toss_shadow_alert(
        blocked,
        previous_status=None,
        sent_fingerprints=set(),
        sender=lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    assert delivery_failure["status"] == "ALERT_DELIVERY_FAILED"
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
