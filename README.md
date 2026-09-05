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

## X / Twitter posting with Twikit

The workflow generates a `tweet.txt` file with the current forecasts and the previous +2h reliability result, for example:

```text
BTC/USD - TimesFM 3
Now $100,000
2h $100,200 (+0.20%) | 4h $100,450 (+0.45%)
8h $100,800 (+0.80%) | 16h $101,100 (+1.10%)
Prev +2h: 0.31% error | direction OK | in range
Experimental - not financial advice.
```

Posting uses **Twikit 2.3.3** and an authenticated X web-session cookie. It does **not** use X's paid developer API, so the previous `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, and `X_ACCESS_TOKEN_SECRET` secrets are no longer required by this branch.

Twikit is an unofficial X/Twitter client. It can break when X changes its internal endpoints, and X may reject activity that looks automated. Reuse the same session instead of logging in on every workflow run, keep the posting rate modest, and be prepared to refresh the session if X invalidates it.

### 1. Get the X session cookies from your browser

Open `https://x.com` in a desktop browser where you are already logged in.

In Chrome, Edge or Brave:

1. Open Developer Tools.
2. Go to **Application -> Storage -> Cookies -> https://x.com**.
3. Copy the values of `auth_token` and `ct0`.

In Safari on macOS:

1. Enable **Safari -> Settings -> Advanced -> Show features for web developers**.
2. Open the Web Inspector for `x.com`.
3. Go to **Storage -> Cookies**.
4. Copy the values of `auth_token` and `ct0`.

Create a temporary local file named `x_cookies.json`:

```json
{
  "auth_token": "YOUR_AUTH_TOKEN",
  "ct0": "YOUR_CT0"
}
```

Additional X cookies may be included, but `auth_token` and `ct0` are the ones required by `post_to_x.py`.

Treat this file like a password. It is ignored by `.gitignore` and must never be committed.

### 2. Store the session in GitHub Actions

The workflow expects one repository secret:

```text
X_COOKIES_JSON
```

With GitHub CLI:

```bash
gh secret set X_COOKIES_JSON --repo jpfelgueiras/btc-timesfm < x_cookies.json
```

Or with the GitHub UI:

1. Open `jpfelgueiras/btc-timesfm`.
2. Go to **Settings -> Secrets and variables -> Actions**.
3. Select **New repository secret**.
4. Name it `X_COOKIES_JSON`.
5. Paste the complete JSON object as the value.

After the secret is stored, remove the temporary local file:

```bash
rm x_cookies.json
```

Once Twikit posting has been tested successfully, the four old X API OAuth secrets can be deleted because this branch no longer reads them.

### 3. Test the session

Run **BTC TimesFM 3 Forecast** manually from GitHub Actions on `feature/x-forecast-reliability` with `post_to_x` enabled.

A successful post writes an `x_post_status.json` similar to:

```json
{
  "status": "posted",
  "provider": "twikit",
  "post_id": "1234567890"
}
```

If the session expires, is revoked, or X blocks the request, refresh `auth_token` and `ct0` from a logged-in browser session and replace the `X_COOKIES_JSON` secret.

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

To post the generated `tweet.txt` locally with a temporary cookie file:

```bash
export X_COOKIES_JSON="$(cat x_cookies.json)"
python post_to_x.py
```

## Output

`forecast.json` contains the current forecast plus a `previous_forecast_reliability` section when a comparable previous forecast is available.

The JSON, generated X post and `x_post_status.json` are shown or uploaded by GitHub Actions as appropriate, with artifacts retained for 7 days.

## Important

This is a forecasting experiment, not a trading signal.

TimesFM 3 pretrained weights currently have a separate non-commercial/non-production license. Check the model license before using it for real-money or production trading.
