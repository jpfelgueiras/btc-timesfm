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

The posting step uses X API v2 through Tweepy with OAuth 1.0a user credentials.

### 1. Create an X developer app

1. Open the X Developer Console at <https://console.x.com/> and sign in with the X account that should publish the forecasts.
2. Create an app, or open an existing app.
3. Configure OAuth 1.0a permissions as **Read and write**. Read-only credentials cannot create posts.
4. Open the app's **Keys and tokens** section.
5. Generate or copy the following credentials:
   - **API Key** -> `X_API_KEY`
   - **API Key Secret** -> `X_API_SECRET`
   - **Access Token** -> `X_ACCESS_TOKEN`
   - **Access Token Secret** -> `X_ACCESS_TOKEN_SECRET`
6. If you changed the app from read-only to read/write after generating user access tokens, regenerate/re-authorize the Access Token and Access Token Secret so they inherit the new permissions.

X's OAuth 1.0a documentation:

- <https://docs.x.com/fundamentals/authentication/oauth-1-0a/overview>
- <https://docs.x.com/fundamentals/authentication/oauth-1-0a/api-key-and-secret>
- <https://docs.x.com/fundamentals/developer-apps>

Treat all four values as passwords. Do not commit them to this repository, paste them into workflow YAML, or store them in forecast artifacts.

### 2. Add the credentials as GitHub Actions secrets

Create these repository secrets:

- `X_API_KEY`
- `X_API_SECRET`
- `X_ACCESS_TOKEN`
- `X_ACCESS_TOKEN_SECRET`

Using the GitHub UI:

1. Open this repository on GitHub.
2. Go to **Settings -> Secrets and variables -> Actions**.
3. Select **New repository secret**.
4. Add each of the four names above with the corresponding value from the X Developer Console.

Or, with GitHub CLI authenticated for this repository:

```bash
gh secret set X_API_KEY --repo jpfelgueiras/btc-timesfm
gh secret set X_API_SECRET --repo jpfelgueiras/btc-timesfm
gh secret set X_ACCESS_TOKEN --repo jpfelgueiras/btc-timesfm
gh secret set X_ACCESS_TOKEN_SECRET --repo jpfelgueiras/btc-timesfm
```

`gh` prompts securely for each value, so the token does not need to appear in your shell history.

### 3. Test before enabling scheduled posting

Run **BTC TimesFM 3 Forecast** manually from GitHub Actions on this branch. Leave `post_to_x` disabled first to verify the forecast and generated `tweet.txt`. Then run it again with `post_to_x` enabled to verify that the app can create a post.

Credentials are only read from GitHub Actions secrets and are never written to `forecast.json`, `tweet.txt`, artifacts, or the repository.

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
