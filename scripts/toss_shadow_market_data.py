from __future__ import annotations

import datetime
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "toss-market-data-shadow-v1"
TOKEN_ENDPOINT = "https://openapi.tossinvest.com/oauth2/token"
PRICES_ENDPOINT = "https://openapi.tossinvest.com/api/v1/prices"
US_CALENDAR_ENDPOINT = "https://openapi.tossinvest.com/api/v1/market-calendar/US"
MAX_SYMBOLS_PER_PRICE_REQUEST = 200
MAX_PRICE_REQUESTS_PER_RUN = 2
PASS_STATUS = "TOSS_SHADOW_PASS"
FAILURE_STATUSES = {
    "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED",
    "TOSS_SHADOW_RATE_LIMITED",
    "TOSS_SHADOW_TRANSIENT_FAILURE",
    "TOSS_SHADOW_SCHEMA_INVALID",
    "TOSS_SHADOW_STALE_OR_PARTIAL",
    "TOSS_SHADOW_SOURCE_CONFLICT",
}


class _Response(Protocol):
    status_code: int
    content: bytes
    headers: Any

    def json(self) -> Any: ...


class _Session(Protocol):
    def post(self, url: str, **kwargs: Any) -> _Response: ...

    def get(self, url: str, **kwargs: Any) -> _Response: ...


