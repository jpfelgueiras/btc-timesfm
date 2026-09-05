from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}: {old[:80]!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "adaptive_weighting.py",
    '        if model_name == "ensemble":\n            continue\n',
    "",
)

replace(
    "btc_forecast.py",
    "from adaptive_weighting import adaptive_model_weights, attach_persisted_outcomes\n",
    "from adaptive_weighting import adaptive_model_weights, attach_persisted_outcomes\n"
    "from conformal_calibration import conformal_calibration_multiplier, evaluation_report\n",
)
replace(
    "btc_forecast.py",
    "forecast_engine.adaptive_model_weights = adaptive_model_weights\n",
    "forecast_engine.adaptive_model_weights = adaptive_model_weights\n"
    "forecast_engine.empirical_calibration_multiplier = conformal_calibration_multiplier\n",
)
replace(
    "btc_forecast.py",
    "    engine_output = build_forecast(model, data, history, adaptive_confidence=adaptive_confidence)\n"
    "    generated_at = datetime.now(timezone.utc)\n",
    "    engine_output = build_forecast(model, data, history, adaptive_confidence=adaptive_confidence)\n"
    "    interval_calibration_evaluation = evaluation_report(\n"
    "        history, actuals, regime=str(engine_output[\"regime\"])\n"
    "    )\n"
    "    generated_at = datetime.now(timezone.utc)\n",
)
replace(
    "btc_forecast.py",
    '        "drift_detection": drift_report,\n        **engine_output,\n',
    '        "drift_detection": drift_report,\n'
    '        "interval_calibration_evaluation": interval_calibration_evaluation,\n'
    '        **engine_output,\n',
)

replace(
    ".github/workflows/tests.yml",
    "            history_backup.py \\\n            history_migrations.py \\\n",
    "            history_backup.py \\\n            conformal_calibration.py \\\n            history_migrations.py \\\n",
)

roadmap = Path("ROADMAP.md")
text = roadmap.read_text(encoding="utf-8")
text = text.replace("- #31 Conformal interval calibration", "- ✅ #31 Conformal interval calibration")
roadmap.write_text(text, encoding="utf-8")

readme = Path("README.md")
text = readme.read_text(encoding="utf-8")
marker = "## "
if "CONFORMAL_CALIBRATION.md" not in text:
    text += "\n## Interval calibration\n\nPrediction intervals use finite-sample conformal calibration once enough matured history exists, with the previous empirical multiplier as the sparse-history fallback. See `CONFORMAL_CALIBRATION.md` for configuration and diagnostics.\n"
readme.write_text(text, encoding="utf-8")

Path("_apply_issue31.py").unlink(missing_ok=True)
Path(".github/workflows/_apply_issue31.yml").unlink(missing_ok=True)
