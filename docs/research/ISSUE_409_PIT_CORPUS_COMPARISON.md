# Issue #409 — point-in-time BTC/USD corpus procurement comparison

_Public documentation reviewed: 2026-09-27. No provider was contacted; no
quote, sample, dataset, license, or approval was obtained._

## Outcome: blocked

The machine-readable comparison is
[`ISSUE_409_PIT_CORPUS_COMPARISON.json`](ISSUE_409_PIT_CORPUS_COMPARISON.json).
Five archive/data channels are compared against the unchanged canonical
contract. Public product descriptions establish plausible historical data
channels, not the complete set of exact-pair, complete-coverage, publication
vintage, revision-0, and licensed-retention requirements. Therefore **no source
is admitted**, no procurement is approved, and no data was acquired. The
existing canonical absence audit remains 0/32,136 target hours and 0/4,320
warm-up hours; it is not an audit of vendor data.

## Contract and decision rule

The required archive is one named spot venue, exact BTC/USD or XBT/USD, hourly
UTC OHLCV for target `[2023-01-01T00:00:00Z, 2026-09-01T00:00:00Z)` and its
contiguous 180-day warm-up `[2022-07-05T00:00:00Z, 2023-01-01T00:00:00Z)`.
Each observation must provide the exact candle-close timestamp, the exact
candle's first-publication time no later than its close, and original revision-0
values plus revision history. Research storage/use and retention must be
explicitly licensed. The unchanged canonical audit requires all 32,136 target
and 4,320 warm-up hours; no dataset was available to run through it.

## Comparison findings

| Channel | Publicly documented capability | Eligibility blocker / unknown |
| --- | --- | --- |
| Kaiko | Spot trade aggregations including OHLCV, exchange/instrument references, versioning documentation, and API/cloud channels. | Exact eligible instrument and interval not entitlement-verified; documentation does not establish per-candle first-publication plus recoverable original revision-0 history; license, retention, quote, and sample not obtained. |
| Coin Metrics Market Data Feed | Market-specific OHLCV including `1h`, UTC timestamps, catalog/coverage discovery, historical HTTP access. Its candle documentation explicitly describes recalculation of recent values and replacement by finalized candles. | Exact venue coverage and full interval not verified. No evidence that per-candle initial publication and original revision-0 candle values/history can be obtained. License, retention, quote, and sample not obtained. |
| CoinAPI Flat Files / Market Data API | Product documentation describes bulk historical files and historical/realtime market data. | Public product overview does not establish exact pair, coverage, first-publication/revision-0 fields, license/retention, or cost. No sample or quote obtained. |
| Amberdata spot OHLCV | Historical/batch OHLCV, hourly granularity, and venue/dataset coverage table are documented. Public coverage lists Kraken and Bitstamp spot OHLCV history before the target start. | Coverage dates do not prove pair-level exactness or every required hour; publication vintage/revision-0 history is not established. License, retention, quote, and sample not obtained. |
| Direct Kraken public OHLC endpoint | Repository's #397 source review records the documented 720-entry recent limit and inability to retrieve older records. | Cannot cover target plus warm-up and does not establish first publication or revision history. No separate licensed archive identified. |

These are documentation-level findings only. In particular, a data-versioning
feature, long history start date, hourly API, or coverage table alone does not
prove that the exact immutable revision-0 records required by this gate can be
licensed and delivered.

## Procurement decision and reproducible next action

No provider contact, purchase, budget approval, or license approval is claimed.
The responsible role is the **project owner/budget authority**; this audit does
not name or assign an individual. Before any provider outreach, that role must
record written authorization for a procurement evaluation and acceptable
spend/contract boundaries. This authorizes inquiry only. Purchase, account
creation, paid access, and acquisition each remain subject to explicit approval
under the resulting terms.

### First inquiry after authorization: Kaiko

