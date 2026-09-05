#!/usr/bin/env python3
"""Temporary source patch helper for issue #33. Removed before merge."""

from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one match, found {count}: {old[:80]!r}")
    file_path.write_text(text.replace(old, new, 1), encoding="utf-8")


# --- history schema v4: durable drift events ---------------------------------
replace_once("history_migrations.py", "CURRENT_SCHEMA_VERSION = 3", "CURRENT_SCHEMA_VERSION = 4")
replace_once(
    "history_migrations.py",
    "\n\nMIGRATIONS: tuple[Migration, ...] = (\n",
    '''\n\ndef _migration_4_add_drift_events(connection: sqlite3.Connection) -> None:\n    connection.execute(\n        """\n        CREATE TABLE IF NOT EXISTS drift_events (\n            id INTEGER PRIMARY KEY AUTOINCREMENT,\n            evaluation_origin_at TEXT NOT NULL,\n            evaluated_at TEXT NOT NULL,\n            experiment_run_id TEXT,\n            signal_key TEXT NOT NULL,\n            kind TEXT NOT NULL,\n            severity TEXT NOT NULL CHECK (severity IN ('warning', 'severe')),\n            model_name TEXT,\n            horizon_hours INTEGER,\n            feature_name TEXT,\n            metrics_json TEXT NOT NULL,\n            created_at TEXT NOT NULL,\n            UNIQUE(evaluation_origin_at, signal_key, severity)\n        )\n        """\n    )\n    connection.execute(\n        """\n        CREATE INDEX IF NOT EXISTS idx_drift_events_severity_time\n            ON drift_events(severity, evaluation_origin_at)\n        """\n    )\n    connection.execute(\n        """\n        CREATE INDEX IF NOT EXISTS idx_drift_events_signal_time\n            ON drift_events(signal_key, evaluation_origin_at)\n        """\n    )\n\n\nMIGRATIONS: tuple[Migration, ...] = (\n''',
)
replace_once(
    "history_migrations.py",
    '    Migration(3, "add_experiment_manifests", _migration_3_add_experiment_manifests),\n)',
    '    Migration(3, "add_experiment_manifests", _migration_3_add_experiment_manifests),\n'
    '    Migration(4, "add_drift_events", _migration_4_add_drift_events),\n)',
)
replace_once(
    "history_migrations.py",
    '''    if expected_version >= 2:\n        required_tables.add("schema_migrations")\n    missing = sorted(required_tables - tables)\n''',
    '''    if expected_version >= 2:\n        required_tables.add("schema_migrations")\n    if expected_version >= 4:\n        required_tables.add("drift_events")\n    missing = sorted(required_tables - tables)\n''',
)

