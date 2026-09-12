# Local-First Intraday Equity Trading Platform

Research and paper-trading platform for deterministic NSE/BSE equity strategies using Kite Connect. It is not a profit guarantee and must be validated out of sample before any live use.

## Quick start

```powershell
cd algo-trading
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:PYTHONPATH = (Get-Location).Path
py -m app.main
py -m pytest
```

The default mode is `PAPER`. Live order submission requires `TRADING_MODE=LIVE`, valid credentials, and explicit broker safety checks in the execution layer.

## Research flow

1. Put historical OHLCV files in `data/raw/`.
2. Run `py scripts/run_backtest.py --input data/raw/example.csv`.
3. Inspect the generated report in `data/processed/`.
4. Run paper trading with `py scripts/run_paper_trading.py` only after validating the strategy out of sample.

Launch the local frontend with:

```powershell
py -m streamlit run dashboard/app.py
```

The dashboard reads SQLite activity and CSV files from `data/raw/`; it does not fabricate market data when those sources are empty.

## Always-on signal engine

Open the dashboard, select watchlists, and use `Start scan engine` on `Scanner & signals`. This starts the EMA 9 -> EMA 200 progressive worker inside the dashboard process. The worker reads selected watchlists from SQLite on every cycle, evaluates each unique stock only after a completed configured candle, and persists its lifecycle in Bucket A or Bucket B.

- Bucket A / LIGHT: a fresh completed-candle EMA9 cross above EMA200.
- Bucket B / STRONG: the active cycle also satisfies EMA9 > EMA20 > EMA50.
- INVALIDATED: the active cycle falls back to EMA9 <= EMA200.
- WEAKENED: a Bucket B cycle loses the EMA9 > EMA20 > EMA50 alignment while its lifecycle record remains available.

Signal identity is scoped per user as `instrument + signal type + candle timestamp`; duplicate signals are ignored. Temporary market-data failures are retried, market-closed cycles do no work, and watchlist changes apply without restarting the worker. `USER_ID` defines the dashboard user scope; the persistence model supports multiple user IDs independently.

For headless deployments without the dashboard, `py scripts/run_signal_engine.py` remains available as an alternative entrypoint.

The `Swing auto trading` page scans selected watchlists with completed daily candles and requires at least 201 candles before looking for a fresh EMA 9 cross above EMA 200. It offers per-position capital and quantity caps, submits live entries as CNC market orders, and places the initial CNC SL-M protective order in Zerodha. When the page session is running, each new completed daily candle can ratchet that same Zerodha-side stop upward using a daily ATR trail. Stopping or closing the page does not cancel an existing broker stop, but it also stops further daily ratchets; validate this workflow in PAPER mode and with small live limits before relying on it.

The `Scanner & signals` and `Automatic trading` pages also offer the `High-conviction long` strategy. It evaluates completed 5-minute candles only and requires a close above EMA 200, EMA 9 > EMA 20 > EMA 50, a close above the previous 20-candle high, RVOL >= 1.5, close above VWAP and open, positive 5-minute change, and candle strength >= 0.60. It uses a 100-point score with a minimum of 85, an ATR(14) stop, +1% target 1, and +2% target 2; target 1 moves the protective stop to breakeven. NIFTY above VWAP contributes the optional market-confirmation points.

The `Historical day scan` page evaluates one saved watchlist on one completed date using daily history through that date. It supports the EMA 9/200 swing, EMA 200 close-above, and previous-day high breakout strategies, returns every matching stock without a result cap, reports symbol-level data errors separately, and can export the result table as CSV.

The `Watchlists` page creates user-owned lists from the full NSE/BSE instrument catalog. Search by company or symbol, add instruments to one or more lists, paste a comma/newline-separated stock list for bulk adding, select multiple lists for signals, and manage each list without importing an index. Bulk stock import resolves only equity instruments on the chosen NSE or BSE exchange and reports symbols it cannot find. An optional bulk index import is available as a convenience. Selected instruments are deduplicated by instrument token before the backend processes them. `Scanner & signals` and `Automatic trading` consume persisted backend records only; opening either page is not required for generation. The dashboard reports fresh, stalled, and error heartbeats for the signal engine. Notifications remain persisted for Telegram delivery and JSON backup, but there is no notification page or popover.

## Research strategy modules

Backtests and Automatic trading may continue to use the research strategy modules in `app/strategy/`; the always-on Scanner uses the EMA 9 -> EMA 200 progressive lifecycle and remains subject to the existing paper/live execution and risk safeguards.

For live Kite authentication, set the Redirect URL in the Kite developer app to `http://localhost:8501/`. In Risk & settings, open Kite login, sign in, and click Generate access token after the redirect. The generated token is kept for the current session; add `KITE_ACCESS_TOKEN=<token>` to `.env` to load it after a restart. Kite access tokens expire daily and must not be shared.

## Realtime pipeline

The realtime runner follows this path:

`KiteTicker ticks -> completed candles -> selected watchlists -> EMA 9/200 progressive signals -> risk approval -> market entry + protective stop -> target/trailing exit`

The runner builds the NIFTY 500 universe automatically by intersecting the Kite NSE instrument API with the official Nifty Indices constituent feed. The dashboard also supports NIFTY 50, NIFTY Next 50, NIFTY 100, NIFTY 200, NIFTY 500, Midcap, Smallcap, Microcap, and LargeMidcap feeds. Start it from `algo-trading` after placing a current daily access token in `.env`:

```powershell
$env:PYTHONPATH = (Get-Location).Path
py scripts/run_paper_trading.py
```

The dashboard consumes persisted signals and can submit them through the existing risk gates. Position monitoring remains an execution concern; signal generation is owned by the separate always-on worker and does not depend on a browser session.

PAPER mode records orders locally and never calls the broker order API. LIVE mode additionally requires `TRADING_MODE=LIVE`, the API secret, an authenticated client, and all configured risk checks. LIVE entries place a broker-side `SL-M` protective order; the dashboard cancels that order before a managed exit. Open positions, stop/target metadata, activity, and daily risk totals are persisted in `data/trading.sqlite3` and restored on restart. The dashboard and realtime runner append every pipeline event, including submitted entries and exits, skipped signals, risk rejections, validation rejections, and broker/API rejections. Overview reports P&L, win rate, profit factor, drawdown, time-of-day results, rejection counts, and the complete activity ledger. The trailing stop is maintained locally while the process is running, so reconcile broker positions after any disconnect before resuming live trading. Telegram notifications can be enabled with the existing `ENABLE_TELEGRAM`, `TELEGRAM_BOT_TOKEN`, and `TELEGRAM_CHAT_ID` settings.
