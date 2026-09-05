#!/usr/bin/env python3
"""One-time integration patch for issue #21."""

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one {label} anchor, found {count}")
    return text.replace(old, new, 1)


# Production forecast integration.
path = Path("btc_forecast.py")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from forecast_engine import TARGET_HOURS, build_forecast, load_timesfm\n",
    "from experiment_manifest import build_experiment_manifest, seed_everything\n"
    "from forecast_engine import TARGET_HOURS, build_forecast, load_timesfm\n",
    "btc_forecast imports",
)
text = replace_once(
    text,
    '        "market_data_provenance": output.get("market_data_provenance"),\n        "regime": output["regime"],',
    '        "market_data_provenance": output.get("market_data_provenance"),\n'
    '        "experiment_manifest": output.get("experiment_manifest"),\n'
    '        "regime": output["regime"],',
    "rolling manifest",
)
text = replace_once(
    text,
    '        json.dumps({"version": 3, "forecasts": deduplicated[-HISTORY_LIMIT:]}, indent=2) + "\\n"\n',
    '        json.dumps({"version": 4, "forecasts": deduplicated[-HISTORY_LIMIT:]}, indent=2) + "\\n"\n',
    "rolling state version",
)
text = replace_once(
    text,
    "def main() -> None:\n    selection = fetch_redundant_hourly(512)",
    "def main() -> None:\n    seed_everything()\n    selection = fetch_redundant_hourly(512)",
    "production seed",
)
text = replace_once(
    text,
    "    engine_output = build_forecast(model, data, history)\n    output: dict[str, Any] = {\n        \"generated_at\": datetime.now(timezone.utc).isoformat(),",
    "    engine_output = build_forecast(model, data, history)\n"
    "    generated_at = datetime.now(timezone.utc)\n"
    "    experiment_manifest = build_experiment_manifest(\n"
    "        run_type=\"production_forecast\",\n"
    "        data=data,\n"
    "        data_source=selection.source,\n"
    "        data_pair=selection.source_pair,\n"
    "        run_parameters={\"rolling_history_limit\": HISTORY_LIMIT},\n"
    "        model_names=sorted(engine_output.get(\"model_predictions\", {})),\n"
    "        created_at=generated_at,\n"
    "    )\n"
    "    output: dict[str, Any] = {\n"
    "        \"generated_at\": generated_at.isoformat(),",
    "production manifest construction",
)
text = replace_once(
    text,
    '        "market_data_provenance": {\n            "provider": selection.provider,\n            "fallback_used": selection.fallback_used,\n            "source_pair": selection.source_pair,\n            "comparison": selection.comparison,\n        },\n        **engine_output,',
    '        "market_data_provenance": {\n'
    '            "provider": selection.provider,\n'
    '            "fallback_used": selection.fallback_used,\n'
    '            "source_pair": selection.source_pair,\n'
    '            "comparison": selection.comparison,\n'
    '        },\n'
    '        "experiment_manifest": experiment_manifest,\n'
    '        **engine_output,',
    "production manifest output",
)
path.write_text(text, encoding="utf-8")


# Backtest integration.
path = Path("backtest.py")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from adaptive_weighting import DEFAULT_HISTORY_LIMIT, adaptive_model_weights\n",
    "from adaptive_weighting import DEFAULT_HISTORY_LIMIT, adaptive_model_weights\n"
    "from experiment_manifest import build_experiment_manifest, seed_everything\n",
    "backtest imports",
)
text = replace_once(
    text,
    "    args = parser.parse_args()\n\n    data = fetch_binance_history(args.days)",
    "    args = parser.parse_args()\n\n    seed_everything()\n    data = fetch_binance_history(args.days)",
    "backtest seed",
)
text = replace_once(
    text,
    '    report = {\n        "generated_at": datetime.now(timezone.utc).isoformat(),\n        "data_source": "Binance BTCUSDT 1h (historical proxy for BTC/USD)",',
    '    generated_at = datetime.now(timezone.utc)\n'
    '    data_source = "Binance BTCUSDT 1h (historical proxy for BTC/USD)"\n'
    '    experiment_manifest = build_experiment_manifest(\n'
    '        run_type="backtest",\n'
    '        data=data,\n'
    '        data_source=data_source,\n'
    '        data_pair="BTC/USDT",\n'
    '        run_parameters={\n'
    '            "days_requested": args.days,\n'
    '            "samples_requested": args.samples,\n'
    '            "adaptive_history_limit": DEFAULT_HISTORY_LIMIT,\n'
    '        },\n'
    '        model_names=sorted(samples[-1]["forecast"]["model_predictions"]) if samples else [],\n'
    '        created_at=generated_at,\n'
    '    )\n'
    '    report = {\n'
    '        "generated_at": generated_at.isoformat(),\n'
    '        "data_source": data_source,\n'
    '        "experiment_manifest": experiment_manifest,',
    "backtest manifest",
)
path.write_text(text, encoding="utf-8")


