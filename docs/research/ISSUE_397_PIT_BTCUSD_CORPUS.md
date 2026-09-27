# Issue #397 — point-in-time BTC/USD corpus acquisition audit

_Research checked: 2026-09-27_

## Outcome: blocked

No source located in this audit satisfies all of the corpus contract, and no
data was acquired. The canonical audit was run without a data argument; its
machine-readable result is committed as
[`ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json`](ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json).
It reports **0/32,136** target hours and **0/4,320** warm-up hours. The report
is an audit of absence, not an audit of any candidate dataset.

## Exact acceptance contract

A qualifying acquisition must be licensed for research storage and use, and
must consist of one named spot venue's BTC/USD (or XBT/USD) hourly OHLCV candles
covering `[2023-01-01T00:00:00Z, 2026-09-01T00:00:00Z)` plus the contiguous
180-day warm-up `[2022-07-05T00:00:00Z, 2023-01-01T00:00:00Z)`. Each row must
identify the candle-close UTC timestamp and that exact candle's first-publication
time and revision history. A frozen original vintage (revision 0) must be
available, with publication no later than the candle close. Any archive must
retain the source files, acquisition time and method, applicable license, and
SHA-256 hashes; no stitching venues or later revised history is admissible.

The canonical command and storage convention are:

```bash
mkdir -p .state/research/canonical-benchmark
# Save the licensed, immutable source drop there; preserve the provider's
# original files, license/terms, acquisition manifest, and SHA-256 manifest.
sha256sum .state/research/canonical-benchmark/btc_usd_hourly.csv \
  > .state/research/canonical-benchmark/SHA256SUMS
PYTHONPATH=src python -m btc_timesfm.research.canonical_benchmark \
  --data .state/research/canonical-benchmark/btc_usd_hourly.csv \
  --output .state/research/canonical-benchmark/audit.json
```

`.state/` is local ignored state: do not commit licensed/large source data or
private provider credentials. Keep a sanitized acquisition manifest and audit
report in the review package; verify hashes before replay. The canonical audit
also hashes the CSV itself and fails closed on mixed venue/pair, non-USD pair,
missing/duplicate/gapped hours, invalid OHLCV, missing point-in-time fields,
later publication, or nonzero revision. Passing it only authorizes replay, not
a skill comparison.

## Source evidence and remaining blocker

* [Kraken OHLC API documentation](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/)
  says it returns at most 720 recent entries and older data cannot be retrieved;
  it also includes the current uncommitted timeframe. This is insufficient for
  the required 36,456-hour contiguous interval and documents no first-publication
  or revision history.
* [Bitstamp OHLC API documentation](https://www.bitstamp.net/api/#tag/Market-info/operation/GetOHLCData)
  has a maximum response limit of 1,000 rows. It documents current OHLC retrieval,
  not a versioned point-in-time archive. Its commercial-use page requires a
  separate data license for commercial reuse; no research-storage license or
  provenance archive was obtained for this issue.
* The existing source review in [`../BENCHMARKS.md`](../BENCHMARKS.md) records
  that Coinbase Exchange candle requests are capped at 300 and warn history may
  be incomplete; candle responses do not establish first-publication vintages
  or revision state. Historical candles from any such endpoint cannot repair
  that provenance gap.
* Binance BTCUSDT is a different quote asset and is expressly transfer-only in
  the audit policy; it is not a BTC/USD substitute. No mixed-venue splice is
  allowed to satisfy duration.

Accordingly, the blocker is the absence of an identified licensed, immutable,
single-venue archive whose hourly rows have candle-close UTC plus verifiable
first-publication/revision provenance for all 36,456 required timestamps.
The next valid action is to obtain a provider's explicit license and such a
versioned export (or begin contemporaneous immutable capture and wait until full
coverage exists), preserve the original files and checksums, then rerun the
canonical command above. Do not lower coverage, infer vintages, use a latest
revised endpoint, use BTCUSDT, or claim a skill result from this blocked audit.
