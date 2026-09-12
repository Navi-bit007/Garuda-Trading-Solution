You are a senior quantitative developer, Python architect, algorithmic trading engineer,
risk engineer, and QA engineer.

Build a COMPLETE LOCAL-FIRST INTRADAY EQUITY ALGORITHMIC TRADING PLATFORM for Indian
NSE/BSE stocks using Zerodha Kite Connect.

IMPORTANT:
- This is a research/trading engineering project, NOT a guaranteed-profit system.
- NEVER claim that any strategy guarantees profit.
- The system must optimize for risk-adjusted returns, positive expectancy, controlled
  drawdown, robustness and out-of-sample performance.
- Do NOT use an LLM to make trading decisions.
- All trading decisions must be deterministic, reproducible and backtestable.
- Initially the system MUST run in PAPER TRADING mode.
- NEVER place a real order unless TRADING_MODE=LIVE is explicitly enabled.
- Add multiple safety checks before any live order.
- Never use look-ahead data.
- Never use future candles in indicators or signal generation.
- All signals must be generated using information available at that exact timestamp.
- Intraday positions must be closed before the configured market close.
- Build the system so that it can later be moved to cloud, but DO NOT require cloud now.

============================================================
1. BUSINESS OBJECTIVE
============================================================

Initial capital:
₹1,00,000

Market:
Indian equities

Universe:
NIFTY 500

Trading style:
Intraday equity trading

Primary objective:
Find statistically robust intraday strategies that can potentially generate positive
risk-adjusted returns after brokerage, taxes, slippage and transaction costs.

Do NOT target a fixed guaranteed daily profit.

For research purposes use:
- risk per trade: 0.5% of equity
- maximum daily loss: 1.5% of equity
- maximum simultaneous positions: 3
- maximum trades per day: configurable
- maximum capital deployment: configurable
- default maximum capital deployment: 80%

The system must support configuration of all these values.

============================================================
2. TECHNOLOGY
============================================================

Use:

Python 3.12+

Kite Connect API
Pandas
NumPy
pandas-ta or equivalent indicator library
SQLite
SQLAlchemy
Streamlit
APScheduler
python-dotenv
pytest
Pydantic
logging

DO NOT require:
- PostgreSQL
- Redis
- Docker
- Kubernetes
- Azure
- AWS
- GCP

Everything must work on a normal Windows local machine.

Use SQLite for persistence.

Use in-memory Python state for realtime state.

Use CSV/Parquet for historical research data.

============================================================
3. PROJECT STRUCTURE
============================================================

Create exactly this structure:

algo-trading/

    app/
        __init__.py

        main.py

        config/
            __init__.py
            settings.py
            constants.py

        broker/
            __init__.py
            kite_client.py
            authentication.py
            market_data.py
            order_api.py
            positions_api.py

        market/
            __init__.py
            universe.py
            scanner.py
            candles.py
            indicators.py
            market_regime.py

        strategy/
            __init__.py
            base.py
            ema_trend.py
            vwap_momentum.py
            opening_range_breakout.py
            atr_momentum.py
            strategy_selector.py
            signal.py

        risk/
            __init__.py
            risk_manager.py
            position_sizing.py
            daily_limits.py
            exposure.py

        execution/
            __init__.py
            order_manager.py
            position_manager.py
            trailing_stop.py
            exit_manager.py
            reconciliation.py

        database/
            __init__.py
            database.py
            models.py
            repository.py

        monitoring/
            __init__.py
            logger.py
            health.py
            notifications.py

        scheduler/
            __init__.py
            jobs.py

    backtest/
        __init__.py
        engine.py
        data_loader.py
        simulator.py
        metrics.py
        walk_forward.py
        optimizer.py
        reports.py

    dashboard/
        app.py

    data/
        raw/
        processed/

    logs/

    tests/
        test_indicators.py
        test_strategies.py
        test_risk.py
        test_position_sizing.py
        test_trailing_stop.py
        test_backtest.py
        test_order_manager.py

    scripts/
        download_instruments.py
        download_historical_data.py
        run_backtest.py
        run_paper_trading.py

    .env.example
    .gitignore
    requirements.txt
    README.md

============================================================
4. CONFIGURATION
============================================================

Create .env.example:

KITE_API_KEY=
KITE_API_SECRET=

TRADING_MODE=PAPER

INITIAL_CAPITAL=500000

RISK_PER_TRADE=0.005
MAX_DAILY_LOSS=0.015
MAX_OPEN_POSITIONS=3
MAX_TRADES_PER_DAY=5
MAX_CAPITAL_DEPLOYMENT=0.80

MARKET_OPEN=09:15
ENTRY_START=09:20
ENTRY_END=14:45
FORCE_EXIT=15:15

TRAILING_ATR_MULTIPLIER=1.5

ENABLE_TELEGRAM=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

Create strongly typed Pydantic settings.

Never hardcode credentials.

Never commit .env.

============================================================
5. KITE CONNECT
============================================================

Implement a clean KiteClient abstraction.

It must support:

- authentication
- access token handling
- profile retrieval
- instrument download
- historical candles
- live WebSocket
- order placement
- order modification
- order cancellation
- order status
- positions
- holdings
- margins

Create an interface so the rest of the application never directly depends on
the Kite SDK.

Example:

KiteClient
    get_profile()
    get_instruments()
    get_historical_data()
    subscribe()
    place_order()
    modify_order()
    cancel_order()
    get_orders()
    get_positions()
    get_margins()

Implement PaperBroker with the SAME interface.

Therefore:

PaperBroker
and
KiteBroker

must both implement the same broker interface.

This allows switching:

TRADING_MODE=PAPER

or

TRADING_MODE=LIVE

without changing strategy code.

============================================================
6. PAPER TRADING IS DEFAULT
============================================================

This is mandatory.

If:

TRADING_MODE != LIVE

then NEVER call a live order placement API.

Instead:

PaperBroker should simulate:

BUY
SELL
order status
fills
slippage
brokerage
position
P&L
stop loss
trailing stop

Paper trading should use real live market prices from Kite WebSocket when available.

============================================================
7. NIFTY 500 UNIVERSE
============================================================

Build a universe manager.

It must:

1. Load current NIFTY 500 constituents.
2. Map symbols to Kite instrument tokens.
3. Remove invalid/non-tradable symbols.
4. Filter by liquidity.
5. Filter by price.
6. Filter by average volume.
7. Rank candidates.

Do not hardcode the NIFTY 500 list permanently.

Create a configurable universe source.

Store instrument metadata in SQLite.

============================================================
8. MARKET DATA
============================================================

Build a WebSocket market data manager.

Requirements:

- reconnect automatically
- heartbeat monitoring
- stale tick detection
- maintain latest tick per symbol
- maintain OHLC candles
- handle connection failure safely
- never generate signals from stale data

Support:

1-minute candles
5-minute candles
15-minute candles
Daily candles

The default execution timeframe should be 5-minute.

============================================================
9. MARKET REGIME
============================================================

Before selecting trades, classify the market.

Use NIFTY 50 as the primary market regime reference.

Implement:

TRENDING_BULL
TRENDING_BEAR
SIDEWAYS
HIGH_VOLATILITY
UNKNOWN

Use deterministic indicators such as:

EMA
ADX
ATR
VWAP
market breadth if available

Do not overfit the regime classifier.

If regime is UNKNOWN:

NO TRADE.

If market is strongly sideways:

reduce or disable momentum strategies.

============================================================
10. STRATEGIES
============================================================

Implement at least FOUR independent strategies.

Strategy 1:
EMA TREND MOMENTUM

Daily filter:
Close > 200 EMA
50 EMA > 200 EMA

Intraday:
20 EMA > 50 EMA
Price > VWAP
ADX > configurable threshold
Relative volume > configurable threshold

Entry:
confirmed bullish momentum/breakout candle.

Strategy 2:
VWAP MOMENTUM

Conditions:
Price > VWAP
EMA alignment bullish
relative volume spike
higher high
momentum confirmation

Entry only after candle close.

Strategy 3:
OPENING RANGE BREAKOUT

Opening range:
09:15 to configurable range end.

Long:
price breaks opening range high
AND volume confirmation
AND price above VWAP
AND market regime not bearish.

Short:
only if short selling is explicitly enabled.

Default:
LONG ONLY.

Strategy 4:
ATR MOMENTUM

Use ATR to identify sufficient volatility.

Avoid stocks where ATR as percentage of price is too low.

Use momentum + VWAP + trend confirmation.

============================================================
11. YOUR EXISTING 4EMA STRATEGY
============================================================

Implement my existing indicator as a separate strategy.

EMA:
20
50
100
200

Bullish structure:

20 EMA > 50 EMA > 100 EMA > 200 EMA

Bearish structure:

20 EMA < 50 EMA < 100 EMA < 200 EMA

Implement the crossover signals exactly.

Also implement the ATR trailing logic from the supplied TradingView script.

Do NOT silently change the original logic.

Create tests against known candle sequences.

Name it:

Alxuse4EMA

This strategy must be compared against the other strategies.

============================================================
12. SIGNAL OBJECT
============================================================

Strategies must NEVER directly place orders.

Create:

Signal

with:

symbol
instrument_token
direction
strategy_name
timestamp
entry_price
stop_price
risk_per_share
confidence
market_regime
reason
timeframe

Example:

Signal(
    symbol="RELIANCE",
    direction="BUY",
    strategy_name="VWAP_MOMENTUM",
    entry_price=1420,
    stop_price=1408,
    confidence=0.81
)

============================================================
13. SIGNAL CONFIRMATION
============================================================

Never trade on an unfinished candle.

Only generate signals after candle close.

No lookahead.

No future information.

No repainting.

All backtests must use the same confirmation behavior as live trading.

============================================================
14. STOCK RANKING
============================================================

Do not trade every signal.

Rank candidates.

Score:

Trend: 20%
Momentum: 20%
Relative Volume: 20%
Liquidity: 15%
Volatility: 15%
VWAP position: 10%

Make weights configurable.

Select only the top N candidates.

Default:

TOP 10

Then allow strategy signal confirmation.

============================================================
15. RISK ENGINE
============================================================

For capital ₹500,000:

Default risk per trade:

0.5%

Maximum theoretical loss per trade:

₹2,500

Position sizing:

quantity =
risk_amount /
abs(entry_price - stop_price)

Always round quantity down to valid integer quantity.

Never exceed available capital.

Never exceed configured exposure.

Never increase position size to recover losses.

============================================================
16. DAILY RISK LIMIT
============================================================

Default:

MAX DAILY LOSS = 1.5%

For ₹500,000:

₹7,500

If realized + unrealized P&L reaches -₹7,500:

1. cancel pending orders
2. close positions safely according to configured policy
3. disable new entries
4. enter HALTED state
5. send notification
6. write audit log

Do not reset the limit during the day.

============================================================
17. CONSECUTIVE LOSS PROTECTION
============================================================

Implement:

MAX_CONSECUTIVE_LOSSES = 3

After 3 consecutive losing trades:

pause new entries.

Require configurable cooldown.

============================================================
18. TRAILING STOP ENGINE
============================================================

This is critical.

Implement an ATR-based trailing stop.

Initial stop:

entry - ATR * INITIAL_STOP_ATR

For long positions:

highest_price = maximum price since entry

trailing_stop =
highest_price - ATR * TRAILING_ATR_MULTIPLIER

Never decrease the stop.

For example:

entry = 1000
initial stop = 990

If price reaches:

1010
stop may move toward breakeven.

If price reaches:

1020
stop moves upward.

If price reaches:

1030
stop moves upward again.

If price falls through the trailing stop:

EXIT.

Support configurable profit-lock rules:

At +1R:
move stop to breakeven.

At +1.5R:
enable trailing.

At +2R:
lock minimum +1R profit.

All rules configurable.

============================================================
19. TRAILING STOP SAFETY
============================================================

Trailing stop must NEVER:

- move backwards
- increase risk
- exceed current market price incorrectly
- use future candles
- depend on unavailable future information

If the application crashes:

the system must reconcile positions after restart.

============================================================
20. POSITION MANAGER
============================================================

Maintain:

symbol
entry
quantity
side
current price
highest price
lowest price
initial stop
current stop
ATR
R multiple
unrealized P&L
realized P&L
strategy
entry timestamp