# --- history store drift inputs/persistence -----------------------------------
replace_once(
    "history_store.py",
    '''            first_last = connection.execute(\n                "SELECT MIN(origin_at), MAX(origin_at) FROM forecast_origins"\n            ).fetchone()\n        diagnostics = schema_diagnostics(self.path)\n''',
    '''            first_last = connection.execute(\n                "SELECT MIN(origin_at), MAX(origin_at) FROM forecast_origins"\n            ).fetchone()\n            drift_events = int(connection.execute("SELECT COUNT(*) FROM drift_events").fetchone()[0])\n            latest_drift = connection.execute(\n                "SELECT severity, evaluation_origin_at FROM drift_events ORDER BY id DESC LIMIT 1"\n            ).fetchone()\n        diagnostics = schema_diagnostics(self.path)\n''',
)
replace_once(
    "history_store.py",
    '''            "pending_predictions": predictions - matured,\n            "first_origin_at": first_last[0],\n''',
    '''            "pending_predictions": predictions - matured,\n            "drift_events": drift_events,\n            "latest_drift_severity": latest_drift["severity"] if latest_drift is not None else None,\n            "latest_drift_origin_at": (\n                latest_drift["evaluation_origin_at"] if latest_drift is not None else None\n            ),\n            "first_origin_at": first_last[0],\n''',
)
replace_once(
    "history_store.py",
    '''    def verify(self) -> dict[str, Any]:\n        return validate_database(self.path)\n''',
    '''    def load_drift_history(self) -> dict[str, list[dict[str, Any]]]:\n        """Return only past observed features and matured prediction outcomes."""\n        with self._connect() as connection:\n            prediction_rows = connection.execute(\n                """\n                SELECT origin_at, model_name, horizon_hours, target_at,\n                       absolute_error_pct, signed_error_pct, direction_correct, matured_at\n                FROM forecast_predictions\n                WHERE actual_target_price_usd IS NOT NULL\n                ORDER BY target_at, model_name, horizon_hours\n                """\n            ).fetchall()\n            origin_rows = connection.execute(\n                """\n                SELECT origin_at, market_features_json\n                FROM forecast_origins\n                ORDER BY origin_at\n                """\n            ).fetchall()\n\n        features: list[dict[str, Any]] = []\n        for row in origin_rows:\n            try:\n                parsed = json.loads(row["market_features_json"])\n            except (json.JSONDecodeError, TypeError):\n                parsed = {}\n            features.append(\n                {\n                    "origin_at": str(row["origin_at"]),\n                    "market_features": parsed if isinstance(parsed, dict) else {},\n                }\n            )\n        return {\n            "prediction_rows": [dict(row) for row in prediction_rows],\n            "feature_rows": features,\n        }\n\n    def record_drift_events(\n        self, report: dict[str, Any], *, experiment_run_id: str | None = None\n    ) -> int:\n        """Persist warning/severe drift signals once per observed origin."""\n        events = report.get("events")\n        if not isinstance(events, list) or not events:\n            return 0\n        evaluated_at = str(report.get("evaluated_at") or _iso(_utc_now()))\n        evaluation_origin_at = str(\n            report.get("latest_observed_origin_at") or evaluated_at\n        )\n        created_at = _iso(_utc_now())\n        inserted = 0\n        with self._connect() as connection:\n            for event in events:\n                if not isinstance(event, dict):\n                    continue\n                severity = str(event.get("severity") or "none")\n                if severity not in {"warning", "severe"}:\n                    continue\n                signal_key = str(event.get("signal_key") or "")\n                if not signal_key:\n                    continue\n                cursor = connection.execute(\n                    """\n                    INSERT OR IGNORE INTO drift_events(\n                        evaluation_origin_at, evaluated_at, experiment_run_id,\n                        signal_key, kind, severity, model_name, horizon_hours,\n                        feature_name, metrics_json, created_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    """,\n                    (\n                        evaluation_origin_at,\n                        evaluated_at,\n                        experiment_run_id,\n                        signal_key,\n                        str(event.get("kind") or "unknown"),\n                        severity,\n                        event.get("model_name"),\n                        event.get("horizon_hours"),\n                        event.get("feature_name"),\n                        json.dumps(event.get("metrics", {}), sort_keys=True, separators=(",", ":")),\n                        created_at,\n                    ),\n                )\n                inserted += cursor.rowcount\n        return inserted\n\n    def recent_drift_events(self, limit: int = 50) -> list[dict[str, Any]]:\n        with self._connect() as connection:\n            rows = connection.execute(\n                """\n                SELECT * FROM drift_events\n                ORDER BY id DESC\n                LIMIT ?\n                """,\n                (max(0, int(limit)),),\n            ).fetchall()\n        events: list[dict[str, Any]] = []\n        for row in rows:\n            item = dict(row)\n            try:\n                metrics = json.loads(item.pop("metrics_json"))\n            except (json.JSONDecodeError, TypeError):\n                metrics = {}\n            item["metrics"] = metrics\n            events.append(item)\n        return events\n\n    def verify(self) -> dict[str, Any]:\n        return validate_database(self.path)\n''',
)