# Durable history migration v3.
path = Path("history_migrations.py")
text = path.read_text(encoding="utf-8")
text = replace_once(text, "CURRENT_SCHEMA_VERSION = 2", "CURRENT_SCHEMA_VERSION = 3", "schema version")
anchor = "\n\nMIGRATIONS: tuple[Migration, ...] = (\n"
migration = '''\n\ndef _migration_3_add_experiment_manifests(connection: sqlite3.Connection) -> None:\n    columns = {\n        str(row[1]) for row in connection.execute("PRAGMA table_info(forecast_origins)").fetchall()\n    }\n    additions = (\n        ("experiment_run_id", "TEXT"),\n        ("configuration_id", "TEXT"),\n        ("experiment_manifest_json", "TEXT"),\n    )\n    for name, sql_type in additions:\n        if name not in columns:\n            connection.execute(f"ALTER TABLE forecast_origins ADD COLUMN {name} {sql_type}")\n    connection.execute(\n        """\n        CREATE INDEX IF NOT EXISTS idx_forecast_origins_configuration\n            ON forecast_origins(configuration_id, origin_at)\n        """\n    )\n'''
text = replace_once(text, anchor, migration + anchor, "migration insertion")
text = replace_once(
    text,
    '    Migration(2, "add_migration_audit", _migration_2_add_migration_audit),\n)',
    '    Migration(2, "add_migration_audit", _migration_2_add_migration_audit),\n'
    '    Migration(3, "add_experiment_manifests", _migration_3_add_experiment_manifests),\n'
    ')',
    "migration registry",
)
path.write_text(text, encoding="utf-8")


# Durable history storage of production manifests.
path = Path("history_store.py")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '        market_features = snapshot.get("market_features", {})\n        if not isinstance(market_features, dict):\n            market_features = {}\n\n        prediction_rows = list(_prediction_rows(snapshot))',
    '        market_features = snapshot.get("market_features", {})\n'
    '        if not isinstance(market_features, dict):\n'
    '            market_features = {}\n'
    '        experiment_manifest = snapshot.get("experiment_manifest")\n'
    '        if not isinstance(experiment_manifest, dict):\n'
    '            experiment_manifest = {}\n'
    '        experiment_run_id = experiment_manifest.get("run_id")\n'
    '        configuration_id = experiment_manifest.get("configuration_id")\n'
    '        experiment_manifest_json = (\n'
    '            json.dumps(experiment_manifest, sort_keys=True, separators=(",", ":"))\n'
    '            if experiment_manifest\n'
    '            else None\n'
    '        )\n\n'
    '        prediction_rows = list(_prediction_rows(snapshot))',
    "store manifest variables",
)
text = replace_once(
    text,
    '                INSERT OR IGNORE INTO forecast_origins(\n                    origin_at, generated_at, source_name, pair, source_price_usd,\n                    regime, market_features_json, first_seen_at, last_seen_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
    '                INSERT OR IGNORE INTO forecast_origins(\n'
    '                    origin_at, generated_at, source_name, pair, source_price_usd,\n'
    '                    regime, market_features_json, first_seen_at, last_seen_at,\n'
    '                    experiment_run_id, configuration_id, experiment_manifest_json\n'
    '                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
    "origin insert columns",
)
text = replace_once(
    text,
    '                    now,\n                    now,\n                ),',
    '                    now,\n'
    '                    now,\n'
    '                    experiment_run_id,\n'
    '                    configuration_id,\n'
    '                    experiment_manifest_json,\n'
    '                ),',
    "origin insert values",
)
text = replace_once(
    text,
    '                snapshot: dict[str, Any] = {\n                    "generated_at": origin["generated_at"],',
    '                experiment_manifest: dict[str, Any] | None = None\n'
    '                if origin["experiment_manifest_json"]:\n'
    '                    try:\n'
    '                        parsed_manifest = json.loads(origin["experiment_manifest_json"])\n'
    '                        if isinstance(parsed_manifest, dict):\n'
    '                            experiment_manifest = parsed_manifest\n'
    '                    except (json.JSONDecodeError, TypeError):\n'
    '                        pass\n'
    '                snapshot: dict[str, Any] = {\n'
    '                    "generated_at": origin["generated_at"],',
    "load manifest parsing",
)
text = replace_once(
    text,
    '                    "market_features": market_features,\n                    "model_weights": {},',
    '                    "market_features": market_features,\n'
    '                    "experiment_manifest": experiment_manifest,\n'
    '                    "model_weights": {},',
    "load manifest field",
)
text = replace_once(
    text,
    '                       o.market_features_json,\n                       p.model_name,',
    '                       o.market_features_json,\n'
    '                       o.experiment_run_id,\n'
    '                       o.configuration_id,\n'
    '                       o.experiment_manifest_json,\n'
    '                       p.model_name,',
    "export manifest columns",
)
path.write_text(text, encoding="utf-8")


