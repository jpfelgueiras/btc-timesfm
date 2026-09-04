# BTC TimesFM 3 Forecast

A small experiment using Google's **TimesFM 3** to forecast BTC/USD from completed hourly candles.

## Forecast horizons

Every run forecasts:

- +2 hours
- +4 hours
- +8 hours
- +16 hours

The output also includes TimesFM's 10th, 50th and 90th percentile estimates.

## Schedule

GitHub Actions runs every 2 hours, at minute `17` UTC:

```text
00:17
02:17
04:17
...
22:17
```

You can also start the workflow manually.

## Data source

Kraken's public BTC/USD hourly OHLC endpoint is used, so no exchange API key or GitHub secret is required.

Only completed candles are passed to the model.

## Model

```text
google/timesfm-3.0-pytorch
```

The project uses `timesfm[torch]==3.0.1` and runs inference on CPU so it works on a standard GitHub-hosted Ubuntu runner.

The Hugging Face model directory is cached between runs.

## Run locally

Python 3.11:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python btc_forecast.py
```

The first run downloads the TimesFM 3 checkpoint.

## Output

`forecast.json` contains a result similar to:

```json
{
  "pair": "BTC/USD",
  "latest_close_usd": 100000.0,
  "predictions": {
    "2h": {
      "price_usd": 100200.0,
      "change_pct": 0.2,
      "q10_usd": 99000.0,
      "q50_usd": 100100.0,
      "q90_usd": 101200.0
    }
  }
}
```

The JSON is also shown in the GitHub Actions job summary and uploaded as a 7-day artifact.

## Important

This is a forecasting experiment, not a trading signal.

TimesFM 3 pretrained weights currently have a separate non-commercial/non-production license. Check the model license before using it for real-money or production trading.

A useful next step is to persist each forecast and compare it with the later actual BTC price, so we can measure MAE, directional accuracy and performance by forecast horizon.
