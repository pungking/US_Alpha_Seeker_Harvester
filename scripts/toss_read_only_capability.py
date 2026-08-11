from __future__ import annotations

import datetime
import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


SCHEMA_VERSION = "toss-read-only-capability-v1"
TOKEN_ENDPOINT = "https://openapi.tossinvest.com/oauth2/token"
CANDLES_ENDPOINT = "https://openapi.tossinvest.com/api/v1/candles"


class _Response(Protocol):
    status_code: int
    content: bytes
    headers: Any

    def json(self) -> Any: ...


class _Session(Protocol):
    def post(self, url: str, **kwargs: Any) -> _Response: ...

    def get(self, url: str, **kwargs: Any) -> _Response: ...


def _status_category(status_code: int) -> str:
    return f"{status_code // 100}xx" if 100 <= status_code <= 599 else "invalid"


def _failure_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "TOSS_AUTH_OR_ENTITLEMENT_BLOCKED"
    if status_code == 429:
        return "TOSS_RATE_LIMITED"
    if status_code >= 500:
        return "TOSS_TRANSIENT_FAILURE"
    return "TOSS_RESPONSE_CONTRACT_INVALID"


def _safe_oauth_error_code(response: _Response) -> str | None:
    """Return only a bounded machine-readable error code, never the message/body."""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    if not isinstance(code, str):
        return None
    normalized = code.strip().lower()
    return normalized if re.fullmatch(r"[a-z0-9_-]{1,64}", normalized) else None


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _base_result(symbol: str, retrieved_at: str) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "probeStatus": "TOSS_RESPONSE_CONTRACT_INVALID",
        "source": "TOSS_OPEN_API_CANDLES",
        "oauthEndpoint": "/oauth2/token",
        "marketDataEndpoint": "/api/v1/candles",
        "retrievedAt": retrieved_at,
        "probeSymbolSha256": hashlib.sha256(symbol.encode("utf-8")).hexdigest(),
        "requestCounts": {"oauth": 0, "marketData": 0},
        "oauthHttpStatusCategory": None,
        "oauthErrorCode": None,
        "marketDataHttpStatusCategory": None,
        "schemaValid": False,
        "adjustedRequest": True,
        "adjustedSemantics": "OFFICIAL_REQUEST_PARAMETER_ACCEPTED_NO_RESPONSE_ECHO",
        "pagination": {
            "officialMaxCount": 200,
            "requestedCount": 2,
            "returnedCount": 0,
            "nextBeforePresent": False,
            "nextBeforeValid": False,
        },
        "timestampEvidence": {
            "sourceAsOf": None,
            "sourceAsOfBasis": "LATEST_CANDLE_TIMESTAMP",
            "timezoneOffsetPresent": False,
        },
        "rateLimitHeadersPresent": False,
        "rateLimitHeaders": {
            "limitPresent": False,
            "remainingPresent": False,
            "resetPresent": False,
        },
        "responseSha256": None,
        "accountHeaderUsed": False,
        "orderEndpointUsed": False,
        "safeErrorCategory": None,
    }


