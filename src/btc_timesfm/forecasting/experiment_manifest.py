"""Reproducibility manifests for production forecasts and research runs."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import random
import subprocess
from datetime import datetime, timezone
from typing import Any

import numpy as np

from btc_timesfm.forecasting.adaptive_weighting import (
    COVERAGE_PENALTY,
    DEFAULT_HISTORY_LIMIT,
    MIN_COVERAGE_SAMPLES,
    TARGET_INTERVAL_COVERAGE,
)
from btc_timesfm.forecasting.forecast_engine import (
    ADAPTIVE_DIRECTION_REWARD,
    ADAPTIVE_FULL_SAMPLES,
    ADAPTIVE_MAE_LAMBDA,
    ADAPTIVE_MAX_BLEND,
    ADAPTIVE_MAX_WEIGHT,
    ADAPTIVE_MIN_SAMPLES,
    ADAPTIVE_MIN_WEIGHT,
    CONTEXT_WINDOWS,
    INTERVAL_MINUTES,
    MODEL_ID,
    PAIR,
    PERSISTENCE_FALLBACK_BOOST,
    TARGET_HOURS,
)


MANIFEST_VERSION = 1
DEFAULT_SEED = 0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _package_version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "not-installed"


def _git_state() -> dict[str, Any]:
    env_sha = os.getenv("GITHUB_SHA") or os.getenv("GIT_COMMIT")
    sha = env_sha
    if not sha:
        try:
            sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            sha = "unknown"

    dirty: bool | None = None
    try:
        status = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        dirty = bool(status.strip())
    except (OSError, subprocess.CalledProcessError):
        pass
    return {"git_sha": sha, "dirty": dirty}


def seed_everything(seed: int = DEFAULT_SEED) -> dict[str, int | None]:
    """Apply and return deterministic seeds used by the project runtime."""
    random.seed(seed)
    np.random.seed(seed)
    torch_seed: int | None = None
    try:
        import torch

        torch.manual_seed(seed)
        torch_seed = seed
    except ImportError:
        pass
    return {"python": seed, "numpy": seed, "torch": torch_seed}


def market_data_identity(data: Any) -> dict[str, Any]:
    """Return exact window metadata plus a stable digest of OHLCV input bytes."""
    if not getattr(data, "timestamps", None):
        raise ValueError("market data must contain timestamps")

    digest = hashlib.sha256()
    timestamps = [int(value) for value in data.timestamps]
    digest.update(np.asarray(timestamps, dtype="<i8").tobytes())
    for name in ("opens", "highs", "lows", "closes", "volumes"):
        values = np.asarray(getattr(data, name), dtype="<f8")
        digest.update(name.encode("ascii"))
        digest.update(values.tobytes())

    return {
        "candle_count": len(timestamps),
        "first_candle_at": datetime.fromtimestamp(timestamps[0], tz=timezone.utc).isoformat(),
        "last_candle_at": datetime.fromtimestamp(timestamps[-1], tz=timezone.utc).isoformat(),
        "ohlcv_sha256": digest.hexdigest(),
    }


def forecast_configuration(model_names: list[str] | None = None) -> dict[str, Any]:
    """Capture model/ensemble settings that materially affect forecast behavior."""
    return {
        "model_id": MODEL_ID,
        "target_hours": list(TARGET_HOURS),
        "context_windows_hours": list(CONTEXT_WINDOWS),
        "interval_minutes": INTERVAL_MINUTES,
        "kraken_pair": PAIR,
        "model_names": sorted(model_names or []),
        "adaptive_weighting": {
            "history_limit": DEFAULT_HISTORY_LIMIT,
            "min_samples": ADAPTIVE_MIN_SAMPLES,
            "full_samples": ADAPTIVE_FULL_SAMPLES,
            "max_blend": ADAPTIVE_MAX_BLEND,
            "min_weight": ADAPTIVE_MIN_WEIGHT,
            "max_weight": ADAPTIVE_MAX_WEIGHT,
            "mae_lambda": ADAPTIVE_MAE_LAMBDA,
            "direction_reward": ADAPTIVE_DIRECTION_REWARD,
            "persistence_fallback_boost": PERSISTENCE_FALLBACK_BOOST,
            "target_interval_coverage": TARGET_INTERVAL_COVERAGE,
            "coverage_penalty": COVERAGE_PENALTY,
            "min_coverage_samples": MIN_COVERAGE_SAMPLES,
        },
    }


def build_experiment_manifest(
    *,
    run_type: str,
    data: Any,
    data_source: str,
    data_pair: str,
    run_parameters: dict[str, Any] | None = None,
    model_names: list[str] | None = None,
    feature_set_version: str | None = None,
    seed: int = DEFAULT_SEED,
    created_at: datetime | None = None,
    git_sha: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Build a versioned manifest with stable config/data fingerprints."""
    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)

    git = _git_state()
    if git_sha is not None:
        git["git_sha"] = git_sha

    data_identity = {
        "source": data_source,
        "pair": data_pair,
        **market_data_identity(data),
    }
    dependencies: dict[str, str] = {
        "timesfm": _package_version("timesfm"),
        "numpy": np.__version__,
        "requests": _package_version("requests"),
    }
    configuration: dict[str, Any] = {
        "forecast": forecast_configuration(model_names),
        "run_parameters": run_parameters or {},
        "dependencies": dependencies,
        "feature_set_version": feature_set_version,
        "seed": seed,
    }
    configuration_hash = _sha256(configuration)
    configuration_id = f"cfg-{configuration_hash[:20]}"
    data_id = f"data-{data_identity['ohlcv_sha256'][:20]}"
    if run_id is None:
        stamp = created.strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{run_type}-{stamp}-{configuration_hash[:8]}-{data_identity['ohlcv_sha256'][:8]}"

    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "run_type": run_type,
        "created_at": created.isoformat(),
        "configuration_id": configuration_id,
        "data_id": data_id,
        "code": git,
        "model": {
            "id": MODEL_ID,
            "package": "timesfm",
            "package_version": dependencies["timesfm"],
        },
        "configuration": configuration,
        "data": data_identity,
        "seeds": {"python": seed, "numpy": seed, "torch": seed},
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }
