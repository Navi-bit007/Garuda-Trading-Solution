# CLAUDE.md

Project context and a running change log for work done in this repo with Claude Code, so any
future session (or teammate) can see what was asked and what changed without re-deriving it from
scratch.

## Project

`algo-trading/` — a Zerodha Kite Connect trading system: a Streamlit dashboard
(`algo-trading/dashboard/app.py`) plus a standalone trailing-stop agent subprocess
(`algo-trading/app/execution/trailing_stop_agent.py`, launched via `agent_launcher.py`) that
independently trails protective SL-M orders for open positions. Both share the same SQLite DB
(`algo-trading/data/trading.sqlite3`). Branch: `sankaran-dev` (remote:
`https://github.com/Navi-bit007/Garuda-Trading-Solution`, shared with teammate Navi-bit007).

## Change log

Newest first. Each entry: what was asked, what changed, and the files touched.

---

### 2026-09-17 — Intratrading scan kept evaluating/rejecting every symbol after the daily limit was hit
**Asked:** Confirmed the daily-trade-limit validation itself was working (every rejection
correctly showed "daily trade limit reached (5/5 trades today)"), but the scan kept running
through the rest of the universe anyway, rejecting one symbol at a time.
**What changed:** Added the same early-halt pattern already used for `max_open_positions`
(see `run_automatic_cycle`'s docstring) for the daily trade limit too: if the limit is already
reached before a scan cycle starts, the cycle halts immediately instead of scanning 600+ stocks
for nothing. Also added a mid-scan check so if the limit is reached partway through the same
cycle (e.g. the 5th trade lands mid-scan), the result-processing loop stops consuming further
candle-fetch results instead of evaluating/rejecting every remaining symbol one by one.
**Files:** `dashboard/app.py` (`run_automatic_cycle`).

---

### 2026-09-17 — Same compact status badge extended to Intratrading and Swing auto trading
**Asked:** Liked the Live monitor badge/popover approach; wanted it on the Intraday
(`render_automatic_trading`, page title "Intratrading") and Swing (`render_swing_auto_trading`)
pages too, and the scan progress bar moved.
**What changed:** Both pages' full-width `render_scan_activity_banner` status banners are now a
compact badge next to the page title (🟢/🟡/🔴/⚪/🔵 + short label via `st.popover`, full detail on
click). Each page's scan progress bar now renders into a fixed `st.empty()` slot reserved right
under the title (via `scan_progress_slot`), instead of a fresh `st.progress()` created wherever
the scan loop happened to sit further down the page.
**Files:** `dashboard/app.py` (`render_automatic_trading`/`render_automatic_status`,
`render_swing_auto_trading`/`render_swing_content`).
_Follow-up:_ the badge looked blank for the whole duration of a scan on both pages. Root cause
differed per page: on Intratrading, the badge is written from inside a `@st.fragment`, which
doesn't reliably flush into a container created outside it while the same function then blocks
for the scan; on Swing, the status-computation code textually sits *after* the (blocking) scan
block, so it simply hadn't executed yet. Fixed by writing a plain, non-fragment "🔵 Scanning..."
state directly into the badge slot the instant each scan starts (same proven technique the
already-working progress bar uses).

### 2026-09-17 — Narrower columns so the calculation/details text has room
**Asked:** In the Live monitor "SL-M stop updates" table and the P&L page's "Trade activity"
table, give the long text column (Calculation details / Details) max width and shrink the rest.
**What changed:** Set `width="small"` on Time/Symbol/From/To (Live monitor) and
Time/Event/Result/Final P&L (P&L page), keeping `width="large"` on the text column. These are
just defaults -- columns stay user-resizable by dragging in the UI.
**Files:** `dashboard/app.py` (both `st.dataframe` column_config blocks).

### 2026-09-17 — Live monitor's agent-status banner took too much vertical space
**Asked:** The full-width "Trailing-stop agent active" banner (with pulse animation) pushed the
position tables down on every render; wanted something compact next to the page title instead.
**What changed:** Replaced the always-visible banner with a small badge next to the "Live
monitor" title (🟢 Agent active / 🟡 Heartbeat stale / 🔴 Needs attention / ⚪ Agent stopped) using
`st.popover` — the full detail (and the "Start agent now" button when stopped) still renders
exactly as before, just inside the popover instead of always on the page.
**Files:** `dashboard/app.py` (`render_live_monitor`).

---

### 2026-09-17 — Daily trade limit only counted closed trades, not entries taken
**Asked:** Max trades/day = 5, but a 6th signal (DLINKINDIA) was still sent to the broker with
3 trades closed + 2 open that day.
**Root cause:** `DailyLimits.record_trade()` incremented the daily-trade counter only when a
position *closed* (`app/execution/trading_pipeline.py:267,631`), never when one *opened*. So a
trade closing effectively "freed up" a slot for a new one within the same day.
**Fix:** Split entry-counting from P&L-tracking. `DailyLimits.record_entry()` (new) increments
the counter at entry time; `record_trade(pnl)` now only accumulates `realized_pnl`. Restore-on-
restart (`Repository.load_daily_trade_stats`) now counts `entry_submitted` activity rows for
today instead of closed-trade rows.
**Files:** `app/risk/daily_limits.py`, `app/execution/trading_pipeline.py`,
`app/database/repository.py`, `tests/test_risk.py`, `tests/test_trading_pipeline.py`.

### 2026-09-17 — Protective stop left orphaned at the broker after a rejected entry
**Asked:** Traceback: `kiteconnect.exceptions.InputException: Order cannot be cancelled as it is
being processed. Try later.` when cancelling a protective stop right after its sibling entry
order was rejected (seen 11 times in `data/dashboard.log`, e.g. NSE:GILLETTE, NSE:INDOMIM).
**Root cause:** Zerodha's RMS/exchange pipeline hadn't settled the just-placed protective-stop
order into a cancellable state yet; the code gave up on the first failed cancel attempt, leaving
that SL-M order resting at the broker with no tracked position behind it.
**Fix:** `TradingPipeline._cancel_protective_stop_with_retry()` retries the cancel up to
`PROTECTIVE_STOP_CANCEL_RETRY_ATTEMPTS` (3) times with a short backoff before giving up (and
logging clearly if it still fails).
**Files:** `app/execution/trading_pipeline.py`, `tests/test_trading_pipeline.py`.

### 2026-09-17 — Manual "Exit position" button on the Live monitor page
**Asked:** A button to force-close any open position (intraday or swing) on demand from the
dashboard — cancel the resting SL-M and send a market exit, recorded the same way an automated
exit is.
**What changed:** Extracted the trailing-stop agent's force-exit sequence (place market order →
cancel protective stop → save trade/activity/decision-log rows → link the decision-log outcome)
into a shared `close_position_at_market()` helper, used by both the agent's own force-exit and a
new confirm-then-submit button on the Live monitor page. Handles INTRADAY (MIS) and SWING (CNC)
product codes, and re-checks the position immediately before acting (staleness guard against the
agent closing it moments earlier).
**Files:** `app/execution/exit_actions.py` (new), `app/execution/trailing_stop_agent.py`,
`dashboard/app.py` (`render_manual_exit_action`, wired into `render_live_monitor`),
`tests/test_exit_actions.py` (new).
_Follow-up:_ moved the Exit panel to render below the "SL-M stop updates" panel instead of above
it (`dashboard/app.py`).

