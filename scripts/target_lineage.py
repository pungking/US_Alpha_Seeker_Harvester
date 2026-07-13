import math
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any


def build_target_lineage(target_value: Any, retrieved_at: str) -> dict[str, str | None]:
    try:
        target = float(target_value)
    except (TypeError, ValueError):
        target = math.nan

    if not math.isfinite(target) or target <= 0:
        return {
            "targetMeanPriceSource": None,
            "targetMeanPriceRetrievedAt": None,
            "targetMeanPriceAsOf": None,
            "targetMeanPriceAsOfStatus": "TARGET_SOURCE_NOT_AVAILABLE",
        }

    return {
        "targetMeanPriceSource": "YFINANCE_INFO",
        "targetMeanPriceRetrievedAt": retrieved_at,
        "targetMeanPriceAsOf": None,
        "targetMeanPriceAsOfStatus": "VENDOR_TARGET_ASOF_UNKNOWN",
    }


def _parse_utc_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def summarize_target_lineage(
    records: Iterable[Mapping[str, Any]],
    reference_time: str | None = None,
    freshness_max_hours: float = 48.0,
) -> dict[str, Any]:
    finite_target_rows = 0
    complete_lineage_rows = 0
    fresh_lineage_rows = 0
    stale_lineage_rows = 0
    unparseable_retrieved_at_rows = 0
    retrieved_ages_hours: list[float] = []
    source_counts: Counter[str] = Counter()
    as_of_status_counts: Counter[str] = Counter()
    reference = _parse_utc_timestamp(reference_time)

    for record in records:
        try:
            target = float(record.get("targetMeanPrice"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(target) or target <= 0:
            continue

        finite_target_rows += 1
        source = str(record.get("targetMeanPriceSource") or "").strip()
        retrieved_at = str(record.get("targetMeanPriceRetrievedAt") or "").strip()
        as_of_status = str(record.get("targetMeanPriceAsOfStatus") or "").strip()
        if source:
            source_counts[source] += 1
        if as_of_status:
            as_of_status_counts[as_of_status] += 1
        if source and retrieved_at and as_of_status:
            complete_lineage_rows += 1
            if reference is not None:
                retrieved = _parse_utc_timestamp(retrieved_at)
                if retrieved is None:
                    unparseable_retrieved_at_rows += 1
                else:
                    age_hours = max(0.0, (reference - retrieved).total_seconds() / 3600)
                    retrieved_ages_hours.append(age_hours)
                    if age_hours <= freshness_max_hours:
                        fresh_lineage_rows += 1
                    else:
                        stale_lineage_rows += 1

    missing_lineage_rows = finite_target_rows - complete_lineage_rows
    return {
        "overall": (
            "no_finite_vendor_targets"
            if finite_target_rows == 0
            else "fail_incomplete_lineage"
            if missing_lineage_rows > 0
            else "warn_stale_lineage"
            if reference is not None and (stale_lineage_rows > 0 or unparseable_retrieved_at_rows > 0)
            else "pass_complete_fresh_lineage"
            if reference is not None
            else "pass_complete_lineage"
        ),
        "finiteTargetRows": finite_target_rows,
        "completeLineageRows": complete_lineage_rows,
        "missingLineageRows": missing_lineage_rows,
        "freshLineageRows": fresh_lineage_rows,
        "staleLineageRows": stale_lineage_rows,
        "unparseableRetrievedAtRows": unparseable_retrieved_at_rows,
        "freshnessMaxHours": freshness_max_hours if reference is not None else None,
        "maxRetrievedAgeHours": round(max(retrieved_ages_hours), 2) if retrieved_ages_hours else None,
        "lineageCoveragePct": round((complete_lineage_rows / finite_target_rows) * 100, 1)
        if finite_target_rows
        else 0.0,
        "sourceCounts": dict(sorted(source_counts.items())),
        "asOfStatusCounts": dict(sorted(as_of_status_counts.items())),
    }
