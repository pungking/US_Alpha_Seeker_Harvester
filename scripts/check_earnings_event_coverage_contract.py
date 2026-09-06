from __future__ import annotations

import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from harvester import (
    EARNINGS_COVERAGE_STATUSES,
    EARNINGS_EVENT_MARKET_TIMEZONE,
    EARNINGS_EVENT_MAX_FORWARD_DAYS,
    build_earnings_event_coverage_audit,
    extract_yf_earnings_observation,
    normalize_event_date,
    upsert_earnings_event,
)
import harvester


HARVESTER = (ROOT / "harvester.py").read_text(encoding="utf-8")
MAIN_WORKFLOW = (ROOT / ".github/workflows/main.yml").read_text(encoding="utf-8")
CI_WORKFLOW = (ROOT / ".github/workflows/telegram-routing-ci.yml").read_text(
    encoding="utf-8"
)


class _Frame:
    def __init__(self, *values: object) -> None:
        self.index = list(values)


class _DatedStock:
    calendar: dict[str, object] = {}
    info: dict[str, object] = {}

    def get_earnings_dates(self, limit: int) -> _Frame:
        assert limit == 1
        return _Frame("2026-09-10")


class _PastThenFutureStock:
    calendar = {"Earnings Date": "2026-09-12"}
    info: dict[str, object] = {}

    def get_earnings_dates(self, limit: int) -> _Frame:
        assert limit == 1
        return _Frame("2026-09-01")


class _EmptyStock:
    calendar: dict[str, object] = {}
    info: dict[str, object] = {}

    def get_earnings_dates(self, limit: int) -> _Frame:
        assert limit == 1
        return _Frame()


class _InvalidStock(_EmptyStock):
    def get_earnings_dates(self, limit: int) -> _Frame:
        assert limit == 1
        return _Frame("not-a-date")


class _BrokenStock:
    def get_earnings_dates(self, limit: int) -> _Frame:
        raise RuntimeError("fixture failure")

    @property
    def calendar(self) -> object:
        raise RuntimeError("fixture failure")

    @property
    def info(self) -> object:
        raise RuntimeError("fixture failure")


def _test_yfinance_observation_classification() -> None:
    now_date = datetime.date(2026, 9, 6)
    assert extract_yf_earnings_observation(_DatedStock(), now_date) == {
        "date": "2026-09-10",
        "status": "EARNINGS_DATE_REPORTED",
    }
    assert extract_yf_earnings_observation(_PastThenFutureStock(), now_date) == {
        "date": "2026-09-12",
        "status": "EARNINGS_DATE_REPORTED",
    }
    assert extract_yf_earnings_observation(_EmptyStock(), now_date)["status"] == (
        "EARNINGS_PROVIDER_NO_DATED_EVENT"
    )
    assert extract_yf_earnings_observation(_InvalidStock(), now_date)["status"] == (
        "EARNINGS_DATE_INVALID"
    )
    assert extract_yf_earnings_observation(_BrokenStock(), now_date)["status"] == (
        "EARNINGS_PROVIDER_LOOKUP_FAILED"
    )
    near_midnight_utc = datetime.datetime(
        2026, 9, 7, 0, 30, tzinfo=datetime.timezone.utc
    ).timestamp()
    assert normalize_event_date(near_midnight_utc) == "2026-09-06"


def _test_window_classification() -> None:
    now_date = datetime.date(2026, 9, 6)
    events: dict[str, dict[str, object]] = {}
    assert (
        upsert_earnings_event(
            events, "IN", "2026-09-10", now_date, "fixture", "HIGH"
        )
        == "EARNINGS_EVENT_PRESENT"
    )
    assert (
        upsert_earnings_event(
            events, "PAST", "2026-09-05", now_date, "fixture", "HIGH"
        )
        == "EARNINGS_ONLY_PAST_EVENT_REPORTED"
    )
    assert (
        upsert_earnings_event(
            events, "FUTURE", "2026-11-06", now_date, "fixture", "HIGH"
        )
        == "EARNINGS_EVENT_OUTSIDE_WINDOW_FUTURE"
    )
    assert set(events) == {"IN"}