def _bool_env(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def toss_shadow_runtime_decision(environment: Mapping[str, Any]) -> tuple[bool, str]:
    if not _bool_env(environment.get("TOSS_SHADOW_PROVIDER_ENABLED")):
        return False, "shadow_provider_disabled"
    if _bool_env(environment.get("GITHUB_ACTIONS")):
        return False, "github_hosted_runner_disabled"
    if not _bool_env(environment.get("TOSS_SHADOW_REGISTERED_EGRESS_CONFIRMED")):
        return False, "registered_egress_not_confirmed"
    return True, "registered_server_runtime_enabled"


def _parse_timestamp(value: Any) -> datetime.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return timestamp if timestamp.utcoffset() is not None else None


def _parse_date(value: Any) -> datetime.date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _status_category(status_code: int) -> str:
    return f"{status_code // 100}xx" if 100 <= status_code <= 599 else "invalid"


def _bounded_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized if re.fullmatch(r"[a-z0-9_-]{1,64}", normalized) else None


def _safe_error_code(response: _Response, *, oauth: bool) -> str | None:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if oauth:
        return _bounded_error_code(
            error.get("code") if isinstance(error, dict) else error
        )
    return _bounded_error_code(error.get("code") if isinstance(error, dict) else error)


def _response_sha256(response: _Response) -> str:
    return hashlib.sha256(bytes(getattr(response, "content", b""))).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _rate_limit_evidence(response: _Response) -> dict[str, Any]:
    headers = {
        str(key).lower(): str(value)
        for key, value in (getattr(response, "headers", {}) or {}).items()
    }
    evidence = {
        "limit": headers.get("x-ratelimit-limit"),
        "remaining": headers.get("x-ratelimit-remaining"),
        "reset": headers.get("x-ratelimit-reset"),
        "retryAfter": headers.get("retry-after"),
    }
    evidence["requiredHeadersPresent"] = all(
        evidence[key] is not None for key in ("limit", "remaining", "reset")
    )
    return evidence


def _base_result(
    *,
    symbols: list[str],
    retrieved_at: str,
    calendar_date: str,
    capability_artifact_sha256: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "TOSS_SHADOW_SCHEMA_INVALID",
        "mode": "SHADOW_ONLY",
        "provider": "TOSS_OPEN_API",
        "priceSemantics": "LATEST_QUOTE_NOT_HISTORICAL_ADJUSTED_CANDLE",
        "adjustedPriceSemantics": "NOT_APPLICABLE_TO_PRICES_ENDPOINT",
        "capabilityArtifactSha256": capability_artifact_sha256,
        "requestScopeSha256": _canonical_sha256(
            {"symbols": symbols, "calendarDate": calendar_date}
        ),
        "retrievedAt": retrieved_at,
        "sourceAsOf": None,
        "marketTimezone": "America/New_York",
        "requestCounts": {"oauth": 0, "marketCalendar": 0, "prices": 0},
        "endpointGroups": {
            "oauth": "AUTH",
            "marketCalendar": "MARKET_INFO",
            "prices": "MARKET_DATA",
        },
        "responseSha256": {"marketCalendar": None, "prices": []},
        "httpStatusCategories": {
            "oauth": None,
            "marketCalendar": None,
            "prices": [],
        },
        "rateLimitHeaders": {"oauth": None, "marketCalendar": None, "prices": []},
        "rateLimitHeadersComplete": False,
        "calendar": {"schemaValid": False, "marketTimezone": "Asia/Seoul"},
        "prices": [],
        "summary": {
            "requestedRows": len(symbols),
            "matchedRows": 0,
            "missingRows": len(symbols),
            "invalidRows": 0,
            "duplicateRows": 0,
            "responseSha256Rows": 0,
        },
        "diagnostics": {
            "staleOrFutureRows": 0,
            "currencyConflictRows": 0,
        },
        "eligible": False,
        "tossEvidenceExcluded": True,
        "analysisContinued": True,
        "canonicalSourceChanged": False,
        "policyImpact": "NONE_REPORT_ONLY",
        "circuitBreaker": "OPEN_FOR_RUN",
        "affectedEndpointGroup": None,
        "httpStatusCategory": None,
        "safeErrorCategory": None,
        "safeErrorCode": None,
        "accountHeaderUsed": False,
        "orderEndpointUsed": False,
    }


def _failure_status(status_code: int) -> str:
    if status_code in {401, 403}:
        return "TOSS_SHADOW_AUTH_OR_NETWORK_BLOCKED"
    if status_code == 429:
        return "TOSS_SHADOW_RATE_LIMITED"
    if status_code >= 500:
        return "TOSS_SHADOW_TRANSIENT_FAILURE"
    return "TOSS_SHADOW_SCHEMA_INVALID"


def build_toss_shadow_blocked_result(
    *,
    status: str,
    safe_error_category: str,
    capability_artifact_sha256: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    result = _base_result(
        symbols=[],
        retrieved_at=retrieved_at or "",
        calendar_date="",
        capability_artifact_sha256=capability_artifact_sha256,
    )
    result["runtimeAction"] = "NOT_RUN_CAPABILITY_BLOCKED"
    return _mark_failure(
        result,
        status=status,
        category=safe_error_category,
        endpoint_group="AUTH",
    )


def _mark_failure(
    result: dict[str, Any],
    *,
    status: str,
    category: str,
    endpoint_group: str,
    http_status: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    result["status"] = status
    result["eligible"] = False
    result["tossEvidenceExcluded"] = True
    result["circuitBreaker"] = "OPEN_FOR_RUN"
    result["prices"] = []
    result["safeErrorCategory"] = category
    result["safeErrorCode"] = error_code
    result["affectedEndpointGroup"] = endpoint_group
    result["httpStatusCategory"] = (
        _status_category(http_status) if http_status is not None else None
    )
    return result


def _validate_calendar(
    payload: Any,
    requested_date: str,
) -> tuple[dict[str, Any] | None, set[datetime.date]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return None, set()

    required_days = ("today", "previousBusinessDay", "nextBusinessDay")
    if not all(isinstance(result.get(key), dict) for key in required_days):
        return None, set()
    if result["today"].get("date") != requested_date:
        return None, set()

    parsed_dates = {
        key: _parse_date(result[key].get("date"))
        for key in required_days
    }
    if (
        any(value is None for value in parsed_dates.values())
        or not parsed_dates["previousBusinessDay"]
        < parsed_dates["today"]
        < parsed_dates["nextBusinessDay"]
    ):
        return None, set()

    allowed_price_dates: set[datetime.date] = set()
    safe_days: dict[str, Any] = {}
    for key in required_days:
        day = result[key]
        parsed_day = parsed_dates[key]
        if key in {"today", "previousBusinessDay"}:
            allowed_price_dates.add(parsed_day)
        safe_day: dict[str, Any] = {"date": day["date"]}
        for session_name in ("dayMarket", "preMarket", "regularMarket", "afterMarket"):
            session = day.get(session_name)
            if session is None:
                safe_day[session_name] = None
                continue
            if not isinstance(session, dict):
                return None, set()
            start = _parse_timestamp(session.get("startTime"))
            end = _parse_timestamp(session.get("endTime"))
            if (
                start is None
                or end is None
                or start.utcoffset() != datetime.timedelta(hours=9)
                or end.utcoffset() != datetime.timedelta(hours=9)
                or start >= end
            ):
                return None, set()
            safe_day[session_name] = {
                "startTime": session["startTime"],
                "endTime": session["endTime"],
            }
        safe_days[key] = safe_day
    return safe_days, allowed_price_dates


def _normalize_symbols(symbols: list[str]) -> list[str]:
    valid = {
        symbol.strip().upper()
        for symbol in symbols
        if isinstance(symbol, str)
        and re.fullmatch(r"[A-Za-z0-9.\-]+", symbol.strip())
    }
    return sorted(valid)


def collect_toss_shadow_market_data(
    session: _Session,
    *,
    client_id: str,
    client_secret: str,
    symbols: list[str],
    retrieved_at: str,
    calendar_date: str,
    capability_artifact_sha256: str,
    max_price_requests: int = 2,
) -> dict[str, Any]:
    normalized_symbols = _normalize_symbols(symbols)
    result = _base_result(
        symbols=normalized_symbols,
        retrieved_at=retrieved_at,
        calendar_date=calendar_date,
        capability_artifact_sha256=capability_artifact_sha256,
    )
    retrieved_timestamp = _parse_timestamp(retrieved_at)
    if (
        not normalized_symbols
        or retrieved_timestamp is None
        or _parse_date(calendar_date) is None
        or not re.fullmatch(r"[0-9a-f]{64}", capability_artifact_sha256)
        or max_price_requests <= 0
        or max_price_requests > MAX_PRICE_REQUESTS_PER_RUN
        or len(normalized_symbols) > MAX_SYMBOLS_PER_PRICE_REQUEST * max_price_requests
    ):
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="request_contract_invalid",
            endpoint_group="LOCAL_CONTRACT",
        )

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
        return _mark_failure(
            result,
            status="TOSS_SHADOW_TRANSIENT_FAILURE",
            category=type(exc).__name__,
            endpoint_group="AUTH",
        )

    result["rateLimitHeaders"]["oauth"] = _rate_limit_evidence(token_response)
    token_status = int(getattr(token_response, "status_code", 0) or 0)
    result["httpStatusCategories"]["oauth"] = _status_category(token_status)
    if token_status != 200:
        return _mark_failure(
            result,
            status=_failure_status(token_status),
            category=f"oauth_http_{token_status}",
            endpoint_group="AUTH",
            http_status=token_status,
            error_code=_safe_error_code(token_response, oauth=True),
        )
    try:
        token_payload = token_response.json()
    except (TypeError, ValueError) as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=f"oauth_json_{type(exc).__name__}",
            endpoint_group="AUTH",
        )
    access_token = token_payload.get("access_token") if isinstance(token_payload, dict) else None
    if (
        not isinstance(access_token, str)
        or not access_token
        or token_payload.get("token_type") != "Bearer"
        or not isinstance(token_payload.get("expires_in"), int)
        or token_payload["expires_in"] <= 0
    ):
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="oauth_schema_invalid",
            endpoint_group="AUTH",
        )

    authorization = {"Authorization": f"Bearer {access_token}"}
    result["requestCounts"]["marketCalendar"] = 1
    try:
        calendar_response = session.get(
            US_CALENDAR_ENDPOINT,
            params={"date": calendar_date},
            headers=authorization,
            timeout=20,
        )
    except Exception as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_TRANSIENT_FAILURE",
            category=type(exc).__name__,
            endpoint_group="MARKET_INFO",
        )
    calendar_status = int(getattr(calendar_response, "status_code", 0) or 0)
    result["httpStatusCategories"]["marketCalendar"] = _status_category(
        calendar_status
    )
    result["rateLimitHeaders"]["marketCalendar"] = _rate_limit_evidence(calendar_response)
    result["responseSha256"]["marketCalendar"] = _response_sha256(calendar_response)
    result["summary"]["responseSha256Rows"] += 1
    if calendar_status != 200:
        return _mark_failure(
            result,
            status=_failure_status(calendar_status),
            category=f"market_calendar_http_{calendar_status}",
            endpoint_group="MARKET_INFO",
            http_status=calendar_status,
            error_code=_safe_error_code(calendar_response, oauth=False),
        )
    try:
        calendar_payload = calendar_response.json()
    except (TypeError, ValueError) as exc:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=f"market_calendar_json_{type(exc).__name__}",
            endpoint_group="MARKET_INFO",
        )
    safe_calendar, allowed_price_dates = _validate_calendar(calendar_payload, calendar_date)
    if safe_calendar is None:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category="market_calendar_schema_invalid",
            endpoint_group="MARKET_INFO",
        )
    result["calendar"] = {
        "schemaValid": True,
        "marketTimezone": "Asia/Seoul",
        "requestedDate": calendar_date,
        **safe_calendar,
    }

    requested = set(normalized_symbols)
    seen: dict[str, dict[str, Any]] = {}
    invalid_rows = 0
    duplicate_rows = 0
    stale_rows = 0
    currency_conflicts = 0
    source_timestamps: list[tuple[datetime.datetime, str]] = []

    for offset in range(0, len(normalized_symbols), MAX_SYMBOLS_PER_PRICE_REQUEST):
        batch = normalized_symbols[offset : offset + MAX_SYMBOLS_PER_PRICE_REQUEST]
        result["requestCounts"]["prices"] += 1
        try:
            price_response = session.get(
                PRICES_ENDPOINT,
                params={"symbols": ",".join(batch)},
                headers=authorization,
                timeout=20,
            )
        except Exception as exc:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_TRANSIENT_FAILURE",
                category=type(exc).__name__,
                endpoint_group="MARKET_DATA",
            )
        price_status = int(getattr(price_response, "status_code", 0) or 0)
        result["httpStatusCategories"]["prices"].append(
            _status_category(price_status)
        )
        result["rateLimitHeaders"]["prices"].append(_rate_limit_evidence(price_response))
        result["responseSha256"]["prices"].append(_response_sha256(price_response))
        result["summary"]["responseSha256Rows"] += 1
        if price_status != 200:
            return _mark_failure(
                result,
                status=_failure_status(price_status),
                category=f"prices_http_{price_status}",
                endpoint_group="MARKET_DATA",
                http_status=price_status,
                error_code=_safe_error_code(price_response, oauth=False),
            )
        try:
            price_payload = price_response.json()
        except (TypeError, ValueError) as exc:
            return _mark_failure(
                result,
                status="TOSS_SHADOW_SCHEMA_INVALID",
                category=f"prices_json_{type(exc).__name__}",
                endpoint_group="MARKET_DATA",
            )
        rows = price_payload.get("result") if isinstance(price_payload, dict) else None
        if not isinstance(rows, list):
            invalid_rows += 1
            continue
        for row in rows:
            if not isinstance(row, dict):
                invalid_rows += 1
                continue
            symbol = row.get("symbol")
            price = _decimal(row.get("lastPrice"))
            currency = row.get("currency")
            timestamp = _parse_timestamp(row.get("timestamp"))
            if (
                not isinstance(symbol, str)
                or symbol not in requested
                or price is None
                or price <= 0
                or not isinstance(currency, str)
            ):
                invalid_rows += 1
                continue
            if symbol in seen:
                duplicate_rows += 1
                continue
            if currency != "USD":
                currency_conflicts += 1
                continue
            if (
                timestamp is None
                or timestamp > retrieved_timestamp
                or timestamp.astimezone(ZoneInfo("America/New_York")).date()
                not in allowed_price_dates
            ):
                stale_rows += 1
                continue
            safe_row = {
                "symbol": symbol,
                "timestamp": row["timestamp"],
                "lastPrice": format(price, "f"),
                "currency": currency,
                "sourceAsOfUtc": timestamp.astimezone(datetime.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
            seen[symbol] = safe_row
            source_timestamps.append((timestamp, row["timestamp"]))

    missing = requested.difference(seen)
    result["summary"].update(
        {
            "matchedRows": len(seen),
            "missingRows": len(missing),
            "invalidRows": invalid_rows,
            "duplicateRows": duplicate_rows,
        }
    )
    result["diagnostics"] = {
        "staleOrFutureRows": stale_rows,
        "currencyConflictRows": currency_conflicts,
    }
    rate_rows = [
        result["rateLimitHeaders"]["oauth"],
        result["rateLimitHeaders"]["marketCalendar"],
        *result["rateLimitHeaders"]["prices"],
    ]
    result["rateLimitHeadersComplete"] = all(
        isinstance(row, dict) and row.get("requiredHeadersPresent") is True
        for row in rate_rows
    )

    if duplicate_rows or currency_conflicts:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SOURCE_CONFLICT",
            category=(
                "duplicate_symbol_response"
                if duplicate_rows
                else "currency_mismatch"
            ),
            endpoint_group="MARKET_DATA",
        )
    if invalid_rows or not result["rateLimitHeadersComplete"]:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_SCHEMA_INVALID",
            category=("prices_schema_invalid" if invalid_rows else "rate_limit_headers_missing"),
            endpoint_group="MARKET_DATA",
        )
    if stale_rows or missing:
        return _mark_failure(
            result,
            status="TOSS_SHADOW_STALE_OR_PARTIAL",
            category=("stale_or_future_timestamp" if stale_rows else "partial_symbol_response"),
            endpoint_group="MARKET_DATA",
        )

    latest = max(source_timestamps, key=lambda item: item[0])
    result.update(
        {
            "status": PASS_STATUS,
            "sourceAsOf": latest[1],
            "prices": [seen[symbol] for symbol in sorted(seen)],
            "eligible": True,
            "tossEvidenceExcluded": False,
            "circuitBreaker": "CLOSED",
            "safeErrorCategory": None,
            "safeErrorCode": None,
        }
    )
    return result


