import math
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
