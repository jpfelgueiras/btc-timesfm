"""Research-only validation for the no-cross-horizon-envelope candidate."""

from __future__ import annotations

import math
from typing import Any


def preserve_valid_marginals(predictions: dict[str, Any]) -> dict[str, Any]:
    """Return independent marginal forecasts unchanged after within-horizon checks.

    This candidate intentionally does not require q10/q90 bounds to be
    monotone across forecast horizons. It still rejects malformed marginal
    distributions. It is not connected to production publication.
    """
    result = {
        key: dict(value) if isinstance(value, dict) else value for key, value in predictions.items()
    }
    for horizon, item in result.items():
        if not isinstance(item, dict):
            continue
        try:
            q10 = float(item["q10_usd"])
            q50 = float(item["q50_usd"])
            q90 = float(item["q90_usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{horizon} is missing finite q10/q50/q90 quantiles") from exc
        if not all(math.isfinite(value) for value in (q10, q50, q90)):
            raise ValueError(f"{horizon} quantiles must be finite")
        if not q10 <= q50 <= q90:
            raise ValueError(f"{horizon} has crossed marginal quantiles")
    return result
