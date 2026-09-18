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

### 2026-09-19 — Reconciler race fixed with an atomic claim; found & cleaned up a second stray dashboard process on port 8502
**Asked:** Implement the reconciler-consolidation plan from 2026-09-18 (see that entry) --
approved as-is: an atomic "claim" primitive rather than an ownership rewrite.

**What changed:** New `Repository.claim_position_close(symbol) -> PositionRecord | None`
(`app/database/repository.py`) -- `DELETE FROM positions WHERE symbol = ? RETURNING *` in one
statement, so SQLite itself serializes the race across processes; whichever caller's claim
actually removes the row gets the record back, the other gets `None` immediately. Replaced the
old "check `load_positions()`, then separately `delete_position`" pattern (which still had a
real TOCTOU window) at all four detection sites with claim-then-write:
- `trailing_stop_agent.py`: `_reconcile_one_intraday_position` and the swing reconciliation
  block -- previously wrote **unconditionally**, no guard at all.
- `trading_pipeline.py` / `swing_auto_trader.py`: `sync_broker_positions` -- previously had the
  narrower check-then-act guard from the earlier 2026-09-18 fix.
`SwingAutoTrader._delete_position` was left with no remaining callers after this and was
deleted rather than left as dead code.

**Deliberately out of scope (flagged, not fixed):** `TradingPipeline._close_position` (manual
exits, target/trailing-stop closes, force-exit, close-all) still calls `delete_position`
unconditionally, not the new atomic claim -- in principle the standalone agent could reconcile
the same broker-side fill in the narrow window between that order being placed and this method's
own delete/write. Left alone since the approved plan was specifically about the *passive
detection* paths (`sync_broker_positions` and the agent's reconcilers), not dashboard-initiated
closes; worth a follow-up if that gap is a concern.

**Also found while restarting the dashboard to test this:** a second, independent
`streamlit run dashboard/app.py` process on port 8502 with live active connections (separate
from the one on 8501 I'd been restarting after every fix today) -- meaning some of today's
earlier restarts may not have been visible if 8502 was the tab actually in use. Both stopped;
a single clean instance relaunched on 8501.

**Files:** `app/database/repository.py`, `app/execution/trailing_stop_agent.py`,
`app/execution/trading_pipeline.py`, `app/execution/swing_auto_trader.py`,
`tests/test_position_reconciliation_race.py` (new, 4 tests including a real concurrent-thread
race test against the same SQLite file, run 5x to confirm no flakiness). Full suite: 317
passed.

---

### 2026-09-18 — "Log out" now also relocks the dashboard password gate
**Asked:** Clicking "Log out" on the Kite authentication page returned to Kite's sign-in step,
not the dashboard's own password prompt -- confirmed the password gate itself works fine.

**Found:** `log_out_of_kite()` cleared Kite-session state but never touched
`st.session_state.dashboard_unlocked` (the password gate's own flag from the change earlier
today), so the browser stayed "unlocked" past a Kite logout.

**What changed:** `log_out_of_kite()` (`dashboard/app.py:647`) now also pops
`dashboard_unlocked`, so "Log out" returns all the way to the password prompt, not just Kite's
sign-in step.

**Files:** `dashboard/app.py`, `tests/test_dashboard_settings.py` (new test). Full suite: 313
passed. Dashboard restarted cleanly.

---

### 2026-09-18 — First step of the dashboard/app.py split: extracted frontend settings sync
**Asked:** "yes implement other two items too" (monolith split + reconciler consolidation).
Given both are high-risk/high-effort, asked the user to choose scope: they picked "one small,
contained extraction" for the split, and "plan it properly first" for the reconciler work
(separate entry below) rather than touching either at full scope in one pass.

**What changed:** Moved the fully self-contained, already-tested settings round-trip logic out
of `dashboard/app.py` into a new `app/config/frontend_settings.py`: `EDITABLE_SETTINGS` (the
allowlist of settings fields the UI can override), `settings_values`, `serialize_frontend_settings`,
`deserialize_frontend_settings`, `apply_frontend_settings`. `get_frontend_settings(st, ...)`
stays in `dashboard/app.py` (it's the one function of this group that actually needs `st` and
`get_dashboard_repository`, both dashboard-only concerns) and now just imports the rest. This is
intentionally the *first* of many similar small extractions, not a full rewrite -- chosen as the
starting example because it was already pure-function-shaped and already had test coverage
(`tests/test_dashboard_settings.py`), so it was safe to move mechanically with near-zero
behavioral risk.

**Files:** `app/config/frontend_settings.py` (new), `dashboard/app.py`,
`tests/test_frontend_settings.py` (new, 5 tests, testing the module directly at its new
location). `tests/test_dashboard_settings.py` untouched and still passing (imports
`deserialize_frontend_settings`/`serialize_frontend_settings` from `dashboard.app`, which still
re-exports them via the new import). Full suite: 312 passed. Dashboard restarted cleanly.

---

### 2026-09-18 — Password gate added in front of the entire dashboard
**Asked:** "yes implement auth now" (following up on the flagged "no auth at all" gap -- the
dashboard was previously reachable by anyone on the network with zero barrier, including the
Kite login page itself, real trading controls, and the manual exit button).

**Approach chosen (not asked, since a single-operator app doesn't need a design discussion):** a
single shared password gate, checked before anything else renders -- no new dependency, no
external identity provider, works the same whether run locally or eventually deployed. Auth
approaches tied to a specific host (Entra ID/Teams via Azure Easy Auth) were deliberately not
used since no hosting decision has been made yet; this works regardless of where the app ends
up running.

**What changed:**
- `Settings.dashboard_password: SecretStr` (new field, `app/config/settings.py`), env var
  `DASHBOARD_PASSWORD` (added to `.env`, left **empty** -- deliberately not inventing a
  password on the user's behalf; set your own value and restart to actually enable the gate).
- `render_dashboard_access_gate(st, settings)` (new function, `dashboard/app.py`): if
  `DASHBOARD_PASSWORD` is empty, shows a persistent warning that the dashboard is unprotected
  but doesn't block (an explicit, visible opt-out rather than a silent no-op); otherwise shows a
  password form and blocks all further rendering until the correct password is entered
  (`hmac.compare_digest` for a timing-safe comparison), then unlocks for the rest of the browser
  session via `st.session_state.dashboard_unlocked`.
- Wired in at the very top of `main()`, before the Kite-authentication page or any workspace
  page can render.

**Files:** `app/config/settings.py`, `.env`, `dashboard/app.py`,
`tests/test_dashboard_access_gate.py` (new, 5 tests). Full suite: 307 passed.

**Follow-up note:** password is currently unset in `.env` -- the dashboard is still open until
one is set there and the app restarted.

---

### 2026-09-18 — Self-healing restart for a crashed trailing-stop agent
**Asked:** "now focus other items" -- continuing down the improvement list, picked the
agent-crash recovery gap (the smallest remaining item, no design decision needed, unlike auth
or the two bigger architectural ones).

**Found:** `maybe_autostart_trailing_agent()` (`agent_launcher.py:132-142`) already exists and
already restarts the agent safely (it internally no-ops if a fresh heartbeat exists), but it
was only ever called from the Kite-authentication page at login/token-paste time
(`dashboard/app.py:2156, 2195`). If the agent process died mid-session while the token was
still valid, nothing called it again -- the Live monitor badge just sat on "🔴 Needs attention"
until someone noticed and manually clicked "Start agent now".

**What changed:** `render_live_monitor_content` (the `run_every=10` fragment already computing
the agent-status badge) now calls `maybe_autostart_trailing_agent(...)` itself when
`agent_state == "stalled"` (a heartbeat exists but has gone quiet -- implies an unexpected
crash) -- but deliberately not for `"stopped"` (heartbeat cleared by an explicit Stop click,
respecting the user's choice) or `"error"` (agent still alive and reporting each cycle).
Gated on the same `auto_start_trailing_agent` preference the login-time restart already
respects. A new `trailing_agent_self_heal_attempted_at` session-state timestamp with a 60s
cooldown (`TRAILING_AGENT_SELF_HEAL_COOLDOWN_SECONDS`) stops the 10s fragment re-run from
spawning a second duplicate process before the first restart's heartbeat has had time to land.

**Files:** `dashboard/app.py`. Full suite: 302 passed (no new tests -- this is a Streamlit
fragment wiring change with no new pure-logic function to unit test; verified via syntax check
+ clean dashboard restart).

---

### 2026-09-18 — Automated daily DB backup added; checked Kite daily token expiry (no gap found)
**Asked:** Continuing "what else can improve" -> chose to implement automated DB backup and
check the Kite daily-token-expiry flow.

**Kite token expiry finding (no code change):** Confirmed `verified_kite_access_token`
(`dashboard/app.py:648-687`) already does the right thing -- Kite invalidates every access
token once a trading day with no refresh grant (a broker-side constraint, not something any
app can work around), and the app already detects a dead token via a real `profile()` call,
clears it, and prompts re-login (`kite_session_expired_notice`) rather than silently failing
later. No gap here; nothing to implement.

**DB backup implemented:** `Database.backup_daily()` (new method,
`app/database/database.py`) takes a hot backup via sqlite3's online backup API (safe against a
live, open connection, unlike a raw file copy) into `data/backups/trading_<date>.sqlite3`,
skipping the copy if today's backup already exists (idempotent -- safe to call from both the
dashboard and the standalone agent, whichever starts first each day) and pruning backups older
than 14 days. Called automatically at the end of `Database.initialize()`; failures are caught
and logged, never block app startup. Verified end-to-end against the real production DB path
(`data/backups/trading_2026-09-18.sqlite3` created).

**Files:** `app/database/database.py`, `algo-trading/.gitignore` (added `data/backups/*`),
`tests/test_database_backup.py` (new, 3 tests). Full suite: 302 passed.

---

### 2026-09-18 — Stopped tracking the live trading DB/logs in git
**Asked:** "what else can improve in this app?" -> found this while investigating; user then said
"yes go ahead and implement it."

**Found:** `algo-trading/data/trading.sqlite3`, `algo-trading/data/dashboard.log`, and
`algo-trading/data/trailing_stop_agent.log` were all tracked in git on the shared `sankaran-dev`
branch. These files change on every trade/log line, so they generated a constant diff and were
a natural merge-conflict source with teammate Navi-bit007 (plausibly a contributor to the
dashboard/app.py merge conflicts hit earlier this session). Also found a stale, orphaned
duplicate `Garuda-Trading-Solution/data/trading.sqlite3` (repo root, not under `algo-trading/`,
6 days old) -- confirmed unused since `app/database/database.py:10` only ever resolves the
relative default path `data/trading.sqlite3` under the app's actual working directory
(`algo-trading/`), never the repo root. `.env` was separately confirmed to already be safely
gitignored -- this was specifically about the DB/log files.

**What changed:** Added `data/*.sqlite3` and `data/*.log` to `algo-trading/.gitignore`.
`git rm --cached` on all four files (untracked, left on disk -- the live DB/logs are still
exactly where the running app expects them, untouched). Committed on `sankaran-dev`
(commit `72c380a`), not pushed.

**Files:** `algo-trading/.gitignore`; git-untracked (not deleted):
`algo-trading/data/trading.sqlite3`, `algo-trading/data/dashboard.log`,
`algo-trading/data/trailing_stop_agent.log`, `data/trading.sqlite3`.

---

### 2026-09-18 — Telegram blocked by corporate IT; disabled again after confirming
**Asked:** Send a live test notification to confirm the Telegram integration end-to-end.

**Found:** `notifier.send(...)` to `api.telegram.org` failed with
`SSL: CERTIFICATE_VERIFY_FAILED -- self-signed certificate in certificate chain`, while the
same machine's HTTPS call to `api.kite.trade` succeeded normally. That's the signature of a
corporate proxy specifically intercepting/blocking Telegram's domain (not a general network or
code issue) -- confirmed by the user: IT does block Telegram on this network. Did not disable
TLS certificate verification to force it through, since that would silently defeat a real
security control.

**What changed:** `.env`: `ENABLE_TELEGRAM` set back to `false` (bot token/chat ID left in
place, unused while disabled, so they don't need to be re-entered if this is ever tried again
from an unrestricted network). Dashboard restarted to pick up the change.
`Notifier.send()` already no-ops immediately when `enabled` is `False`, so no code change was
needed -- alerts are simply inert now, and the entry/trail/close notify call sites added earlier
today stay in place for whenever Telegram access is available.

**Files:** `.env`.

---

### 2026-09-18 — Telegram alerts extended to swing trades and manual exits; real credentials wired in
**Asked:** Whether WhatsApp notifications were possible (answered: yes, but Telegram is free
and already built in, so pursued that instead), then to configure Telegram (BotFather steps
explained) and wire up alerts for three events: a new trade taken, SL-M stop moved, and a trade
closed, using supplied real bot credentials.

**Found:** Two of the three events were already fully wired for intraday
(`trading_pipeline.py`) and the standalone trailing-stop agent (`trailing_stop_agent.py` +
`exit_actions.py`) — entry, stop-trailing, and close all already call `Notifier.send(...)`.
The gaps were narrower than expected: `SwingAutoTrader` had no `Notifier` at all (no alert on a
swing entry or a broker-detected swing close), and the dashboard's Live Monitor manual "Exit
position" button called `close_position_at_market(...)` without passing `notifier=`, so manual
exits never alerted even though the helper already supported it. Also, `ENABLE_TELEGRAM` was
`false` with empty credentials in `.env`, so even the already-wired paths were silent no-ops.

**What changed:**
- `.env`: `ENABLE_TELEGRAM=true`, real `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` filled in.
- Extracted the duplicated `Settings` → `Notifier` construction (the `SecretStr`-unwrapping
  ternary) into a shared `notifier_from_settings(settings)` helper in
  `app/monitoring/notifications.py`; `trading_pipeline.py` and `dashboard/app.py`'s
  `record_signal_notifications` now use it instead of duplicating the logic.
- `SwingAutoTrader` now takes an optional `settings` constructor param, builds `self.notifier`
  from it (or a disabled `Notifier()` if omitted), and sends alerts from `_register_position`
  (`swing_entry_submitted`, on every new swing entry) and `sync_broker_positions`
  (`swing_exit_broker_detected`, on every broker-detected swing close).
- `dashboard/app.py`: `create_swing_auto_trader(...)` and both its call sites now pass
  `settings=settings` through to `SwingAutoTrader`; `render_manual_exit_action` now passes
  `notifier=notifier_from_settings(settings)` into `close_position_at_market(...)` so manual
  Live-Monitor exits alert the same way automated/agent exits already do.

**Files:** `.env`, `app/monitoring/notifications.py`, `app/execution/swing_auto_trader.py`,
`app/execution/trading_pipeline.py`, `dashboard/app.py`, `tests/test_swing_auto_trader.py` (new
`test_swing_trader_sends_telegram_alerts_on_entry_and_broker_detected_close`). Full suite:
299 passed.

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

### 2026-09-18 — Intratrading badge stuck on "Scanning..." even after a halt
**Asked:** The "Recent scan cycles" log correctly showed "scan halted: insufficient broker
margin...", but the status badge next to the title stayed on "🔵 Scanning..." indefinitely.
**Root cause:** The "Scanning..." badge is written directly (not via the status fragment) so
it's visible immediately when a scan starts, but nothing corrected it back afterwards -- an
early-return halt, an exception, or even a normal completion (scan-only mode, entry window
closed) all left it stuck until the status fragment's own 300s timer happened to fire.
**Fix:** Added a `halt_scan()` helper that writes the corrected "🔴 Needs attention" badge (with
the error message) the instant any halt fires, used by all three halt checks plus the cycle's
exception handler. For normal completions, `render_automatic_status()` is called again right
after `run_automatic_cycle()` returns (both the manual "Scan only" click and the 300s scheduler),
which re-renders the badge from the same already-correct state logic instead of leaving it stale.
**Files:** `dashboard/app.py` (`run_automatic_cycle`, `render_automatic_trading`).

### 2026-09-18 — Check broker margin before scanning, halt and alert if insufficient
**Asked:** A trade was rejected at the broker for insufficient funds -- can the app check the
account balance itself and stop the scan / alert the user instead of discovering it one
rejected order at a time?
**What changed:** New `OrderAPI.available_margin()` calls Kite's `margins("equity")` endpoint
and returns the `net` figure (the same "available margin" number the Zerodha UI shows), `None`
in PAPER mode or on any failure. Both Intratrading (`run_automatic_cycle`) and Swing auto
trading (`render_swing_content`) now check this once before a scan (not per-symbol) and halt
immediately with a clear message if the available margin is below the configured
capital-per-position limit -- surfaced the same way the existing max-open-positions/daily-limit
halts already are (red "Needs attention" badge, one alert, no wasted per-symbol scanning).
**Files:** `app/broker/order_api.py` (`available_margin`), `dashboard/app.py`
(`run_automatic_cycle`, `render_swing_content`), `tests/test_order_api.py` (new).

### 2026-09-18 — Duplicate trade records from two independent reconcilers racing
**Asked:** Investigating why the daily-trade-limit scan kept running, found NSE:ABB and
NSE:PVRINOX each recorded as CLOSED *twice* in the P&L page with identical entry/exit price but
different exit reasons, ~1 second apart. (Separately: also found two independent
`streamlit run dashboard/app.py` server processes running against the same DB — stopped one.)
**Root cause:** The standalone trailing-stop agent reconciles *every* LIVE position in the
shared `positions` table with no ownership boundary; the dashboard's own `TradingPipeline`
(Intratrading page) and `SwingAutoTrader` (Swing page) *also* independently poll the broker and
reconcile the same shared table via their own `sync_broker_positions()`. Neither knows about the
other, so both can detect and record the same broker-side close.
**Fix:** Both `TradingPipeline.sync_broker_positions()` and `SwingAutoTrader.sync_broker_positions()`
now re-check whether the position still exists in the DB `positions` table immediately before
writing a trade/activity record for a detected close. If it's already gone (someone else already
reconciled it), they only clean up their own in-memory tracking instead of writing a duplicate
trade record. This is a symptom-level fix, not a structural one — it's directly related to the
multi-user architecture gap under discussion (no per-user/per-instance ownership of positions);
a proper fix will fall out of that redesign.
**Files:** `app/execution/trading_pipeline.py` (`sync_broker_positions`),
`app/execution/swing_auto_trader.py` (`sync_broker_positions`).

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
