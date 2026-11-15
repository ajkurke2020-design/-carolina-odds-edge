# NC Odds Edge

A local dashboard for comparing betting-venue prices, estimating positive
expected value, finding cross-venue arbitrage, and tracking placed bets. Its
compact dark board takes general UI inspiration from odds-screen
products; it does not use OddsJam branding or assets and is not affiliated
with OddsJam.

## Live odds source

The app uses PropLine's API (`https://api.prop-line.com`) for real-time game
lines. Each selected sport is fetched with one bulk request for `h2h`, `spreads`,
and `totals`; the app does not poll automatically or make one request per event.
upcoming scans show events whose kickoff is still in the future in US Eastern
time, while live scans show events marked live and not completed by the API.
Event times are displayed in US Eastern time (`America/New_York`). Requests
include the configured North Carolina venues (DraftKings, FanDuel, BetMGM,
Fanatics, theScore Bet, Underdog, Novig, and ProphetX) and Pinnacle. Pinnacle
is collected only as a sharp reference and never shown as a bettable venue.
No automatic retry is
made when a rate or daily quota limit is reached.
Each sport is fetched in its own HTTPS session, with up to three sports in
flight at once. Direct PropLine connections use the host's IPv4 address because
the local IPv6 route was observed to fail TLS intermittently; hostname and
certificate validation remain enabled. Transient TLS handshake failures are
retried up to four times with exponential backoff, and connect/read timeouts
are retried once. A failed sport does not prevent the remaining sports from
being attempted.

## Run on Windows

From this directory:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
streamlit run app.py
```

The app opens at `http://localhost:8501`. Add your API key in the sidebar's
masked **PropLine API key** field, or set `PROPLINE_API_KEY` in the server
environment before starting the app. A server-configured key is never put in a
browser-side widget. Get a free key at https://prop-line.com. Select sports and
schedule, choose betting venues, then click **Scan odds**. The app displays
remaining daily quota when PropLine includes it in the response headers.

The app is locked unless `APP_ACCESS_PASSWORD` is configured with at least 16
characters. Set a long, random shared passphrase and give it only to people
you intend to invite. In PowerShell, set both values before running Streamlit:

```powershell
$env:APP_ACCESS_PASSWORD = "your-long-random-shared-passphrase"
$env:PROPLINE_API_KEY = "your-key"
```

For Streamlit Community Cloud, add these values to the app's **Settings >
Secrets** instead of committing them:

```toml
APP_ACCESS_PASSWORD = "your-long-random-shared-passphrase"
PROPLINE_API_KEY = "your-key"
```

The passphrase grants access to the shared app and shared bet database; it does
not create separate private accounts for each friend. Rotate the passphrase if
you need to revoke access.

The free tier allows 1,000 requests per day. A scan uses at most one request
per selected sport (seven if all supported sports are selected). Re-scan only
when you want a refreshed snapshot; responses are not polled in the background.

## Calculations and scan quality

- Totals are grouped by line, period, and team. A game total is never compared
  with either team's total; team-total rows show the team in the market label.
- Duplicate market generations use the newest `last_update`. Outcomes whose
  `last_seen_at` predates that market update are excluded as no longer offered.
- Fair probability comes only from a complete, two-way Pinnacle market on the
  same sport, event, market, period, line, team scope, and selection. Spreads
  must have opposite matching signed points; totals must have matching points.
  The app never falls back to soft-book consensus. Optional line-adjusted rows
  are disabled by default; when enabled, the scanner first interpolates between
  bracketing Pinnacle lines and otherwise uses a configured per-sport normal
  distribution model. It never extrapolates interpolation beyond Pinnacle's
  quoted range and always enforces the maximum line difference.
- Pinnacle vig is removed with the power method by default. The shared
  `scanner_config.py` config supports `power`, `shin`, or `multiplicative`,
  alongside the minimum EV (2%), 60-second quote-age limit, maximum Pinnacle
  overround (1.06), bankroll/Kelly caps, and line-adjusted controls. Line
  adjustments default off, with `max_line_diff` set to 1.5 points and their
  Kelly fraction defaulting to half the exact-match fraction. The configured
  football model adds 4- and 2.5-percentage-point adjustments when spreads
  cross 3 and 7, respectively. If the feed has no explicit mainline marker,
  the model infers the mainline from the Pinnacle line whose fair probability
  is closest to 50% (ties use lower overround). Per-sport standard deviations
  are starting assumptions in config; validate them against line-adjusted CLV
  before enabling the feature.
- Quotes older than the configured age, markets over the overround limit, and
  already-started events are excluded. Rows with EV above 15% remain visible
  with a `verify` flag. Each scan reports fetched/kept counts, drop reasons,
  and the minimum, median, and maximum EV. Reported venue liquidity below the
  configured `min_liquidity_dollars` threshold is excluded; the default is
  `$0`, and quotes without a liquidity value are not filtered.
- For non-push lines, EV is `(decimal odds * fair probability - 1) * 100`.
  For whole-number spread/total lines, the model estimates win, lose, and push
  probabilities and uses `win_p * (decimal odds - 1) - lose_p`; a push returns
  the stake. The table shows those probabilities for each opportunity.
- Alt-line ladders are reduced to the highest-EV rung for each event, market
  type, period, team scope, and selection.
- Arbitrage chooses the best compatible price pair among selected venues in the
  same event, market, and period. It requires exactly two outcomes, different
  venues for the two legs, and opposite points for spreads.
  Quotes without a timestamp or older than `max_quote_age_seconds` are filtered
  by PropLine and again locally before arbitrage calculation. The UI snapshot
  does not auto-refresh; scan again before acting.
  Stake allocations use the selected whole-dollar rounding increment; the app
  recalculates and displays only opportunities whose rounded stakes still
  guarantee a positive return.
- Kelly uses `f = (fair probability * decimal odds - 1) / (decimal odds - 1)`,
  then applies the selected fractional-Kelly multiplier. Each suggestion is
  capped at 1% of bankroll per bet and 3% per event, and rounded down to cents.
  Kelly remains sensitive to probability errors and does not remove betting risk.
- Pinnacle-reference opportunities and eligible Pinnacle market snapshots are
  logged locally in `data/bets.db`. After an event starts, the app calculates
  CLV against the latest stored pre-kickoff Pinnacle fair probability for the
  same selection. Exact-match and line-adjusted rows are stored and displayed
  in separate CLV buckets; adjusted rows estimate the close at the venue's
  line from same-scan Pinnacle snapshots. This is an observed close estimate,
  not an official closing line; scan near kickoff for more useful CLV data.
- To track a wager you actually placed, use **Log** beside a +EV row or
  **Log placed arbitrage** on an arbitrage card, then confirm the actual odds
  and stake. Arbitrage legs are saved as separate tracker rows in one
  transaction; uncheck any leg you did not place. Grade each placed leg after
  the event to update net profit and ROI. These actions record bets only; they
  do not place wagers. Manual entry remains available for bets not shown by a
  scan.

The app cannot ensure a quote is still available or that every leg is accepted.
Arbitrage calculations exclude limits, rejected wagers, price movement, fees,
and settlement-rule differences. Exchange fees and bid/ask spreads are not
deducted from +EV calculations. Verify prices, fees, and market rules at each
venue before acting.

PropLine's odds feed supplies `price` as American odds. The app keeps that
source price and converts it once to decimal odds for EV and arbitrage math.
Where PropLine provides exchange liquidity and its update time, these are
retained with the quote. Liquidity does not cap the suggested arbitrage stake,
and exchange fees are not included. Underdog's source price is used as
provided; its payout multiplier is metadata and is not applied a second time.
