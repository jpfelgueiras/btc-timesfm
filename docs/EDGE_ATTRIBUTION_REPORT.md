# Edge attribution report

The edge attribution report answers where the production ensemble beats the
persistence baseline and where it does not. It is generated only from durable
forecast history and stored experiment manifests, so it is suitable for CI or
scheduled Actions jobs.

```bash
PYTHONPATH=src python -m btc_timesfm.research.edge_attribution_report \
  --db .state/forecast_history.sqlite \
  --json edge_attribution_report.json \
  --markdown edge_attribution_report.md
```

The JSON artifact includes paired ensemble-vs-persistence metrics for 2h, 4h,
8h, and 16h horizons plus segmentation by:

- market regime
- realized-volatility bucket
- trend-strength bucket
- UTC time-of-day and day-of-week
- ensemble confidence bucket
- feature-set version from the experiment manifest/configuration id
- dominant model contribution from ensemble weights

Each segment reports sample count, ensemble MAE, persistence MAE, MAE delta,
bootstrap confidence interval, direction-accuracy delta, bias delta, conclusion,
and an `unstable_or_low_sample` flag. Positive MAE delta means the ensemble beat
persistence. Segments with no/low samples or inconclusive bootstrap evidence are
explicitly marked unstable.