# --- adaptive weighting confidence control ------------------------------------
replace_once(
    "adaptive_weighting.py",
    '''    enabled: bool = True,\n    history_limit: int | None = None,\n) -> tuple[dict[str, float], dict[str, Any]]:\n''',
    '''    enabled: bool = True,\n    history_limit: int | None = None,\n    confidence: float = 1.0,\n) -> tuple[dict[str, float], dict[str, Any]]:\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''    limit = max(ADAPTIVE_MIN_SAMPLES, int(history_limit or DEFAULT_HISTORY_LIMIT))\n    prior = static_model_weights(model_names, regime)\n''',
    '''    limit = max(ADAPTIVE_MIN_SAMPLES, int(history_limit or DEFAULT_HISTORY_LIMIT))\n    adaptive_confidence = min(1.0, max(0.0, float(confidence)))\n    prior = static_model_weights(model_names, regime)\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''    else:\n        source = "insufficient_history"\n        selected = all_scores\n\n    metrics = {\n''',
    '''    else:\n        source = "insufficient_history"\n        selected = all_scores\n\n    if enabled and adaptive_confidence <= 0.0 and source != "insufficient_history":\n        source = "drift_fallback"\n\n    metrics = {\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''    if not enabled or source == "insufficient_history":\n        diagnostics = {\n''',
    '''    if not enabled or source in {"insufficient_history", "drift_fallback"}:\n        diagnostics = {\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''            "history_limit": limit,\n            "blend_factor": 0.0,\n''',
    '''            "history_limit": limit,\n            "adaptive_confidence": round(adaptive_confidence, 4),\n            "blend_factor": 0.0,\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''    blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress\n    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}\n''',
    '''    unscaled_blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress\n    blend = unscaled_blend * adaptive_confidence\n    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}\n''',
)
replace_once(
    "adaptive_weighting.py",
    '''        "history_limit": limit,\n        "blend_factor": round(blend, 4),\n''',
    '''        "history_limit": limit,\n        "adaptive_confidence": round(adaptive_confidence, 4),\n        "unscaled_blend_factor": round(unscaled_blend, 4),\n        "blend_factor": round(blend, 4),\n''',
)

# --- forecast engine carries adaptive confidence into weighting ----------------
replace_once(
    "forecast_engine.py",
    '''    actual_by_timestamp: dict[int, float],\n    enabled: bool = True,\n) -> tuple[dict[str, float], dict[str, Any]]:\n    """Blend static priors with recent out-of-sample model performance."""\n    prior = static_model_weights(model_names, regime)\n''',
    '''    actual_by_timestamp: dict[int, float],\n    enabled: bool = True,\n    confidence: float = 1.0,\n) -> tuple[dict[str, float], dict[str, Any]]:\n    """Blend static priors with recent out-of-sample model performance."""\n    adaptive_confidence = min(1.0, max(0.0, float(confidence)))\n    prior = static_model_weights(model_names, regime)\n''',
)
replace_once(
    "forecast_engine.py",
    '''    else:\n        source = "insufficient_history"\n        selected = all_scores\n\n    metrics = {\n''',
    '''    else:\n        source = "insufficient_history"\n        selected = all_scores\n\n    if enabled and adaptive_confidence <= 0.0 and source != "insufficient_history":\n        source = "drift_fallback"\n\n    metrics = {\n''',
)
replace_once(
    "forecast_engine.py",
    '''    if not enabled or source == "insufficient_history":\n        diagnostics = {\n''',
    '''    if not enabled or source in {"insufficient_history", "drift_fallback"}:\n        diagnostics = {\n''',
)
replace_once(
    "forecast_engine.py",
    '''            "horizon": f"{hour}h",\n            "regime": regime,\n            "blend_factor": 0.0,\n''',
    '''            "horizon": f"{hour}h",\n            "regime": regime,\n            "adaptive_confidence": round(adaptive_confidence, 4),\n            "blend_factor": 0.0,\n''',
)
replace_once(
    "forecast_engine.py",
    '''    blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress\n\n    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}\n''',
    '''    unscaled_blend = 0.25 + (ADAPTIVE_MAX_BLEND - 0.25) * progress\n    blend = unscaled_blend * adaptive_confidence\n\n    blended = {name: (1.0 - blend) * prior[name] + blend * adaptive[name] for name in model_names}\n''',
)
replace_once(
    "forecast_engine.py",
    '''        "horizon": f"{hour}h",\n        "regime": regime,\n        "blend_factor": round(blend, 4),\n''',
    '''        "horizon": f"{hour}h",\n        "regime": regime,\n        "adaptive_confidence": round(adaptive_confidence, 4),\n        "unscaled_blend_factor": round(unscaled_blend, 4),\n        "blend_factor": round(blend, 4),\n''',
)
replace_once(
    "forecast_engine.py",
    '''    actual_by_timestamp: dict[int, float],\n    adaptive_weights_enabled: bool = True,\n) -> tuple[\n''',
    '''    actual_by_timestamp: dict[int, float],\n    adaptive_weights_enabled: bool = True,\n    adaptive_confidence: float = 1.0,\n) -> tuple[\n''',
)
replace_once(
    "forecast_engine.py",
    '''            actual_by_timestamp,\n            enabled=adaptive_weights_enabled,\n        )\n''',
    '''            actual_by_timestamp,\n            enabled=adaptive_weights_enabled,\n            confidence=adaptive_confidence,\n        )\n''',
)
replace_once(
    "forecast_engine.py",
    '''    history: list[dict[str, Any]] | None = None,\n    adaptive_weights_enabled: bool = True,\n) -> dict[str, Any]:\n''',
    '''    history: list[dict[str, Any]] | None = None,\n    adaptive_weights_enabled: bool = True,\n    adaptive_confidence: float = 1.0,\n) -> dict[str, Any]:\n''',
)
replace_once(
    "forecast_engine.py",
    '''        actuals,\n        adaptive_weights_enabled=adaptive_weights_enabled,\n    )\n''',
    '''        actuals,\n        adaptive_weights_enabled=adaptive_weights_enabled,\n        adaptive_confidence=adaptive_confidence,\n    )\n''',
)
replace_once(
    "forecast_engine.py",
    '''        "regime": regime,\n        "model_weights": {\n''',
    '''        "regime": regime,\n        "adaptive_confidence": round(min(1.0, max(0.0, float(adaptive_confidence))), 4),\n        "model_weights": {\n''',
)