def probe_toss_read_only_capability(
    session: _Session,
    *,
    client_id: str,
    client_secret: str,
    symbol: str,
    retrieved_at: str,
) -> dict[str, Any]:
    """Issue one OAuth request and, on success, one read-only candle request."""
    result = _base_result(symbol, retrieved_at)
    result["requestCounts"]["oauth"] = 1
    try:
        token_response = session.post(
            TOKEN_ENDPOINT,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
    except Exception as exc:
        result["probeStatus"] = "TOSS_TRANSIENT_FAILURE"
        result["safeErrorCategory"] = type(exc).__name__
        return result

    token_status = int(getattr(token_response, "status_code", 0) or 0)
    result["oauthHttpStatusCategory"] = _status_category(token_status)
    if token_status != 200:
        result["probeStatus"] = _failure_status(token_status)
        result["oauthErrorCode"] = _safe_oauth_error_code(token_response)
        result["safeErrorCategory"] = f"oauth_http_{token_status}"
        return result

    try:
        token_payload = token_response.json()
    except Exception as exc:
        result["safeErrorCategory"] = f"oauth_json_{type(exc).__name__}"
        return result
    access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    token_type = token_payload.get("token_type") if isinstance(token_payload, dict) else None
    expires_in = token_payload.get("expires_in") if isinstance(token_payload, dict) else None
    if (
        not isinstance(access_token, str)
        or not access_token
        or token_type != "Bearer"
        or not isinstance(expires_in, int)
        or expires_in <= 0
    ):
        result["safeErrorCategory"] = "oauth_schema_invalid"
        return result

    result["requestCounts"]["marketData"] = 1
    try:
        market_response = session.get(
            CANDLES_ENDPOINT,
            params={
                "symbol": symbol,
                "interval": "1d",
                "count": 2,
                "adjusted": "true",
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
    except Exception as exc:
        result["probeStatus"] = "TOSS_TRANSIENT_FAILURE"
        result["safeErrorCategory"] = type(exc).__name__
        return result

    market_status = int(getattr(market_response, "status_code", 0) or 0)
    result["marketDataHttpStatusCategory"] = _status_category(market_status)
    result["responseSha256"] = hashlib.sha256(
        bytes(getattr(market_response, "content", b""))
    ).hexdigest()
    if market_status != 200:
        result["probeStatus"] = _failure_status(market_status)
        result["safeErrorCategory"] = f"market_data_http_{market_status}"
        return result

    try:
        payload = market_response.json()
    except Exception as exc:
        result["safeErrorCategory"] = f"market_data_json_{type(exc).__name__}"
        return result

    page = payload.get("result") if isinstance(payload, dict) else None
    candles = page.get("candles") if isinstance(page, dict) else None
    next_before_present = isinstance(page, dict) and "nextBefore" in page
    next_before = page.get("nextBefore") if isinstance(page, dict) else None
    parsed_next_before = _parse_timestamp(next_before) if next_before is not None else None
    result["pagination"] = {
        "officialMaxCount": 200,
        "requestedCount": 2,
        "returnedCount": len(candles) if isinstance(candles, list) else 0,
        "nextBeforePresent": next_before_present,
        "nextBeforeValid": next_before is None or parsed_next_before is not None,
    }

    required = {
        "timestamp",
        "openPrice",
        "highPrice",
        "lowPrice",
        "closePrice",
        "volume",
        "currency",
    }
    timestamps: list[datetime.datetime] = []
    candle_schema_valid = isinstance(candles, list) and 0 < len(candles) <= 2
    for candle in candles if isinstance(candles, list) else []:
        if not isinstance(candle, dict) or not required.issubset(candle):
            candle_schema_valid = False
            break
        timestamp = _parse_timestamp(candle.get("timestamp"))
        open_price = _decimal(candle.get("openPrice"))
        high_price = _decimal(candle.get("highPrice"))
        low_price = _decimal(candle.get("lowPrice"))
        close_price = _decimal(candle.get("closePrice"))
        volume = _decimal(candle.get("volume"))
        prices = (open_price, high_price, low_price, close_price)
        values_valid = bool(
            all(value is not None and value > 0 for value in prices)
            and volume is not None
            and volume >= 0
            and high_price >= max(open_price, low_price, close_price)
            and low_price <= min(open_price, high_price, close_price)
        )
        if timestamp is None or timestamp.utcoffset() is None or not values_valid:
            candle_schema_valid = False
            break
        timestamps.append(timestamp)

    headers = getattr(market_response, "headers", {}) or {}
    rate_headers = {
        "limitPresent": "X-RateLimit-Limit" in headers,
        "remainingPresent": "X-RateLimit-Remaining" in headers,
        "resetPresent": "X-RateLimit-Reset" in headers,
    }
    result["rateLimitHeaders"] = rate_headers
    result["rateLimitHeadersPresent"] = all(rate_headers.values())
    retrieved_timestamp = _parse_timestamp(retrieved_at)
    source_timestamp = max(timestamps) if timestamps else None
    timestamp_order_valid = bool(
        source_timestamp
        and retrieved_timestamp
        and source_timestamp <= retrieved_timestamp
    )
    result["timestampEvidence"] = {
        "sourceAsOf": source_timestamp.isoformat() if source_timestamp else None,
        "sourceAsOfBasis": "LATEST_CANDLE_TIMESTAMP",
        "timezoneOffsetPresent": bool(timestamps)
        and all(timestamp.utcoffset() is not None for timestamp in timestamps),
        "sourceAsOfNotAfterRetrievedAt": timestamp_order_valid,
    }
    result["schemaValid"] = bool(
        candle_schema_valid
        and next_before_present
        and (next_before is None or parsed_next_before is not None)
        and timestamp_order_valid
    )
    if result["schemaValid"] and result["rateLimitHeadersPresent"]:
        result["probeStatus"] = "TOSS_READ_ONLY_CAPABILITY_PASS"
        result["safeErrorCategory"] = None
    else:
        result["safeErrorCategory"] = "market_data_contract_incomplete"
    return result