Every live tick updates the position manager.

============================================================
21. ORDER MANAGER
============================================================

Centralize all orders.

Before order:

CHECK:

market open
trading enabled
daily loss limit
max positions
max trades
capital
position duplication
symbol validity
quantity
price
risk
stale data
strategy enabled
system health

If ANY check fails:

reject order.

Every order must have:

client_order_id
strategy
signal_id
timestamp
symbol
side
quantity
price
stop
reason

============================================================
22. ORDER RECONCILIATION
============================================================

Never assume an order succeeded just because the API request succeeded.

After every order:

query order status.

Handle:

OPEN
COMPLETE
REJECTED
CANCELLED
TRIGGER_PENDING

Reconcile local database with broker positions.

On application startup:

1. retrieve broker positions
2. retrieve open orders
3. compare with SQLite
4. resolve differences
5. only then enable trading.

============================================================
23. FORCE EXIT
============================================================

Default:

FORCE_EXIT = 15:15

At force exit:

1. disable new entries
2. cancel pending entry orders
3. close all intraday positions
4. verify positions are zero
5. reconcile
6. record daily P&L
7. generate report

Never leave an intraday position unintentionally open.

============================================================
24. BACKTEST ENGINE
============================================================

Build a proper event-driven backtester.

It must simulate:

- candle data
- entries
- exits
- slippage
- brokerage
- taxes/charges configurable
- position sizing
- stop loss
- trailing stop
- market hours
- force exit
- max daily loss
- max positions

DO NOT use simplistic vectorized calculations that introduce lookahead bias.

============================================================
25. BACKTEST METRICS
============================================================

Calculate:

Total return
CAGR
Net P&L
Gross profit
Gross loss
Win rate
Loss rate
Average win
Average loss
Profit factor
Expectancy
Sharpe ratio
Sortino ratio
Maximum drawdown
Maximum drawdown duration
Calmar ratio
Number of trades
Average holding time
Largest winning trade
Largest losing trade
Consecutive wins
Consecutive losses
Monthly returns
Yearly returns

Also compare:

Strategy
vs
Buy & Hold NIFTY 500 benchmark

============================================================
26. WALK-FORWARD TESTING
============================================================

Do NOT optimize on the complete dataset.

Implement:

TRAIN
VALIDATION
OUT-OF-SAMPLE

Example:

2019-2022 TRAIN
2023 VALIDATION
2024-2026 OUT-OF-SAMPLE

Then rolling walk-forward windows.

The strategy is considered robust only if performance remains acceptable
outside the optimization period.

============================================================
27. STRATEGY SELECTION
============================================================

DO NOT simply select the strategy with highest historical return.

Score based on:

profit factor
drawdown
expectancy
Sharpe
trade count
stability
out-of-sample performance

Reject strategies if:

profit factor < 1.2
or
maximum drawdown exceeds configured limit
or
too few trades
or
out-of-sample performance collapses
or
performance is dependent on one stock
or
performance is dependent on one year

These thresholds must be configurable.

============================================================
28. ANTI-OVERFITTING
============================================================

Never optimize dozens of parameters.

Limit optimization parameters.

Use sensible ranges.

Reject strategies that only work with one exact parameter.

Perform sensitivity analysis.

Example:

If ATR multiplier 1.5 works but 1.4 and 1.6 completely fail,
flag possible overfitting.

Generate a robustness report.

============================================================
29. COST MODEL
============================================================

Backtesting must include configurable:

brokerage
STT
exchange transaction charges
GST
SEBI charges
stamp duty
slippage

Do not report gross P&L as the primary performance number.

Primary metric:

NET P&L AFTER COSTS.

============================================================
30. PAPER TRADING ENGINE
============================================================

Build:

python scripts/run_paper_trading.py

When started:

1. load configuration
2. connect to Kite
3. authenticate
4. load universe
5. connect WebSocket
6. check market status
7. initialize risk manager
8. initialize strategies
9. scan market
10. generate signals
11. rank signals
12. risk-check signals
13. simulate orders
14. manage positions
15. update trailing stops
16. simulate exits
17. record trades
18. send notifications
19. generate end-of-day report

============================================================
31. LIVE MODE
============================================================

