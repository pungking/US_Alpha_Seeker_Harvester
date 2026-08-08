from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.toss_read_only_capability import (  # noqa: E402
    probe_toss_read_only_capability,
)


class _Response:
    def __init__(self, status_code: int, payload: dict, *, body: bytes = b"{}", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.content = body
        self.headers = headers or {}

    def json(self) -> dict:
        return self._payload


class _Session:
    def __init__(self, token_response: _Response, market_response: _Response | None = None):
        self.token_response = token_response
        self.market_response = market_response
        self.post_calls = []
        self.get_calls = []

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.token_response

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.market_response is None:
            raise AssertionError("unexpected market-data request")
        return self.market_response


def main() -> int:
    harvester_source = Path("harvester.py").read_text(encoding="utf-8")
    capability_source = Path("scripts/toss_read_only_capability.py").read_text(
        encoding="utf-8"
    )
    workflow_source = Path(".github/workflows/main.yml").read_text(encoding="utf-8")
    assert 'existing.get("oneShotReservedAt")' in harvester_source
    assert '"runtimeAction"] = "REUSED_ONE_SHOT_RESULT"' in harvester_source
    assert 'filtered_tickers.keys()' in harvester_source
    assert "TOSS_CLIENT_ID: ${{ secrets.TOSS_CLIENT_ID }}" in workflow_source
    assert "TOSS_CLIENT_SECRET: ${{ secrets.TOSS_CLIENT_SECRET }}" in workflow_source
    assert "X-Tossinvest-Account" not in capability_source
    assert "/api/v1/orders" not in capability_source

    success = _Session(
        _Response(
            200,
            {"access_token": "secret-token", "token_type": "Bearer", "expires_in": 86400},
        ),
        _Response(
            200,
            {
                "result": {
                    "candles": [
                        {
                            "timestamp": "2026-08-07T09:30:00-04:00",
                            "openPrice": "100.00",
                            "highPrice": "102.00",
                            "lowPrice": "99.00",
                            "closePrice": "101.00",
                            "volume": "1200",
                            "currency": "USD",
                        }
                    ],
                    "nextBefore": "2026-08-06T09:30:00-04:00",
                }
            },
            body=b'{"result":{"candles":[]}}',
            headers={
                "X-RateLimit-Limit": "5",
                "X-RateLimit-Remaining": "4",
                "X-RateLimit-Reset": "0.2",
            },
        ),
    )
    result = probe_toss_read_only_capability(
        success,
        client_id="client-id",
        client_secret="client-secret",
        symbol="SYNTH",
        retrieved_at="2026-08-08T00:00:00Z",
    )
    assert result["probeStatus"] == "TOSS_READ_ONLY_CAPABILITY_PASS"
    assert result["requestCounts"] == {"oauth": 1, "marketData": 1}
    assert result["schemaValid"] is True
    assert result["adjustedRequest"] is True
    assert result["adjustedSemantics"] == "OFFICIAL_REQUEST_PARAMETER_ACCEPTED_NO_RESPONSE_ECHO"
    assert result["pagination"]["nextBeforePresent"] is True
    assert result["pagination"]["officialMaxCount"] == 200
    assert result["pagination"]["returnedCount"] == 1
    assert result["timestampEvidence"]["timezoneOffsetPresent"] is True
    assert result["timestampEvidence"]["sourceAsOfNotAfterRetrievedAt"] is True
    assert result["rateLimitHeadersPresent"] is True
    assert result["accountHeaderUsed"] is False
    assert result["orderEndpointUsed"] is False
    assert re.fullmatch(r"[0-9a-f]{64}", result["responseSha256"])
    assert len(success.post_calls) == 1
    assert len(success.get_calls) == 1
    assert "X-Tossinvest-Account" not in success.get_calls[0][1]["headers"]
    serialized_result = json.dumps(result, sort_keys=True)
    assert "secret-token" not in serialized_result
    assert "client-secret" not in serialized_result

    blocked = _Session(_Response(403, {"error": "access_denied"}))
    blocked_result = probe_toss_read_only_capability(
        blocked,
        client_id="client-id",
        client_secret="client-secret",
        symbol="SYNTH",
        retrieved_at="2026-08-08T00:00:00Z",
    )
    assert blocked_result["probeStatus"] == "TOSS_AUTH_OR_ENTITLEMENT_BLOCKED"
    assert blocked_result["safeErrorCategory"] == "oauth_ip_not_allowed"
    assert blocked_result["requestCounts"] == {"oauth": 1, "marketData": 0}
    assert len(blocked.get_calls) == 0

    limited = _Session(
        _Response(
            200,
            {"access_token": "secret-token", "token_type": "Bearer", "expires_in": 86400},
        ),
        _Response(429, {"error": {"code": "rate-limit"}}),
    )
    limited_result = probe_toss_read_only_capability(
        limited,
        client_id="client-id",
        client_secret="client-secret",
        symbol="RENAMED",
        retrieved_at="2026-08-08T00:00:00Z",
    )
    assert limited_result["probeStatus"] == "TOSS_RATE_LIMITED"
    assert limited_result["requestCounts"] == {"oauth": 1, "marketData": 1}
    assert result["probeSymbolSha256"] != limited_result["probeSymbolSha256"]

    invalid = _Session(_Response(200, {"token_type": "Bearer", "expires_in": 86400}))
    invalid_result = probe_toss_read_only_capability(
        invalid,
        client_id="client-id",
        client_secret="client-secret",
        symbol="SYNTH",
        retrieved_at="2026-08-08T00:00:00Z",
    )
    assert invalid_result["probeStatus"] == "TOSS_RESPONSE_CONTRACT_INVALID"
    assert invalid_result["requestCounts"] == {"oauth": 1, "marketData": 0}

    transient = _Session(
        _Response(
            200,
            {"access_token": "secret-token", "token_type": "Bearer", "expires_in": 86400},
        ),
        _Response(503, {}),
    )
    transient_result = probe_toss_read_only_capability(
        transient,
        client_id="client-id",
        client_secret="client-secret",
        symbol="SYNTH",
        retrieved_at="2026-08-08T00:00:00Z",
    )
    assert transient_result["probeStatus"] == "TOSS_TRANSIENT_FAILURE"
    assert transient_result["requestCounts"] == {"oauth": 1, "marketData": 1}
    print("[TOSS_READ_ONLY_CAPABILITY] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