Kaiko's [official contact page](https://www.kaiko.com/contact) invites
requirements for a custom price quote. Its [Contact form](https://www.kaiko.com/contact-kaiko)
is the documented first inquiry channel. The proposed first instrument is
**Kraken XBT/USD only**, using the CeFi spot count/OHLCV API as product context;
this is an inquiry target, not a finding that Kaiko covers or can provide an
eligible corpus.

After the authorization gate, send the following specification and request
written answers, an authorized sample/schema, and the applicable license terms:

> We are evaluating a research-only BTC/USD historical corpus. Please confirm
> whether you can supply one named spot venue only: Kraken XBT/USD (do not
> combine with another venue), hourly UTC OHLCV for target
> `[2023-01-01T00:00:00Z, 2026-09-01T00:00:00Z)` and contiguous warm-up
> `[2022-07-05T00:00:00Z, 2023-01-01T00:00:00Z)`, totaling 32,136 target hours
> and 4,320 warm-up hours. For the exact original candle records, can you
> provide candle interval/close timestamp, the timestamp each record was first
> published/available (and confirm it is no later than the close), immutable
> original revision-0 values, and revision/change history sufficient to
> distinguish revision 0 from later corrections? Please state completeness/gap
> reporting and exact venue/instrument identifiers. Please provide the
> applicable research storage/use and retention license terms, restrictions on
> retained source files, acquisition/delivery method, quote and contract
> dependencies, and an authorized sample/schema demonstrating these fields. If
> any requested field, coverage, venue/pair, vintage, license, or retention
> right is unavailable, please identify it explicitly as unavailable; do not
> substitute another venue or quote asset.

The machine-readable artifact retains a copy of the inquiry text and question
list. Its tracking fields remain `submission_status: not_sent` and
`response_status: not_requested` until an authorized submission occurs. If a
future authorized inquiry receives no response, record `no_response`, the
submission and follow-up/check dates, and preserve the unanswered state; silence
is not a provider refusal or approval. Record any reply as a dated, sanitized
summary/link without credentials or licensed samples. Treat each unconfirmed
item as unknown; do not infer it. If Kraken XBT/USD is unavailable, do not
substitute a second venue within this corpus; any alternative venue requires a
separate explicit single-venue review.

Only after separate authorized access, preserve original files and terms
outside Git with acquisition metadata and SHA-256 manifest, then run the
canonical audit described in
[`ISSUE_397_PIT_BTCUSD_CORPUS.md`](ISSUE_397_PIT_BTCUSD_CORPUS.md).

Do not use BTCUSDT, combine venues, treat revised latest history as a vintage,
infer missing vintage data, admit partial coverage, or relax any gate. Until a
fully licensed source passes the unchanged audit, the corpus remains blocked.

## Public documentation references

- [Kaiko OHLCV trade aggregations](https://docs.kaiko.com/rest-api/cefi-spot-market-data/trade-aggregations/trade-count-ohlcv-and-vwap.md), [data versioning](https://docs.kaiko.com/rest-api/general/getting-started/data-versioning.md), and [instrument reference](https://docs.kaiko.com/rest-api/reference-data/exchange-trading-pair-codes-instruments.md).
- [Kaiko official Contact page](https://www.kaiko.com/contact), which requests requirements for custom quotes, and its [Contact form](https://www.kaiko.com/contact-kaiko).
- [Coin Metrics market data overview](https://gitbook-docs.coinmetrics.io/market-data/market-data-overview.md) and [market candles](https://gitbook-docs.coinmetrics.io/market-data/market-data-overview/market-candles.md).
- [CoinAPI documentation and product navigation](https://docs.coinapi.io/), including Flat Files and Market Data API.
- [Amberdata OHLCV](https://docs.amberdata.io/data-dictionary/market/ohlcv), [coverage](https://docs.amberdata.io/data-dictionary/coverage/coverage-market), and [spot historical OHLCV](https://docs.amberdata.io/http/market/spot-ohlcv.md).
- [Kraken OHLC endpoint](https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/) and prior repository evidence in [#397 audit](ISSUE_397_PIT_BTCUSD_CORPUS.md).
