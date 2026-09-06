# Order-book and microstructure signals

Issue #35 adds bounded BTC/USD L2 order-book context for research without making the production forecast depend on that data.

## Production collection

Each production run attempts one public Kraken depth request for `XBTUSD` with 100 levels. If Kraken is unavailable or malformed, Bitstamp `btcusd` is used as a fallback. If both fail, the forecast continues with `status=unavailable` and no microstructure features.

The snapshot records both the latest completed hourly candle (`origin_at`) and the actual order-book capture time (`captured_at`). A snapshot is rejected when it is captured before the origin or more than 1.25 hours after it. This prevents silently attaching a badly delayed book to an earlier forecast origin.

Derived features are:

- spread in basis points;
- bid and ask USD depth within 10 bps of mid;
- depth imbalance within 10 bps;
- bid and ask USD depth within 25 bps of mid;
- depth imbalance within 25 bps;
- top-of-book microprice deviation from mid in basis points.

The features are merged into `market_features` only as observed context. They do **not** change production ensemble weights or predictions. Provider, pair, capture lag, missing-feature diagnostics and errors are stored in the experiment manifest and `forecast.json`.

## Historical evaluation

Public exchange APIs do not provide a reliable bounded historical L2 book that can be reconstructed after the fact. The project therefore does not fabricate historical books from candles. Real snapshots are accumulated prospectively in durable forecast history, together with immutable forecast origins and later-matured outcomes.

`microstructure_ablation.py` evaluates only rows where every required microstructure feature was actually captured. For every simulated origin, training labels are eligible only when their target timestamp is already observable. The same ridge forecaster is compared with:

1. the existing spot-engineered feature baseline;
2. the exact same baseline plus the microstructure group.

Reports are separate for 2h, 4h, 8h and 16h, include regime breakdowns and a deterministic paired-bootstrap uncertainty result. Until enough real snapshots have accumulated, the recommendation is explicitly `insufficient_evidence`.

This design keeps data collection within GitHub Actions limits: production adds at most one primary request and one fallback request, and the weekly ablation reads the existing compressed SQLite history rather than downloading historical books.
