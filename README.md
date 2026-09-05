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

## Multi-horizon forecast reliability

The workflow keeps a rolling history of recent forecasts and scores the most recent matured prediction for each horizon: **2h, 4h, 8h and 16h**.

For example, at a 12:00 run:

- the 2h comparison comes from the forecast issued around 10:00
- the 4h comparison comes from the forecast issued around 08:00
- the 8h comparison comes from the forecast issued around 04:00
- the 16h comparison comes from the forecast issued around 20:00 the previous day

Each score is matched to the exact completed Kraken hourly candle at that prediction's target timestamp. The reliability block records:

- predicted and actual price
- absolute price error in USD
- absolute percentage error
- predicted vs actual percentage change
- whether the predicted direction was correct
- whether the actual price landed inside the forecast Q10-Q90 interval

The forecast history keeps up to 48 snapshots and is persisted through the GitHub Actions cache. The old single-forecast cache format is migrated automatically on the first run.

A fresh history needs time to mature: 2h accuracy appears first, then 4h, 8h and finally 16h after enough scheduled runs have accumulated.

## X / Twitter posting with Twikit

The workflow generates a `tweet.txt` file with current forecasts plus the latest available error for each horizon, for example:

```text
BTC/USD - TimesFM 3
Now $100,000
2h $100,200 (+0.20%) | 4h $100,450 (+0.45%)
8h $100,800 (+0.80%) | 16h $101,100 (+1.10%)
Prev err: 2h 0.31% | 4h 0.42% | 8h 0.75% | 16h 1.10%
Experimental - not financial advice.
```

Posting uses **Twikit 2.3.3** and an authenticated X web-session cookie. It does **not** use X's paid developer API.

Twikit is an unofficial X/Twitter client. It can break when X changes its internal endpoints, and X may reject activity that looks automated. Reuse the same session, keep the posting rate modest, and refresh the browser session cookie if X invalidates it.

### X session cookies

Open `https://x.com` in a desktop browser where you are logged in and copy the `auth_token` and `ct0` cookies from the browser developer tools.

Create a temporary local file:

```json
{
  "auth_token": "YOUR_AUTH_TOKEN",
  "ct0": "YOUR_CT0"
}
```

Store it as the repository secret:

```bash
gh secret set X_COOKIES_JSON --repo jpfelgueiras/btc-timesfm < x_cookies.json
rm x_cookies.json
```

Treat these cookies like a password and never commit them.

## Forecast state

`.state/previous_forecast.json` now stores a versioned rolling forecast history rather than only one previous run. Keeping the existing path allows the first multi-horizon run to import the previous single-forecast cache automatically.

The state is saved with `actions/cache/save`, so generated forecast history is not committed to the repository.

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

To post the generated `tweet.txt` locally with a temporary cookie file:

```bash
export X_COOKIES_JSON="$(cat x_cookies.json)"
python post_to_x.py
```

## Output

`forecast.json` contains:

- the current 2h/4h/8h/16h forecasts
- `forecast_reliability` with the latest matured score for each available horizon
- `previous_forecast_reliability` as a backwards-compatible alias for the 2h score

The JSON, generated X post and `x_post_status.json` are shown or uploaded by GitHub Actions as appropriate, with artifacts retained for 7 days.

## Important

This is a forecasting experiment, not a trading signal.

TimesFM 3 pretrained weights currently have a separate non-commercial/non-production license. Check the model license before using it for real-money or production trading.