# --- production forecast drift evaluation/persistence --------------------------
replace_once(
    "btc_forecast.py",
    '''from experiment_manifest import build_experiment_manifest, seed_everything\nfrom forecast_engine import TARGET_HOURS, build_forecast, load_timesfm\n''',
    '''from drift_detection import evaluate_drift, persist_drift_report\nfrom experiment_manifest import build_experiment_manifest, seed_everything\nfrom forecast_engine import TARGET_HOURS, build_forecast, load_timesfm\n''',
)
replace_once(
    "btc_forecast.py",
    '''\ndef main() -> None:\n    seed_everything()\n''',
    '''\ndef evaluate_production_drift(store: ForecastHistoryStore, data: Any) -> dict[str, Any]:\n    """Evaluate only durable matured errors plus already observed market features."""\n    inputs = store.load_drift_history()\n    current_features = forecast_engine.market_features(data)\n    current_origin = datetime.fromtimestamp(data.timestamps[-1], tz=timezone.utc).isoformat()\n    return evaluate_drift(\n        inputs["prediction_rows"],\n        inputs["feature_rows"],\n        current_features=current_features,\n        current_origin_at=current_origin,\n    )\n\n\ndef main() -> None:\n    seed_everything()\n''',
)
replace_once(
    "btc_forecast.py",
    '''    if not summary:\n        # Local/first-run fallback before any matured durable records exist.\n        summary = performance_summary(history, data.closes, data.timestamps)\n\n    model = load_timesfm()\n    engine_output = build_forecast(model, data, history)\n''',
    '''    if not summary:\n        # Local/first-run fallback before any matured durable records exist.\n        summary = performance_summary(history, data.closes, data.timestamps)\n\n    drift_report = evaluate_production_drift(store, data)\n    adaptive_confidence = float(drift_report["adaptive_confidence"])\n\n    model = load_timesfm()\n    engine_output = build_forecast(\n        model, data, history, adaptive_confidence=adaptive_confidence\n    )\n''',
)
replace_once(
    "btc_forecast.py",
    '''    output: dict[str, Any] = {\n        "generated_at": generated_at.isoformat(),\n''',
    '''    persisted_drift_events = store.record_drift_events(\n        drift_report, experiment_run_id=str(experiment_manifest.get("run_id") or "") or None\n    )\n    drift_report["persisted_events"] = persisted_drift_events\n    persist_drift_report(drift_report)\n\n    output: dict[str, Any] = {\n        "generated_at": generated_at.isoformat(),\n''',
)
replace_once(
    "btc_forecast.py",
    '''        "experiment_manifest": experiment_manifest,\n        **engine_output,\n''',
    '''        "experiment_manifest": experiment_manifest,\n        "drift_detection": drift_report,\n        **engine_output,\n''',
)
replace_once(
    "btc_forecast.py",
    '''    print(f"\\nRegime: {output['regime']}")\n    print(f"Model weights: {output['model_weights']}")\n''',
    '''    print(f"\\nRegime: {output['regime']}")\n    print(\n        f"Drift: {drift_report['severity']} | adaptive confidence "\n        f"{drift_report['adaptive_confidence']:.2f}"\n    )\n    print(f"Model weights: {output['model_weights']}")\n''',
)