Implement live mode but make it extremely difficult to enable accidentally.

Require BOTH:

TRADING_MODE=LIVE

AND

ENABLE_LIVE_TRADING=true

AND

LIVE_CONFIRMATION=I_UNDERSTAND_THE_RISK

If any is missing:

PAPER MODE.

Before first live session:

display:

"WARNING: LIVE TRADING ENABLED.
Real money may be lost.
Type ENABLE LIVE TRADING to continue."

Do not accept a simple boolean alone.

============================================================
32. LIVE CAPITAL SAFETY
============================================================

Even if available capital is ₹500,000:

default live allocation:

₹50,000

Require explicit configuration to increase.

Add:

MAX_LIVE_CAPITAL

MAX_ORDER_VALUE

MAX_DAILY_LOSS

MAX_POSITION_VALUE

Never bypass these.

============================================================
33. FAILURE HANDLING
============================================================

Handle:

Kite disconnected
WebSocket disconnected
API timeout
order rejected
order partially filled
duplicate order
stale market data
computer restart
system restart
network failure
clock mismatch
SQLite failure
unexpected exception

For critical failure:

STOP NEW ENTRIES.

Attempt position reconciliation.

Never blindly place another order after an uncertain API response.

============================================================
34. SYSTEM STATES
============================================================

Implement state machine:

STARTING
AUTHENTICATING
READY
MARKET_CLOSED
SCANNING
TRADING
PAUSED
RISK_HALTED
ERROR
FORCE_EXIT
SHUTDOWN

No order can be placed unless state == TRADING.

============================================================
35. LOGGING
============================================================

Log everything.

Separate:

application.log
orders.log
signals.log
risk.log
errors.log

Every trade must be auditable.

Include:

timestamp
symbol
strategy
signal
decision
risk decision
order
fill
exit
P&L

============================================================
36. SQLITE DATABASE
============================================================

Create tables:

instruments
candles
signals
orders
trades
positions
daily_pnl
strategy_metrics
system_events
risk_events

Use SQLAlchemy models.

Add indexes where appropriate.

============================================================
37. STREAMLIT DASHBOARD
============================================================

Build a local dashboard.

Pages:

1. Overview
2. Live Positions
3. Today's Trades
4. Signals
5. Strategy Performance
6. Backtest Results
7. Risk
8. System Health
9. Logs
10. Configuration

Overview must show:

Capital
Today's P&L
Daily loss limit
Available risk
Open positions
Number of trades
Win rate
Current strategy
System state
WebSocket status
Kite connection status

============================================================
38. ONE-CLICK START
============================================================

Create:

run_paper.bat

which:

1. activates virtual environment
2. verifies dependencies
3. verifies .env
4. starts the trading engine
5. starts Streamlit dashboard
6. writes logs
7. opens dashboard

Also create:

run_backtest.bat

and:

run_dashboard.bat

Do not start LIVE trading from a generic start script.

============================================================
39. AUTOMATED STARTUP CHECKS
============================================================

Before paper trading:

check:

Python version
.env
Kite credentials
database
market calendar
system time
internet
Kite connectivity
instrument list
data freshness
strategy configuration
risk configuration

If any critical check fails:

DO NOT START TRADING.

Print a clear error.

============================================================
40. TELEGRAM
============================================================

If enabled:

send:

system started
connection lost
signal generated
paper entry
paper exit
stop loss
trailing stop
daily loss warning
daily halt
force exit
daily report
unexpected error

Never send API secrets.

============================================================
41. DAILY REPORT
============================================================

At end of day:

ALGO DAILY REPORT

Capital
Starting equity
Ending equity
Gross P&L
Charges
Net P&L
Return %
Number of trades
Wins
Losses
Win rate
Profit factor
Maximum intraday drawdown
Best trade
Worst trade
Strategy performance
Reason for exits

============================================================
42. TESTING
============================================================

Write comprehensive pytest tests.

At minimum test:

EMA calculations
VWAP
ATR
ADX
RSI
signal generation
no lookahead
position sizing
risk limits
daily loss
trailing stop
stop never moves backward
order state machine
duplicate order protection
force exit
paper broker
strategy selection
backtest calculations

