from target_lineage import build_target_lineage


def main() -> int:
    retrieved_at = "2026-07-12T01:02:03Z"
    present = build_target_lineage(123.45, retrieved_at)
    assert present == {
        "targetMeanPriceSource": "YFINANCE_INFO",
        "targetMeanPriceRetrievedAt": retrieved_at,
        "targetMeanPriceAsOf": None,
        "targetMeanPriceAsOfStatus": "VENDOR_TARGET_ASOF_UNKNOWN",
    }

    missing = build_target_lineage(None, retrieved_at)
    assert missing == {
        "targetMeanPriceSource": None,
        "targetMeanPriceRetrievedAt": None,
        "targetMeanPriceAsOf": None,
        "targetMeanPriceAsOfStatus": "TARGET_SOURCE_NOT_AVAILABLE",
    }
    print("[TARGET_LINEAGE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