# --- structured observability for drift ---------------------------------------
replace_once(
    "observability.py",
    '''    "data_quality_events": 0,\n    "successful_posts": 0,\n''',
    '''    "data_quality_events": 0,\n    "drift_warnings": 0,\n    "drift_severe": 0,\n    "successful_posts": 0,\n''',
)
replace_once(
    "validated_entrypoints.py",
    '''    original_build_forecast = btc_forecast.build_forecast\n    original_manifest = btc_forecast.build_experiment_manifest\n''',
    '''    original_build_forecast = btc_forecast.build_forecast\n    original_drift = btc_forecast.evaluate_production_drift\n    original_manifest = btc_forecast.build_experiment_manifest\n''',
)
replace_once(
    "validated_entrypoints.py",
    '''    def manifest_observed(*args: Any, **kwargs: Any):\n        manifest = original_manifest(*args, **kwargs)\n''',
    '''    def drift_observed(*args: Any, **kwargs: Any):\n        with observer.stage("drift_detection"):\n            report = original_drift(*args, **kwargs)\n        severity = str(report.get("severity", "none"))\n        if severity == "warning":\n            observer.increment("drift_warnings")\n        elif severity == "severe":\n            observer.increment("drift_severe")\n        observer.event(\n            "drift_evaluated",\n            status="success",\n            severity=severity,\n            adaptive_confidence=report.get("adaptive_confidence"),\n            event_count=report.get("summary", {}).get("events"),\n            fallback_mode=report.get("fallback_mode"),\n        )\n        return report\n\n    def manifest_observed(*args: Any, **kwargs: Any):\n        manifest = original_manifest(*args, **kwargs)\n''',
)
replace_once(
    "validated_entrypoints.py",
    '''        def ingest_snapshot(self, *args: Any, **kwargs: Any):\n            with observer.stage("history_persistence"):\n                return super().ingest_snapshot(*args, **kwargs)\n\n        def verify(self, *args: Any, **kwargs: Any):\n''',
    '''        def ingest_snapshot(self, *args: Any, **kwargs: Any):\n            with observer.stage("history_persistence"):\n                return super().ingest_snapshot(*args, **kwargs)\n\n        def record_drift_events(self, *args: Any, **kwargs: Any):\n            with observer.stage("drift_persistence"):\n                return super().record_drift_events(*args, **kwargs)\n\n        def verify(self, *args: Any, **kwargs: Any):\n''',
)
replace_once(
    "validated_entrypoints.py",
    '''    btc_forecast.build_forecast = build_forecast_observed\n    btc_forecast.build_experiment_manifest = manifest_observed\n''',
    '''    btc_forecast.build_forecast = build_forecast_observed\n    btc_forecast.evaluate_production_drift = drift_observed\n    btc_forecast.build_experiment_manifest = manifest_observed\n''',
)

# --- migration test expectations ----------------------------------------------
path = Path("test_history_migrations.py")
text = path.read_text(encoding="utf-8")
old = "            [1, 2, 3],\n"
if text.count(old) != 2:
    raise SystemExit(f"test_history_migrations.py: expected two version-list matches, got {text.count(old)}")
path.write_text(text.replace(old, "            [1, 2, 3, 4],\n"), encoding="utf-8")

print("Issue #33 source patches applied")