Create deterministic test fixtures.

============================================================
43. NO LOOKAHEAD TEST
============================================================

Create a specific test proving:

A signal at timestamp T can only depend on data <= T.

Use synthetic candles.

If future candle data is modified,
the historical signal before that candle must remain unchanged.

============================================================
44. PERFORMANCE
============================================================

Optimize only after correctness.

Use:

NumPy
Pandas
efficient data structures

Avoid unnecessary API calls.

Cache instruments.

Do not request the same historical data repeatedly.

Respect broker/API limits.

============================================================
45. SECURITY
============================================================

Never log:

API secret
access token
password
session token

Add .env to .gitignore.

Add:

*.db
*.sqlite
.env
logs/
data/raw/
__pycache__/
.venv/

============================================================
46. README
============================================================

Create a complete README explaining:

installation
Python version
Kite setup
environment variables
authentication
historical data download
backtesting
walk-forward testing
paper trading
dashboard
live trading
risk controls
troubleshooting
architecture
strategy definitions

Clearly state that historical performance does not guarantee future performance.

============================================================
47. AUTOMATED RESEARCH PIPELINE
============================================================

Create:

scripts/run_research.py

It must:

1. load historical data
2. run all strategies
3. run backtests
4. calculate metrics
5. run walk-forward testing
6. compare strategies
7. generate CSV/HTML reports
8. identify robust strategies
9. recommend PAPER mode candidates

Never automatically enable LIVE mode based only on backtest results.

============================================================
48. "BEST STRATEGY" DEFINITION
============================================================

Do NOT define best strategy as:

highest return.

Define best as:

high positive expectancy
+
reasonable profit factor
+
controlled drawdown
+
stable yearly performance
+
stable across stocks
+
stable across market regimes
+
stable out-of-sample
+
reasonable trade frequency
+
robust parameter sensitivity.

If no strategy passes the criteria:

display:

NO ROBUST STRATEGY FOUND.

Do not trade.

============================================================
49. IMPORTANT TRADING PRINCIPLE
============================================================

The system must be allowed to do nothing.

If:

no high-quality signal
or
market sideways
or
risk limit reached
or
data stale
or
strategy unhealthy
or
system unhealthy

then:

NO TRADE.

Do not force trades to reach a daily profit target.

============================================================
50. DEVELOPMENT ORDER
============================================================

Implement in this exact order:

PHASE 1
Project structure
configuration
SQLite
logging

PHASE 2
Kite authentication
instrument download
historical data

PHASE 3
indicators
market regime
NIFTY 500 scanner

PHASE 4
strategies
signal engine

PHASE 5
backtester

PHASE 6
risk engine

PHASE 7
trailing stop

PHASE 8
paper broker

PHASE 9
live market WebSocket

PHASE 10
paper trading

PHASE 11
dashboard

PHASE 12
live Kite broker

PHASE 13
live safety confirmation

Do not skip phases.

============================================================
51. COPILOT EXECUTION RULE
============================================================

You are working inside an existing repository.

Before writing code:

1. inspect existing files
2. determine what already exists
3. do not overwrite useful code
4. create missing files
5. modify existing files carefully

After each phase:

- run tests
- fix errors
- verify imports
- verify application starts
- show exactly what was created
- show commands to run it

Do not dump thousands of lines into one file.

Keep modules small and maintainable.

============================================================
52. START NOW
============================================================

Start by creating PHASE 1 only.

Create:

app/
backtest/
dashboard/
tests/
scripts/

Create:

settings.py
constants.py
database.py
models.py
repository.py
logging
.env.example
.gitignore
requirements.txt
README.md

Create a health check:

python -m app.main

It must output:

SYSTEM OK
MODE: PAPER
DATABASE: OK
CONFIG: OK

Do NOT implement live order placement yet.

Do NOT place any real order.

After Phase 1 is complete, run the tests and report:

- files created
- tests passed
- commands to run
- next phase

Then continue to PHASE 2 only after Phase 1 is working.
One important change I'd make to your original expectation

Don't make the "Start" button = immediately trade with ₹5 lakh.

Make it:

Start → scan → evaluate → paper trade → report → prove robustness → then explicitly enable live.




