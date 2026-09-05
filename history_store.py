#!/usr/bin/env python3
"""Durable forecast-history storage and analysis helpers.

The production workflow keeps this SQLite database as a compressed GitHub
Release asset. Forecasts are immutable by logical key (origin, model, horizon),
so manual reruns cannot rewrite the prediction that was first observed. Outcome
fields are filled later when the exact target Kraken candle is available.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, TextIO

from history_migrations import (
    CURRENT_SCHEMA_VERSION,
    migrate_database,
    schema_diagnostics,
    validate_database,
)


SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
DEFAULT_DB_PATH = Path(".state/forecast_history.sqlite")
ENSEMBLE_MODEL = "ensemble"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _direction(value: float, epsilon: float = 1e-9) -> int:
    return 1 if value > epsilon else -1 if value < -epsilon else 0


def _horizon_hours(key: str) -> int | None:
    if not isinstance(key, str) or not key.endswith("h"):
        return None
    try:
        value = int(key[:-1])
    except ValueError:
        return None
    return value if value > 0 else None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_weight(snapshot: dict[str, Any], model_name: str, horizon: str) -> float | None:
    weights = snapshot.get("model_weights")
    if not isinstance(weights, dict):
        return None

    # Current format: model_weights.<horizon>.<model>.
    horizon_weights = weights.get(horizon)
    if isinstance(horizon_weights, dict):
        return _optional_float(horizon_weights.get(model_name))

    # Older static-ensemble snapshots stored one global model-weight mapping.
    return _optional_float(weights.get(model_name))


def _prediction_rows(snapshot: dict[str, Any]) -> Iterable[dict[str, Any]]:
    origin = _parse_timestamp(snapshot["latest_close_at"])
    source_price = float(snapshot["latest_close_usd"])

    ensemble_predictions = snapshot.get("predictions", {})
    if isinstance(ensemble_predictions, dict):
        for horizon, item in ensemble_predictions.items():
            hour = _horizon_hours(horizon)
            if hour is None or not isinstance(item, dict) or "price_usd" not in item:
                continue
            predicted = float(item["price_usd"])
            yield {
                "model_name": ENSEMBLE_MODEL,
                "horizon_hours": hour,
                "target_at": _iso(origin + timedelta(hours=hour)),
                "predicted_price_usd": predicted,
                "predicted_change_pct": float(
                    item.get("change_pct", (predicted / source_price - 1.0) * 100.0)
                ),
                "q10_usd": _optional_float(item.get("q10_usd")),
                "q50_usd": _optional_float(item.get("q50_usd")),
                "q90_usd": _optional_float(item.get("q90_usd")),
                "model_agreement": _optional_float(item.get("model_agreement")),
                "ensemble_weight": None,
            }

    model_predictions = snapshot.get("model_predictions", {})
    if not isinstance(model_predictions, dict):
        return

    for model_name, horizons in model_predictions.items():
        if not isinstance(model_name, str) or not isinstance(horizons, dict):
            continue
        for horizon, item in horizons.items():
            hour = _horizon_hours(horizon)
            if hour is None or not isinstance(item, dict) or "price_usd" not in item:
                continue
            predicted = float(item["price_usd"])
            yield {
                "model_name": model_name,
                "horizon_hours": hour,
                "target_at": _iso(origin + timedelta(hours=hour)),
                "predicted_price_usd": predicted,
                "predicted_change_pct": (predicted / source_price - 1.0) * 100.0,
                "q10_usd": _optional_float(item.get("q10_usd")),
                "q50_usd": _optional_float(item.get("q50_usd")),
                "q90_usd": _optional_float(item.get("q90_usd")),
                "model_agreement": None,
                "ensemble_weight": _model_weight(snapshot, model_name, horizon),
            }


class ForecastHistoryStore:
    """SQLite-backed append-safe store for production forecasts and outcomes."""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = DELETE")
        return connection

    def _initialize(self) -> None:
        migrate_database(self.path)

    def ingest_snapshot(
        self,
        snapshot: dict[str, Any],
        actual_by_timestamp: dict[int, float] | None = None,
    ) -> dict[str, int]:
        """Insert one logical forecast without rewriting an existing prediction.

        The first prediction for a given (origin, model, horizon) wins. A manual
        rerun of the same source candle only updates last_seen_at and can enrich
        missing outcomes; it cannot silently alter the historical forecast.
        """
        origin = _parse_timestamp(snapshot["latest_close_at"])
        origin_at = _iso(origin)
        generated_at = _iso(
            _parse_timestamp(str(snapshot.get("generated_at") or snapshot["latest_close_at"]))
        )
        source_price = float(snapshot["latest_close_usd"])
        now = _iso(_utc_now())
        market_features = snapshot.get("market_features", {})
        if not isinstance(market_features, dict):
            market_features = {}
        experiment_manifest = snapshot.get("experiment_manifest")
        if not isinstance(experiment_manifest, dict):
            experiment_manifest = {}
        experiment_run_id = experiment_manifest.get("run_id")
        configuration_id = experiment_manifest.get("configuration_id")
        experiment_manifest_json = (
            json.dumps(experiment_manifest, sort_keys=True, separators=(",", ":"))
            if experiment_manifest
            else None
        )

        prediction_rows = list(_prediction_rows(snapshot))
        inserted_origins = 0
        inserted_predictions = 0

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO forecast_origins(
                    origin_at, generated_at, source_name, pair, source_price_usd,
                    regime, market_features_json, first_seen_at, last_seen_at,
                    experiment_run_id, configuration_id, experiment_manifest_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    origin_at,
                    generated_at,
                    snapshot.get("source"),
                    snapshot.get("pair"),
                    source_price,
                    snapshot.get("regime"),
                    json.dumps(market_features, sort_keys=True, separators=(",", ":")),
                    now,
                    now,
                    experiment_run_id,
                    configuration_id,
                    experiment_manifest_json,
                ),
            )
            inserted_origins += cursor.rowcount
            connection.execute(
                "UPDATE forecast_origins SET last_seen_at = ? WHERE origin_at = ?",
                (now, origin_at),
            )

            for row in prediction_rows:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO forecast_predictions(
                        origin_at, model_name, horizon_hours, target_at,
                        predicted_price_usd, predicted_change_pct,
                        q10_usd, q50_usd, q90_usd,
                        model_agreement, ensemble_weight
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        origin_at,
                        row["model_name"],
                        row["horizon_hours"],
                        row["target_at"],
                        row["predicted_price_usd"],
                        row["predicted_change_pct"],
                        row["q10_usd"],
                        row["q50_usd"],
                        row["q90_usd"],
                        row["model_agreement"],
                        row["ensemble_weight"],
                    ),
                )
                inserted_predictions += cursor.rowcount

        matured = self.enrich_outcomes(actual_by_timestamp or {})
        return {
            "origins_inserted": inserted_origins,
            "predictions_inserted": inserted_predictions,
            "outcomes_matured": matured,
        }

    def ingest_snapshots(
        self,
        snapshots: Iterable[dict[str, Any]],
        actual_by_timestamp: dict[int, float] | None = None,
    ) -> dict[str, int]:
        totals = {"origins_inserted": 0, "predictions_inserted": 0, "outcomes_matured": 0}
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                continue
            try:
                result = self.ingest_snapshot(snapshot)
            except (KeyError, TypeError, ValueError):
                continue
            totals["origins_inserted"] += result["origins_inserted"]
            totals["predictions_inserted"] += result["predictions_inserted"]
        totals["outcomes_matured"] = self.enrich_outcomes(actual_by_timestamp or {})
        return totals

    def enrich_outcomes(self, actual_by_timestamp: dict[int, float]) -> int:
        """Fill outcomes for every pending row whose exact target candle exists."""
        if not actual_by_timestamp:
            return 0

        matured = 0
        now = _iso(_utc_now())
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT p.origin_at, p.model_name, p.horizon_hours, p.target_at,
                       p.predicted_price_usd, p.q10_usd, p.q90_usd,
                       o.source_price_usd
                FROM forecast_predictions AS p
                JOIN forecast_origins AS o USING(origin_at)
                WHERE p.actual_target_price_usd IS NULL
                ORDER BY p.target_at
                """
            ).fetchall()

            for row in rows:
                target_timestamp = int(_parse_timestamp(row["target_at"]).timestamp())
                actual = actual_by_timestamp.get(target_timestamp)
                if actual is None or float(actual) <= 0:
                    continue
                actual = float(actual)
                predicted = float(row["predicted_price_usd"])
                source_price = float(row["source_price_usd"])
                error = predicted - actual
                q10 = _optional_float(row["q10_usd"])
                q90 = _optional_float(row["q90_usd"])
                within_interval = (
                    int(q10 <= actual <= q90) if q10 is not None and q90 is not None else None
                )

                cursor = connection.execute(
                    """
                    UPDATE forecast_predictions
                    SET actual_target_price_usd = ?,
                        absolute_error_usd = ?,
                        absolute_error_pct = ?,
                        signed_error_pct = ?,
                        actual_change_pct = ?,
                        direction_correct = ?,
                        within_q10_q90 = ?,
                        matured_at = ?
                    WHERE origin_at = ? AND model_name = ? AND horizon_hours = ?
                      AND actual_target_price_usd IS NULL
                    """,
                    (
                        actual,
                        abs(error),
                        abs(error) / actual * 100.0,
                        error / actual * 100.0,
                        (actual / source_price - 1.0) * 100.0,
                        int(
                            _direction(predicted - source_price)
                            == _direction(actual - source_price)
                        ),
                        within_interval,
                        now,
                        row["origin_at"],
                        row["model_name"],
                        row["horizon_hours"],
                    ),
                )
                matured += cursor.rowcount
        return matured

    def load_snapshots(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Reconstruct forecast snapshots for adaptive weighting/calibration code."""
        with self._connect() as connection:
            if limit is None:
                origins = connection.execute(
                    "SELECT * FROM forecast_origins ORDER BY origin_at"
                ).fetchall()
            else:
                origins = connection.execute(
                    "SELECT * FROM forecast_origins ORDER BY origin_at DESC LIMIT ?",
                    (max(0, int(limit)),),
                ).fetchall()[::-1]

            snapshots: list[dict[str, Any]] = []
            for origin in origins:
                try:
                    market_features = json.loads(origin["market_features_json"])
                except (json.JSONDecodeError, TypeError):
                    market_features = {}
                experiment_manifest: dict[str, Any] | None = None
                if origin["experiment_manifest_json"]:
                    try:
                        parsed_manifest = json.loads(origin["experiment_manifest_json"])
                        if isinstance(parsed_manifest, dict):
                            experiment_manifest = parsed_manifest
                    except (json.JSONDecodeError, TypeError):
                        pass
                snapshot: dict[str, Any] = {
                    "generated_at": origin["generated_at"],
                    "latest_close_at": origin["origin_at"],
                    "latest_close_usd": float(origin["source_price_usd"]),
                    "source": origin["source_name"],
                    "pair": origin["pair"],
                    "regime": origin["regime"],
                    "market_features": market_features,
                    "experiment_manifest": experiment_manifest,
                    "model_weights": {},
                    "model_predictions": {},
                    "predictions": {},
                }

                predictions = connection.execute(
                    """
                    SELECT * FROM forecast_predictions
                    WHERE origin_at = ?
                    ORDER BY horizon_hours, model_name
                    """,
                    (origin["origin_at"],),
                ).fetchall()
                for row in predictions:
                    horizon = f"{int(row['horizon_hours'])}h"
                    item: dict[str, Any] = {
                        "price_usd": float(row["predicted_price_usd"]),
                    }
                    for column in ("q10_usd", "q50_usd", "q90_usd"):
                        if row[column] is not None:
                            item[column] = float(row[column])

                    if row["model_name"] == ENSEMBLE_MODEL:
                        item["change_pct"] = float(row["predicted_change_pct"])
                        if row["model_agreement"] is not None:
                            item["model_agreement"] = float(row["model_agreement"])
                        snapshot["predictions"][horizon] = item
                    else:
                        model_name = str(row["model_name"])
                        snapshot["model_predictions"].setdefault(model_name, {})[horizon] = item
                        if row["ensemble_weight"] is not None:
                            snapshot["model_weights"].setdefault(horizon, {})[model_name] = float(
                                row["ensemble_weight"]
                            )
                snapshots.append(snapshot)
            return snapshots

    def stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            origins = int(connection.execute("SELECT COUNT(*) FROM forecast_origins").fetchone()[0])
            predictions = int(
                connection.execute("SELECT COUNT(*) FROM forecast_predictions").fetchone()[0]
            )
            matured = int(
                connection.execute(
                    "SELECT COUNT(*) FROM forecast_predictions WHERE actual_target_price_usd IS NOT NULL"
                ).fetchone()[0]
            )
            first_last = connection.execute(
                "SELECT MIN(origin_at), MAX(origin_at) FROM forecast_origins"
            ).fetchone()
            drift_events = int(connection.execute("SELECT COUNT(*) FROM drift_events").fetchone()[0])
            latest_drift = connection.execute(
                "SELECT severity, evaluation_origin_at FROM drift_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
        diagnostics = schema_diagnostics(self.path)
        return {
            "schema_version": diagnostics["schema_version"],
            "supported_schema_version": diagnostics["supported_schema_version"],
            "applied_migrations": diagnostics["applied_migrations"],
            "origins": origins,
            "predictions": predictions,
            "matured_predictions": matured,
            "pending_predictions": predictions - matured,
            "drift_events": drift_events,
            "latest_drift_severity": latest_drift["severity"] if latest_drift is not None else None,
            "latest_drift_origin_at": (
                latest_drift["evaluation_origin_at"] if latest_drift is not None else None
            ),
            "first_origin_at": first_last[0],
            "latest_origin_at": first_last[1],
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def performance_summary(self) -> dict[str, Any]:
        """Return all-time matured metrics in the same shape used by forecast.json."""
        with self._connect() as connection:
            horizons = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT horizon_hours FROM forecast_predictions
                    WHERE actual_target_price_usd IS NOT NULL
                    ORDER BY horizon_hours
                    """
                ).fetchall()
            ]
            result: dict[str, Any] = {}
            for hour in horizons:
                metrics = connection.execute(
                    """
                    SELECT model_name,
                           COUNT(*) AS samples,
                           AVG(absolute_error_pct) AS mae_pct,
                           AVG(signed_error_pct) AS signed_error_pct,
                           AVG(direction_correct) AS direction_accuracy,
                           AVG(within_q10_q90) AS interval_coverage
                    FROM forecast_predictions
                    WHERE horizon_hours = ? AND actual_target_price_usd IS NOT NULL
                    GROUP BY model_name
                    ORDER BY model_name
                    """,
                    (hour,),
                ).fetchall()
                by_model = {str(row["model_name"]): row for row in metrics}
                ensemble = by_model.get(ENSEMBLE_MODEL)
                if ensemble is None:
                    continue
                models: dict[str, Any] = {}
                for name, row in by_model.items():
                    if name == ENSEMBLE_MODEL:
                        continue
                    models[name] = {
                        "samples": int(row["samples"]),
                        "mae_pct": round(float(row["mae_pct"]), 4),
                        "mean_signed_error_pct": round(float(row["signed_error_pct"]), 4),
                        "direction_accuracy": round(float(row["direction_accuracy"]), 4),
                    }
                result[f"{hour}h"] = {
                    "samples": int(ensemble["samples"]),
                    "mae_pct": round(float(ensemble["mae_pct"]), 4),
                    "mean_signed_error_pct": round(float(ensemble["signed_error_pct"]), 4),
                    "direction_accuracy": round(float(ensemble["direction_accuracy"]), 4),
                    "q10_q90_coverage": (
                        round(float(ensemble["interval_coverage"]), 4)
                        if ensemble["interval_coverage"] is not None
                        else None
                    ),
                    "models": models,
                }
            return result

    def load_drift_history(self) -> dict[str, list[dict[str, Any]]]:
        """Return only past observed features and matured prediction outcomes."""
        with self._connect() as connection:
            prediction_rows = connection.execute(
                """
                SELECT origin_at, model_name, horizon_hours, target_at,
                       absolute_error_pct, signed_error_pct, direction_correct, matured_at
                FROM forecast_predictions
                WHERE actual_target_price_usd IS NOT NULL
                ORDER BY target_at, model_name, horizon_hours
                """
            ).fetchall()
            origin_rows = connection.execute(
                """
                SELECT origin_at, market_features_json
                FROM forecast_origins
                ORDER BY origin_at
                """
            ).fetchall()

        features: list[dict[str, Any]] = []
        for row in origin_rows:
            try:
                parsed = json.loads(row["market_features_json"])
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            features.append(
                {
                    "origin_at": str(row["origin_at"]),
                    "market_features": parsed if isinstance(parsed, dict) else {},
                }
            )
        return {
            "prediction_rows": [dict(row) for row in prediction_rows],
            "feature_rows": features,
        }

    def record_drift_events(
        self, report: dict[str, Any], *, experiment_run_id: str | None = None
    ) -> int:
        """Persist warning/severe drift signals once per observed origin."""
        events = report.get("events")
        if not isinstance(events, list) or not events:
            return 0
        evaluated_at = str(report.get("evaluated_at") or _iso(_utc_now()))
        evaluation_origin_at = str(
            report.get("latest_observed_origin_at") or evaluated_at
        )
        created_at = _iso(_utc_now())
        inserted = 0
        with self._connect() as connection:
            for event in events:
                if not isinstance(event, dict):
                    continue
                severity = str(event.get("severity") or "none")
                if severity not in {"warning", "severe"}:
                    continue
                signal_key = str(event.get("signal_key") or "")
                if not signal_key:
                    continue
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO drift_events(
                        evaluation_origin_at, evaluated_at, experiment_run_id,
                        signal_key, kind, severity, model_name, horizon_hours,
                        feature_name, metrics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evaluation_origin_at,
                        evaluated_at,
                        experiment_run_id,
                        signal_key,
                        str(event.get("kind") or "unknown"),
                        severity,
                        event.get("model_name"),
                        event.get("horizon_hours"),
                        event.get("feature_name"),
                        json.dumps(event.get("metrics", {}), sort_keys=True, separators=(",", ":")),
                        created_at,
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    def recent_drift_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM drift_events
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(0, int(limit)),),
            ).fetchall()
        events: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                metrics = json.loads(item.pop("metrics_json"))
            except (json.JSONDecodeError, TypeError):
                metrics = {}
            item["metrics"] = metrics
            events.append(item)
        return events

    def verify(self) -> dict[str, Any]:
        return validate_database(self.path)

    def export_rows(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.generated_at,
                       o.origin_at,
                       o.source_name,
                       o.pair,
                       o.source_price_usd,
                       o.regime,
                       o.market_features_json,
                       o.experiment_run_id,
                       o.configuration_id,
                       o.experiment_manifest_json,
                       p.model_name,
                       p.horizon_hours,
                       p.target_at,
                       p.predicted_price_usd,
                       p.predicted_change_pct,
                       p.q10_usd,
                       p.q50_usd,
                       p.q90_usd,
                       p.model_agreement,
                       p.ensemble_weight,
                       p.actual_target_price_usd,
                       p.absolute_error_usd,
                       p.absolute_error_pct,
                       p.signed_error_pct,
                       p.actual_change_pct,
                       p.direction_correct,
                       p.within_q10_q90,
                       p.matured_at
                FROM forecast_predictions AS p
                JOIN forecast_origins AS o USING(origin_at)
                ORDER BY o.origin_at, p.horizon_hours, p.model_name
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def export(self, output: Path | str, format_name: str = "csv") -> int:
        rows = self.export_rows()
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if format_name == "csv":
            with output_path.open("w", encoding="utf-8", newline="") as handle:
                if rows:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
                    writer.writeheader()
                    writer.writerows(rows)
                else:
                    handle.write("")
        elif format_name == "jsonl":
            with output_path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
        else:
            raise ValueError(f"Unsupported export format: {format_name}")
        return len(rows)


def _load_state(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and isinstance(payload.get("forecasts"), list):
        return [item for item in payload["forecasts"] if isinstance(item, dict)]
    if isinstance(payload, dict) and "predictions" in payload:
        return [payload]
    raise ValueError("State file does not contain forecast snapshots")


def _write_json(data: Any, stream: TextIO = sys.stdout) -> None:
    json.dump(data, stream, indent=2, sort_keys=True)
    stream.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect/export the durable BTC forecast history")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("verify")
    subparsers.add_parser("stats")
    subparsers.add_parser("summary")

    ingest = subparsers.add_parser("ingest-state")
    ingest.add_argument("--state", type=Path, required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    export.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    store = ForecastHistoryStore(args.db)

    if args.command == "init":
        _write_json(store.stats())
    elif args.command == "verify":
        _write_json(store.verify())
    elif args.command == "stats":
        _write_json(store.stats())
    elif args.command == "summary":
        _write_json(store.performance_summary())
    elif args.command == "ingest-state":
        result = store.ingest_snapshots(_load_state(args.state))
        _write_json({**result, **store.stats()})
    elif args.command == "export":
        rows = store.export(args.output, args.format)
        _write_json({"rows": rows, "format": args.format, "output": str(args.output)})


if __name__ == "__main__":
    main()
