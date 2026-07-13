from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import harvester as harvester_module
from harvester import (
    _parse_pipe_listing_text,
    _update_symbol_state_entry,
    merge_standard_record,
    refresh_ticker_mapping_from_authoritative_sources,
    should_skip_symbol_for_collection,
)


def main() -> int:
    fixture = json.loads(Path("fixtures/listing_lifecycle_contract.json").read_text(encoding="utf-8"))
    rows, creation_time = _parse_pipe_listing_text(fixture["listingText"], fixture["sourceSpec"])
    by_symbol = {row["symbol"]: row for row in rows}
    expected = fixture["expected"]

    assert sorted(by_symbol) == sorted(expected["activeSymbols"])
    assert sorted(symbol for symbol, row in by_symbol.items() if row["analysisEligible"]) == sorted(
        expected["analysisEligibleSymbols"]
    )
    assert by_symbol["BRK-B"]["sourceSymbol"] == "BRK.B"
    assert by_symbol["PREF-A"]["instrumentType"] == "hybrid"
    assert by_symbol["ETFX"]["instrumentType"] == "etf"
    assert creation_time == expected["sourceCreationTime"]

    original_fetch = harvester_module.fetch_authoritative_listing_rows
    try:
        harvester_module.fetch_authoritative_listing_rows = lambda: (
            by_symbol,
            [{"name": "fixture_nasdaqtrader", "status": "ok", "rowCount": len(by_symbol)}],
        )
        refreshed_map, mapping_audit = refresh_ticker_mapping_from_authoritative_sources(
            {
                "OLDX": {"group": "O", "firstMappedAt": "2025-01-01T00:00:00Z"},
                "BRK-B": {"group": "B", "firstMappedAt": "2025-01-01T00:00:00Z"},
            },
            "2026-07-13 22:00:00",
        )
    finally:
        harvester_module.fetch_authoritative_listing_rows = original_fetch
    assert mapping_audit["status"] == "refreshed"
    assert mapping_audit["addedSymbols"] == ["NNEW"]
    assert mapping_audit["removedSymbols"] == expected["removedFromPreviousMapping"]
    assert sorted(symbol for symbol in refreshed_map if not symbol.startswith("_")) == ["BRK-B", "NNEW"]
    assert refreshed_map["BRK-B"]["firstMappedAt"] == "2025-01-01T00:00:00Z"
    assert refreshed_map["_meta"]["addedCount"] == 1
    assert refreshed_map["_meta"]["removedCount"] == 1

    partial = fixture["partialRefresh"]
    merged = merge_standard_record(partial["previous"], partial["incoming"])
    for key, value in partial["expected"].items():
        assert merged[key] == value

    state_map = {
        "NNEW": {
            "state": "PROVISIONAL",
            "firstSeenAt": "2026-07-10 07:00:00",
            "missingHistoryStreak": 1,
            "missingQuoteStreak": 0,
        }
    }
    touched: set[str] = set()
    recovered = _update_symbol_state_entry(
        state_map,
        "NNEW",
        {
            "instrumentType": "common",
            "analysisEligible": True,
            "historyTier": "FULL",
            "historyPeriods": 120,
            "hasQuotePayload": True,
        },
        touched,
        "2026-07-13 22:00:00",
    )
    assert recovered["state"] == "RECOVERED"
    assert recovered["missingHistoryStreak"] == 0
    assert recovered["missingQuoteStreak"] == 0
    assert "NNEW" in touched

    skipped, category, _ = should_skip_symbol_for_collection(
        {"state": "RETIRED", "reason": "absent_from_authoritative_mapping"},
        authoritative_mapping_refreshed=False,
    )
    assert skipped is True and category == "SYMBOL_SKIPPED_RETIRED"
    refreshed_skip, _, _ = should_skip_symbol_for_collection(
        {"state": "RETIRED", "reason": "absent_from_authoritative_mapping"},
        authoritative_mapping_refreshed=True,
    )
    assert refreshed_skip is False

    print("[LISTING_LIFECYCLE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
