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

Scheduled runs post the forecast to X automatically. Manual runs can optionally post by enabling the `post_to_x` workflow input.

## Previous forecast reliability

Each run restores the previous forecast from the GitHub Actions cache and evaluates its **+2h prediction** against the exact completed Kraken hourly candle that corresponds to that forecast target.

The reliability block records:

- absolute price error in USD
- absolute percentage error
- predicted vs actual percentage change
- whether the predicted direction was correct
- whether the actual price landed inside the previous Q10-Q90 interval

The first run has no previous forecast to score. If a run is delayed or skipped, the script uses the timestamp of the previous forecast target rather than blindly comparing with the latest price.

## X / Twitter posting

The workflow generates a `tweet.txt` file with the current forecasts and the previous +2h reliability result, for example:

```text
BTC/USD - TimesFM 3
Now $100,000
2h $100,200 (+0.20%) | 4h $100,450 (+0.45%)
8h $100,800 (+0.80%) | 16h $101,100 (+1.10%)
Prev +2h: 0.31% error | direction OK | in range
Experimental - not financial advice.
```

Add these repository secrets before enabling scheduled posting:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

The X application/user credentials must have permission to create posts.

The posting step uses X API v2 through Tweepy with OAuth 1.0a user credentials. Credentials are only read from GitHub Secrets and are never written to the forecast artifacts.

## Forecast state

The current `forecast.json` is copied to `.state/previous_forecast.json` and saved with `actions/cache/save`. The next workflow run restores the most recent matching cache entry.

This avoids committing generated forecast state back to the repository.

## Data source

Kraken's public BTC/USD hourly OHLC endpoint is used, so no exchange API key is required.

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

To post the generated `tweet.txt` locally, export the four X credentials listed above and run:

```bash
python post_to_x.py
```

## Output

`forecast.json` contains the current forecast plus a `previous_forecast_reliability` section when a comparable previous forecast is available.

The JSON and generated X post are shown in the GitHub Actions job summary and uploaded as a 7-day artifact.

## Important

This is a forecasting experiment, not a trading signal.

TimesFM 3 pretrained weights currently have a separate non-commercial/non-production license. Check the model license before using it for real-money or production trading.
