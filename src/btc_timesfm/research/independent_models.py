"""Independent forecasting model families for ensemble-diversity research.

These are deliberately lightweight NumPy-only forecasters that share the
past-only engineered feature set and forecast schema of ``ridge_features``
(``diversified_model``), so they can be dropped into the same walk-forward
origins and scored identically. Their purpose is to test whether genuinely
different inductive biases add out-of-sample ensemble value, not to displace
the existing production members.

- ``gbdt_features`` — gradient-boosted shallow regression trees fit directly
  per horizon on the engineered feature vector, with bounded tree depth,
  estimator count and a learning rate.
- ``elasticnet_features`` — elastic-net (L1 + L2) linear return model fit by
  coordinate descent, an alternative regularizer to the ridge path.

Both models bound their extrapolation with a volatility-scaled return clip,
mirroring ``ridge_feature_forecast``'s return_limit guard, and are
research-only by default.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable

import numpy as np

from btc_timesfm.forecasting.diversified_model import (
    DEFAULT_MIN_TRAIN_SAMPLES,
    MAX_LAG,
    TARGET_HOURS,
    _feature_at,
    training_examples,
)

MODEL_NAMES = ("gbdt_features", "elasticnet_features")
PRODUCTION_FLAG = "BTC_ENABLE_INDEPENDENT_MODELS"
RESEARCH_ONLY_NOTE = (
    "gbdt_features and elasticnet_features are research candidates only and are "
    "never enabled in the production ensemble without explicit approval via the "
    "promotion-policy gate."
)

DEFAULT_GBDT_ESTIMATORS = int(os.getenv("BTC_GBDT_ESTIMATORS", "40"))
DEFAULT_GBDT_MAX_DEPTH = int(os.getenv("BTC_GBDT_MAX_DEPTH", "2"))
DEFAULT_GBDT_MIN_SAMPLES_LEAF = int(os.getenv("BTC_GBDT_MIN_SAMPLES_LEAF", "8"))
DEFAULT_GBDT_LEARNING_RATE = float(os.getenv("BTC_GBDT_LEARNING_RATE", "0.05"))

DEFAULT_ELASTICNET_ALPHA = float(os.getenv("BTC_ELASTICNET_ALPHA", "0.6"))
DEFAULT_ELASTICNET_L1_RATIO = float(os.getenv("BTC_ELASTICNET_L1_RATIO", "0.6"))
DEFAULT_ELASTICNET_ITERATIONS = int(os.getenv("BTC_ELASTICNET_ITERATIONS", "200"))
DEFAULT_ELASTICNET_TOL = float(os.getenv("BTC_ELASTICNET_TOL", "1e-4"))

ModelFactory = Callable[[Any], dict[str, dict[str, float]]]

# A regression tree is either a leaf value or (feature, threshold, left, right).
Tree = float | tuple[Any, Any, Any, Any]


def production_enabled() -> bool:
    """Return whether the independent model families are approved for production."""
    return os.getenv(PRODUCTION_FLAG, "false").strip().lower() in {"1", "true", "yes", "on"}


def _best_split(
    x: np.ndarray,
    y: np.ndarray,
    min_samples_leaf: int,
) -> tuple[int, float, float] | None:
    """Find the split minimizing residual squared error for shallow trees."""
    n = len(y)
    if n < 2 * min_samples_leaf:
        return None
    y_sum = float(np.sum(y))
    y_ss = float(np.sum(y * y))
    best: tuple[int, float, float] | None = None

    for feature in range(x.shape[1]):
        order = np.argsort(x[:, feature], kind="stable")
        col = x[order, feature]
        if col[0] >= col[-1] - 1e-12:
            continue
        y_sorted = y[order]
        cum_y = np.cumsum(y_sorted)
        cum_y2 = np.cumsum(y_sorted * y_sorted)

        ks: np.ndarray = np.arange(min_samples_leaf, n - min_samples_leaf + 1, dtype=int)
        left_sum: np.ndarray = cum_y[ks - 1]
        left_ss: np.ndarray = cum_y2[ks - 1] - left_sum * left_sum / ks
        right_n: np.ndarray = n - ks
        right_sum = y_sum - left_sum
        right_ss = y_ss - cum_y2[ks - 1] - right_sum * right_sum / right_n
        sse = left_ss + right_ss

        distinct = col[ks - 1] < col[ks] - 1e-12
        valid = distinct & (left_ss > -1e-9) & (right_ss > -1e-9) & np.isfinite(sse)
        if not bool(np.any(valid)):
            continue
        candidates = np.where(valid, sse, np.inf)
        best_here = int(np.argmin(candidates))
        sse_value = float(sse[best_here])
        if best is None or sse_value < best[2]:
            k = int(ks[best_here])
            threshold = float((col[k - 1] + col[k]) / 2.0)
            best = (feature, threshold, sse_value)
    return best


def _fit_tree(
    x: np.ndarray,
    y: np.ndarray,
    *,
    max_depth: int,
    min_samples_leaf: int,
) -> Tree:
    """Fit a least-squares regression tree with bounded depth."""
    if max_depth <= 0 or len(y) < 2 * min_samples_leaf:
        return float(np.mean(y))
    split = _best_split(x, y, min_samples_leaf)
    if split is None:
        return float(np.mean(y))
    feature, threshold, _ = split
    left_mask = x[:, feature] <= threshold
    if (
        int(np.count_nonzero(left_mask)) < min_samples_leaf
        or int(np.count_nonzero(~left_mask)) < min_samples_leaf
    ):
        return float(np.mean(y))
    left = _fit_tree(
        x[left_mask],
        y[left_mask],
        max_depth=max_depth - 1,
        min_samples_leaf=min_samples_leaf,
    )
    right = _fit_tree(
        x[~left_mask],
        y[~left_mask],
        max_depth=max_depth - 1,
        min_samples_leaf=min_samples_leaf,
    )
    return (feature, threshold, left, right)


def _predict_tree(tree: Tree, x: np.ndarray, indices: np.ndarray, out: np.ndarray) -> None:
    """Fill ``out`` for the rows selected by ``indices`` with tree predictions."""
    if isinstance(tree, float):
        out[indices] = tree
        return
    feature, threshold, left, right = tree
    selected = x[indices, feature] <= threshold
    _predict_tree(left, x, indices[selected], out)
    _predict_tree(right, x, indices[~selected], out)


def _tree_predict(tree: Tree, x: np.ndarray, count: int) -> np.ndarray:
    out: np.ndarray = np.empty(count, dtype=float)
    _predict_tree(tree, x, np.arange(count, dtype=int), out)
    return out


def _fit_gbdt(
    x: np.ndarray,
    y: np.ndarray,
    *,
    estimators: int,
    max_depth: int,
    min_samples_leaf: int,
    learning_rate: float,
) -> tuple[list[Tree], float, float]:
    """Fit a gradient-boosted ensemble of shallow regression trees."""
    base_prediction = float(np.mean(y))
    prediction: np.ndarray = np.full(len(y), base_prediction, dtype=float)
    trees: list[Tree] = []
    for _ in range(estimators):
        residual = y - prediction
        tree = _fit_tree(
            x,
            residual,
            max_depth=max_depth,
            min_samples_leaf=min_samples_leaf,
        )
        prediction += learning_rate * _tree_predict(tree, x, len(y))
        trees.append(tree)
    return trees, learning_rate, base_prediction


def _predict_gbdt(
    trees: list[Tree],
    learning_rate: float,
    base_prediction: float,
    x: np.ndarray,
    count: int,
) -> np.ndarray:
    prediction: np.ndarray = np.full(count, base_prediction, dtype=float)
    for tree in trees:
        prediction += learning_rate * _tree_predict(tree, x, count)
    return prediction


def _fit_predict_gbdt(
    x: np.ndarray,
    y: np.ndarray,
    latest_features: np.ndarray,
    *,
    estimators: int,
    max_depth: int,
    min_samples_leaf: int,
    learning_rate: float,
) -> float:
    """Fit a GBDT on the continuous engineered features and predict one origin."""
    if estimators < 1 or learning_rate <= 0 or not math.isfinite(learning_rate):
        raise ValueError("gbdt estimators must be >= 1 and learning_rate must be positive")
    latest = np.asarray(latest_features[1:], dtype=float).reshape(1, -1)
    trees, rate, base = _fit_gbdt(
        x[:, 1:],
        y,
        estimators=estimators,
        max_depth=max_depth,
        min_samples_leaf=min_samples_leaf,
        learning_rate=learning_rate,
    )
    return float(_predict_gbdt(trees, rate, base, latest, 1)[0])


def _soft_threshold(value: float, threshold: float) -> float:
    if abs(value) <= threshold:
        return 0.0
    return float(value - math.copysign(threshold, value))


def _fit_predict_elasticnet(
    x: np.ndarray,
    y: np.ndarray,
    latest_features: np.ndarray,
    *,
    alpha: float,
    l1_ratio: float,
    max_iter: int,
    tol: float,
) -> float:
    """Fit an elastic-net linear model by coordinate descent and predict one origin."""
    if alpha < 0 or not 0.0 <= l1_ratio <= 1.0 or max_iter < 1 or tol <= 0:
        raise ValueError("invalid elastic-net hyperparameters")
    intercept = x[:, :1]
    continuous = x[:, 1:]
    means = np.mean(continuous, axis=0)
    stds = np.std(continuous, axis=0)
    stds = np.where(stds > 1e-9, stds, 1.0)
    scaled = np.concatenate([intercept, (continuous - means) / stds], axis=1)
    latest_scaled = np.concatenate([latest_features[:1], (latest_features[1:] - means) / stds])

    gram_diag = np.sum(scaled * scaled, axis=0)
    beta = np.zeros(scaled.shape[1], dtype=float)
    beta[0] = float(np.mean(y))
    residual = y - scaled @ beta
    shrink = alpha * l1_ratio
    l2 = alpha * (1.0 - l1_ratio)

    for _ in range(max_iter):
        max_change = 0.0
        for j in range(scaled.shape[1]):
            gram = float(gram_diag[j])
            rho = float(scaled[:, j] @ residual) + beta[j] * gram
            if j == 0:
                new_value = rho / gram if gram > 1e-12 else 0.0
            else:
                denominator = gram + l2
                new_value = (
                    _soft_threshold(rho, shrink) / denominator if denominator > 1e-12 else 0.0
                )
            new_value = new_value if math.isfinite(new_value) else 0.0
            delta = new_value - beta[j]
            if abs(delta) < 1e-12:
                continue
            beta[j] = new_value
            residual -= delta * scaled[:, j]
            max_change = max(max_change, float(abs(delta)))
        if max_change < tol:
            break
    return float(latest_scaled @ beta)


def _forecast_wrapper(
    data: Any,
    predict_function: Callable[..., float],
    *,
    min_train_samples: int,
    model_label: str,
    predict_kwargs: dict[str, Any],
) -> dict[str, dict[str, float]]:
    closes = np.asarray(data.closes, dtype=float)
    if len(closes) <= MAX_LAG + max(TARGET_HOURS):
        raise ValueError(f"market window is too short for {model_label} forecasting")
    current_price = float(closes[-1])
    latest_features = _feature_at(data, len(closes) - 1)
    recent_returns = np.diff(np.log(closes[-73:]))
    recent_volatility = max(float(np.std(recent_returns)), 1e-5)

    result: dict[str, dict[str, float]] = {}
    for horizon in TARGET_HOURS:
        x, y, _ = training_examples(data, horizon, min_train_samples=min_train_samples)
        predicted_return = predict_function(x, y, latest_features, **predict_kwargs)
        # Same volatility-scaled extrapolation guard as ridge_feature_forecast.
        return_limit = max(0.005, 4.0 * recent_volatility * math.sqrt(horizon))
        predicted_return = float(np.clip(predicted_return, -return_limit, return_limit))
        predicted_price = current_price * math.exp(predicted_return)
        result[f"{horizon}h"] = {
            "price_usd": predicted_price,
            "predicted_log_return": predicted_return,
            "training_samples": float(len(y)),
        }
    return result


def gbdt_feature_forecast(
    data: Any,
    *,
    estimators: int = DEFAULT_GBDT_ESTIMATORS,
    max_depth: int = DEFAULT_GBDT_MAX_DEPTH,
    min_samples_leaf: int = DEFAULT_GBDT_MIN_SAMPLES_LEAF,
    learning_rate: float = DEFAULT_GBDT_LEARNING_RATE,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
) -> dict[str, dict[str, float]]:
    """Forecast 2h/4h/8h/16h prices with per-horizon gradient-boosted trees."""
    return _forecast_wrapper(
        data,
        lambda x, y, features, **kwargs: _fit_predict_gbdt(x, y, features, **kwargs),
        min_train_samples=min_train_samples,
        model_label="gbdt_features",
        predict_kwargs={
            "estimators": estimators,
            "max_depth": max_depth,
            "min_samples_leaf": min_samples_leaf,
            "learning_rate": learning_rate,
        },
    )


def elasticnet_feature_forecast(
    data: Any,
    *,
    alpha: float = DEFAULT_ELASTICNET_ALPHA,
    l1_ratio: float = DEFAULT_ELASTICNET_L1_RATIO,
    max_iter: int = DEFAULT_ELASTICNET_ITERATIONS,
    tol: float = DEFAULT_ELASTICNET_TOL,
    min_train_samples: int = DEFAULT_MIN_TRAIN_SAMPLES,
) -> dict[str, dict[str, float]]:
    """Forecast 2h/4h/8h/16h prices with per-horizon elastic-net regressions."""
    return _forecast_wrapper(
        data,
        lambda x, y, features, **kwargs: _fit_predict_elasticnet(x, y, features, **kwargs),
        min_train_samples=min_train_samples,
        model_label="elasticnet_features",
        predict_kwargs={
            "alpha": alpha,
            "l1_ratio": l1_ratio,
            "max_iter": max_iter,
            "tol": tol,
        },
    )


MODEL_FORECASTERS: dict[str, ModelFactory] = {
    "gbdt_features": gbdt_feature_forecast,
    "elasticnet_features": elasticnet_feature_forecast,
}