def _alert_fingerprint(status: str, category: Any, endpoint: Any) -> str:
    return _canonical_sha256(
        {
            "status": status,
            "safeErrorCategory": str(category or "none"),
            "affectedEndpointGroup": str(endpoint or "none"),
        }
    )


def _latest_http_status_category(result: Mapping[str, Any]) -> str:
    direct = result.get("httpStatusCategory")
    if isinstance(direct, str) and direct:
        return direct
    categories = result.get("httpStatusCategories")
    if not isinstance(categories, Mapping):
        return "not_available"
    prices = categories.get("prices")
    if isinstance(prices, list) and prices and isinstance(prices[-1], str):
        return prices[-1]
    for key in ("marketCalendar", "oauth"):
        value = categories.get(key)
        if isinstance(value, str) and value:
            return value
    return "not_available"


def dispatch_toss_shadow_alert(
    result: Mapping[str, Any],
    *,
    previous_status: str | None,
    sent_fingerprints: set[str],
    sender: Callable[..., Mapping[str, Any]],
) -> dict[str, Any]:
    status = str(result.get("status") or "TOSS_SHADOW_SCHEMA_INVALID")
    alert_type: str | None = None
    if status == PASS_STATUS:
        if previous_status in FAILURE_STATUSES:
            alert_type = "TOSS_SHADOW_RECOVERED"
        else:
            return {
                "status": "ALERT_NOT_REQUIRED",
                "alertType": None,
                "alertFingerprint": None,
                "safeErrorCategory": None,
            }
    elif status in FAILURE_STATUSES:
        alert_type = "TOSS_SHADOW_FAILURE"
    else:
        return {
            "status": "ALERT_NOT_REQUIRED",
            "alertType": None,
            "alertFingerprint": None,
            "safeErrorCategory": None,
        }

    fingerprint = _alert_fingerprint(
        alert_type,
        result.get("safeErrorCategory"),
        result.get("affectedEndpointGroup"),
    )
    if fingerprint in sent_fingerprints:
        return {
            "status": "ALERT_SUPPRESSED_DUPLICATE",
            "alertType": alert_type,
            "alertFingerprint": fingerprint,
            "safeErrorCategory": None,
        }
    sent_fingerprints.add(fingerprint)

    counts = result.get("requestCounts") or {}
    http_status_category = _latest_http_status_category(result)
    if alert_type == "TOSS_SHADOW_RECOVERED":
        message = (
            "✅ *Toss SHADOW source recovered*\n"
            f"Status: `{status}`\n"
            "Toss evidence eligible=true\n"
            "canonical analysis continued=true"
        )
    else:
        message = (
            "⚠️ *Toss SHADOW source excluded*\n"
            f"Status: `{status}`\n"
            f"Error: `{result.get('safeErrorCategory') or 'unclassified'}`\n"
            f"HTTP: `{http_status_category}`\n"
            f"EndpointGroup: `{result.get('affectedEndpointGroup') or 'unknown'}`\n"
            "Requests: "
            f"oauth={int(counts.get('oauth') or 0)}, "
            f"calendar={int(counts.get('marketCalendar') or 0)}, "
            f"prices={int(counts.get('prices') or 0)}\n"
            "CircuitBreaker: `OPEN_FOR_RUN`\n"
            "Toss evidence excluded=true\n"
            "canonical analysis continued=true\n"
            "Next: verify registered egress, credentials, rate limit, and source contract"
        )

    try:
        delivery = sender(message, channel="alert") or {}
    except Exception as exc:
        return {
            "status": "ALERT_DELIVERY_FAILED",
            "alertType": alert_type,
            "alertFingerprint": fingerprint,
            "safeErrorCategory": type(exc).__name__,
        }
    if delivery.get("delivered") is True:
        alert_status = "ALERT_DELIVERED"
    elif (
        delivery.get("attempted") is False
        and delivery.get("safeErrorCategory") == "config_missing"
    ):
        alert_status = "ALERT_CONFIG_MISSING"
    else:
        alert_status = "ALERT_DELIVERY_FAILED"
    return {
        "status": alert_status,
        "alertType": alert_type,
        "alertFingerprint": fingerprint,
        "safeErrorCategory": delivery.get("safeErrorCategory"),
        "httpStatusCategory": http_status_category,
    }