def _test_coverage_reconciliation() -> None:
    statuses = {
        "IN": {"status": "EARNINGS_EVENT_PRESENT", "source": "fixture"},
        "PAST": {
            "status": "EARNINGS_ONLY_PAST_EVENT_REPORTED",
            "source": "fixture",
        },
        "FUTURE": {
            "status": "EARNINGS_EVENT_OUTSIDE_WINDOW_FUTURE",
            "source": "fixture",
        },
        "NONE": {
            "status": "EARNINGS_PROVIDER_NO_DATED_EVENT",
            "source": "fixture",
        },
        "FAILED": {
            "status": "EARNINGS_PROVIDER_LOOKUP_FAILED",
            "source": "fixture",
        },
        "INVALID": {"status": "EARNINGS_DATE_INVALID", "source": "fixture"},
    }
    assert set(row["status"] for row in statuses.values()) == EARNINGS_COVERAGE_STATUSES
    payload = {
        "events": {
            "IN": {
                "source": "fixture",
                "confidence": "HIGH",
                "event_risk": "NONE",
            }
        },
        "coverage_statuses": statuses,
        "source": "fixture",
        "window": {
            "start_date": "2026-09-06",
            "end_date": "2026-11-05",
            "max_forward_days": EARNINGS_EVENT_MAX_FORWARD_DAYS,
            "market_timezone": EARNINGS_EVENT_MARKET_TIMEZONE,
        },
    }
    audit = build_earnings_event_coverage_audit(
        list(statuses), "STAGE3_FIXTURE.json", "2026-09-06T13:00:00Z", payload
    )
    assert audit["universe_count"] == 6
    assert audit["matched_count"] == 1
    assert audit["missing_count"] == 5
    assert sum(audit["coverage_status_counts"].values()) == 6
    assert audit["unknown_or_unclassified_rows"] == 0
    assert audit["done_when"]["coverageStatusReconciled"] is True
    assert audit["publication_timestamp_available"] is False


def _test_fetch_reconciles_every_symbol_without_external_calls() -> None:
    today = datetime.datetime.now(datetime.timezone.utc).astimezone(
        ZoneInfo(EARNINGS_EVENT_MARKET_TIMEZONE)
    ).date()
    dates = {
        "IN": today + datetime.timedelta(days=5),
        "PAST": today - datetime.timedelta(days=1),
        "FUTURE": today + datetime.timedelta(days=61),
    }

    class _Stock(_EmptyStock):
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def get_earnings_dates(self, limit: int) -> _Frame:
            assert limit == 1
            if self.symbol == "FAILED":
                raise RuntimeError("fixture failure")
            if self.symbol == "INVALID":
                return _Frame("not-a-date")
            value = dates.get(self.symbol)
            return _Frame(value) if value else _Frame()

        @property
        def calendar(self) -> object:
            if self.symbol == "FAILED":
                raise RuntimeError("fixture failure")
            return {}

        @property
        def info(self) -> object:
            if self.symbol == "FAILED":
                raise RuntimeError("fixture failure")
            return {}

    originals = (
        harvester.FMP_API_KEY,
        harvester.FINNHUB_API_KEY,
        harvester.yf.Ticker,
        harvester.time.sleep,
    )
    try:
        harvester.FMP_API_KEY = None
        harvester.FINNHUB_API_KEY = None
        harvester.yf.Ticker = _Stock
        harvester.time.sleep = lambda _: None
        payload = harvester.fetch_earnings_event_map(
            ["IN", "PAST", "FUTURE", "NONE", "FAILED", "INVALID"],
            "STAGE3_FIXTURE.json",
            "2026-09-06T13:00:00Z",
        )
    finally:
        (
            harvester.FMP_API_KEY,
            harvester.FINNHUB_API_KEY,
            harvester.yf.Ticker,
            harvester.time.sleep,
        ) = originals

    assert payload["covered_count"] == 1
    assert payload["missing_count"] == 5
    assert payload["coverage_status_counts"] == {
        "EARNINGS_DATE_INVALID": 1,
        "EARNINGS_EVENT_OUTSIDE_WINDOW_FUTURE": 1,
        "EARNINGS_EVENT_PRESENT": 1,
        "EARNINGS_ONLY_PAST_EVENT_REPORTED": 1,
        "EARNINGS_PROVIDER_LOOKUP_FAILED": 1,
        "EARNINGS_PROVIDER_NO_DATED_EVENT": 1,
    }
    assert sum(payload["coverage_status_counts"].values()) == 6
    assert payload["unknown_or_unclassified_rows"] == 0
    assert payload["coverage_contract_status"] == "PASS"
    yfinance_attempt = next(
        row for row in payload["source_attempts"] if row["source"] == "yfinance"
    )
    assert yfinance_attempt["status"] == "partial"
    assert yfinance_attempt["observation_status_counts"] == payload[
        "coverage_status_counts"
    ]
    assert payload["window"]["max_forward_days"] == 60
    assert payload["window"]["market_timezone"] == "America/New_York"
    assert payload["publication_timestamp_available"] is False


def _test_static_wiring() -> None:
    checker = "python scripts/check_earnings_event_coverage_contract.py"
    assert EARNINGS_EVENT_MAX_FORWARD_DAYS == 60
    assert EARNINGS_EVENT_MARKET_TIMEZONE == "America/New_York"
    assert "datetime.timedelta(days=45)" not in HARVESTER
    assert "datetime.timedelta(days=EARNINGS_EVENT_MAX_FORWARD_DAYS)" in HARVESTER
    assert "ZoneInfo(EARNINGS_EVENT_MARKET_TIMEZONE)" in HARVESTER
    assert checker in MAIN_WORKFLOW
    assert checker in CI_WORKFLOW


def main() -> int:
    _test_yfinance_observation_classification()
    _test_window_classification()
    _test_coverage_reconciliation()
    _test_fetch_reconciles_every_symbol_without_external_calls()
    _test_static_wiring()
    print("[EARNINGS_EVENT_COVERAGE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