### 2026-09-17 — Git: teammate's merge fully reverted, kept only this branch's own commits
**Asked:** After a merge with teammate Navi-bit007's `ed0e903` ("Improve backtest replay and
dashboard controls") caused a silent regression (see below) and a title/branding conflict
("Garuda Trading" → "ART Trading"), the user asked to discard the teammate's changes entirely
and keep only this branch's own history.
**What changed:** `git reset --hard` to the last own commit before the merge (`3164f6b`,
"Enhancement 5"), then `git push --force-with-lease origin sankaran-dev`. `origin/sankaran-dev`
no longer contains `ed0e903`'s backtest-job-runner/UI-redesign work — it will need a deliberate,
coordinated re-merge later if that work is still wanted.
**Note:** Along the way, the same merge silently reverted large unmarked (no conflict-marker)
sections of `render_live_monitor`/`render_pnl_statement` in `dashboard/app.py` to their pre-
session versions (dropping the Trade activity/decision-log viewer, P&L%, row selection, and
reintroducing removed "Last refreshed" banners) — git's 3-way merge picked the older side outside
the 4 hunks it actually flagged as conflicts. Fixed in-session before the eventual full reset.

### Earlier this session — Unified decision-log / LLM training-data pipeline, P&L page overhaul
**Asked:** Store execution/strategy/calculation history in a form usable to later train/query an
LLM, and let the user see it themselves; plus various Live monitor / P&L page usability fixes
(Zerodha's 25-modification order cap, force-exit/auto-square-off before RMS cutoff, a minimum
stop-improvement threshold to reduce modification-cap pressure, Kite session-validity checking,
Today P&L / P&L% cards, and a per-trade "Trade activity" drill-down backed by the decision log).
**Files (representative, not exhaustive):** `app/database/{database,models,repository}.py`
(`decision_log` table, `DecisionLogRecord`, `save_decision`/`load_decisions`/
`link_decision_outcome`), `app/execution/trailing_stop_agent.py`, `app/execution/agent_launcher.py`
(`trailing_agent_env_from_settings`), `app/config/settings.py` (`min_stop_improvement_pct`),
`app/monitoring/signal_engine.py`, `dashboard/app.py`, `scripts/export_llm_dataset.py`,
`scripts/backfill_decision_log.py`.