# Migration tests follow the current registry.
path = Path("test_history_migrations.py")
text = path.read_text(encoding="utf-8")
if text.count("[1, 2],") != 2:
    raise RuntimeError("Expected two migration-version assertions")
text = text.replace("[1, 2],", "[1, 2, 3],")
path.write_text(text, encoding="utf-8")


# History-store test proves manifest round-trip and export persistence.
path = Path("test_history_store.py")
text = path.read_text(encoding="utf-8")
anchor = "\n    def test_performance_summary_uses_all_matured_rows(self) -> None:\n"
test = '''\n    def test_experiment_manifest_is_first_write_wins_and_round_trips(self) -> None:\n        snapshot = make_snapshot(self.origin)\n        snapshot["experiment_manifest"] = {\n            "manifest_version": 1,\n            "run_id": "production_forecast-1",\n            "configuration_id": "cfg-abc",\n            "data_id": "data-123",\n        }\n        self.store.ingest_snapshot(snapshot)\n\n        rerun = make_snapshot(self.origin, generated_offset_minutes=20, ensemble_2h=150.0)\n        rerun["experiment_manifest"] = {\n            "manifest_version": 1,\n            "run_id": "production_forecast-2",\n            "configuration_id": "cfg-different",\n        }\n        self.store.ingest_snapshot(rerun)\n\n        loaded = self.store.load_snapshots()[0]["experiment_manifest"]\n        self.assertEqual(loaded["run_id"], "production_forecast-1")\n        self.assertEqual(loaded["configuration_id"], "cfg-abc")\n        row = self.store.export_rows()[0]\n        self.assertEqual(row["experiment_run_id"], "production_forecast-1")\n        self.assertEqual(row["configuration_id"], "cfg-abc")\n        self.assertEqual(json.loads(row["experiment_manifest_json"])["data_id"], "data-123")\n\n'''
text = replace_once(text, anchor, "\n" + test + "    def test_performance_summary_uses_all_matured_rows(self) -> None:\n", "history manifest test")
path.write_text(text, encoding="utf-8")


# Coverage and type checking include the new critical module.
path = Path("pyproject.toml")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    '    "adaptive_weighting",\n    "history_migrations",',
    '    "adaptive_weighting",\n    "experiment_manifest",\n    "history_migrations",',
    "coverage module",
)
path.write_text(text, encoding="utf-8")

path = Path(".github/workflows/tests.yml")
text = path.read_text(encoding="utf-8")
text = replace_once(
    text,
    "          mypy --follow-imports=skip \\\n            history_migrations.py \\\n",
    "          mypy --follow-imports=skip \\\n            experiment_manifest.py \\\n            history_migrations.py \\\n",
    "mypy module",
)
path.write_text(text, encoding="utf-8")
