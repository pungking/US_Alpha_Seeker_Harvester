from target_lineage import (
    build_target_lineage,
    build_target_lineage_runtime_audit,
    summarize_target_lineage,
)


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
    runtime = summarize_target_lineage(
        [
            {"targetMeanPrice": 123.45, **present},
            {"targetMeanPrice": None, **missing},
        ]
    )
    assert runtime["overall"] == "pass_complete_lineage"
    assert runtime["finiteTargetRows"] == 1
    assert runtime["completeLineageRows"] == 1
    assert runtime["missingLineageRows"] == 0
    fresh_runtime = summarize_target_lineage(
        [{"targetMeanPrice": 123.45, **present}],
        reference_time="2026-07-12T02:02:03Z",
        freshness_max_hours=2,
    )
    assert fresh_runtime["overall"] == "pass_complete_fresh_lineage"
    assert fresh_runtime["freshLineageRows"] == 1
    stale_runtime = summarize_target_lineage(
        [{"targetMeanPrice": 123.45, **present}],
        reference_time="2026-07-13T01:02:03Z",
        freshness_max_hours=2,
    )
    assert stale_runtime["overall"] == "warn_stale_lineage"
    assert stale_runtime["staleLineageRows"] == 1
    checkpoint = build_target_lineage_runtime_audit(
        [{"targetMeanPrice": 123.45, **present}],
        reference_time="2026-07-12T02:02:03Z",
        freshness_max_hours=2,
        batch_mode="manual:all",
        target_symbols=2,
        completed_groups=["A"],
        collection_status="partial_checkpoint",
    )
    assert checkpoint["collectionStatus"] == "partial_checkpoint"
    assert checkpoint["completedGroups"] == ["A"]
    assert checkpoint["completedGroupCount"] == 1
    assert checkpoint["finiteTargetRows"] == 1
    print("[TARGET_LINEAGE_CONTRACT] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
