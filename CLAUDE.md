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

### 2026-09-19 — Live Monitor: daily trade/open-position allowance box, per section
**Asked:** Screenshot mockup showing a small boxed readout next to "Intraday positions" and
"Swing positions" -- "Today Trade: 4/5, Open Position: 2/3" -- so the page itself shows what's
allowed per day and how much is already used, for both intraday and swing. Asked one clarifying
question first: intraday has a real daily-trade-cap setting (`max_trades_per_day`) to show as a
fraction, but swing has none in the codebase -- only an open-position cap
(`swing_max_open_positions`). Answered with the recommended option: show swing's entry count with
no "/max" (since there's nothing to divide by) rather than inventing and enforcing a new
swing-specific daily-trade-limit setting.

**What changed** (`dashboard/app.py`, `render_live_monitor_content`):
- New `closed_records = repository.load_trades()` (previously this function only loaded open
  positions) and `today = date.today()`, added just before the table-rendering helpers.
- New `render_trade_limit_box(position_type)`: `today_entries` counts positions of that type
  entered today, from both still-open positions (`records`) and closed trades (`closed_records`)
  -- a position that already closed today still counts toward "today's trade count". `open_count`
  is just today's currently-open positions of that type. For INTRADAY it renders "Today trade:
  X / max_trades_per_day · Open position: Y / max_open_positions"; for SWING, "Today trade: X ·
  Open position: Y / swing_max_open_positions" (no cap on the trade count, per the answer above).
- `render_table` now splits its header into `subheader_column, limit_column = st.columns([1, 2], ...)`
  so the box sits next to the section title instead of below it, and calls
  `render_trade_limit_box(position_type)` in the second column.
- New `.trade-limit-box` CSS class (bordered, red-tinted pill, matching the mockup's red box).
- Added one bullet to the existing "ℹ️ Information" popover explaining the new box.
- Full suite re-run (353 passed) and dashboard restarted.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Overview page: conditional Win %/Loss % coloring
**Asked:** "if win%>50; green, loss%>50 - red" -- color the Win %/Loss % cells in both the Hour /
Trade / P&L and Daily ledger tables based on the 50% threshold. (The user also flagged what looked
like a column-order swap between "Trades" and "Win %" in a screenshot of the Hour/Trade/P&L table
-- verified with a standalone pandas script, outside Streamlit, using the exact same
`.agg(...)`/reindex code: the DataFrame's column order and values are correct. `st.dataframe`'s
interactive grid lets a viewer drag-reorder columns, and that reorder is remembered client-side
per widget for the browser session -- the most likely explanation, not a server-side/data bug. No
code change was needed for that part; a hard refresh of that browser tab should reset it.)

**What changed** (`dashboard/app.py`): new module-level `style_win_loss_columns(frame)` returns a
pandas Styler with `.map(...)` applied to the "Win %" column (green, bold, when > 50) and "Loss %"
(red, bold, when > 50) -- verified `Styler.map` works under the installed pandas 3.0.5 (the older
`.applymap` alias is gone in pandas 3.x) and that `st.dataframe` accepts a Styler alongside
`column_config` in the installed Streamlit 1.63.0 (column_config's format string still wins for
number formatting; Styler only contributes the cell text color). Wired into both the "Hour /
Trade / P&L" and "Daily ledger" `st.dataframe(...)` calls by wrapping the frame:
`st.dataframe(style_win_loss_columns(by_hour), ...)` / `style_win_loss_columns(daily)`.
Full suite re-run (353 passed) and dashboard restarted.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Overview page follow-ups: inline title badge, inline Period filter, Win %/Loss % columns
**Asked (three quick follow-ups to the Overview redesign above):** (1) Fixed a P&L page typo --
the Breakdown popover's last bullet read "= Net P&L (after charges): ..."; remove the stray "=".
(2) Screenshot: move the LIVE/PAPER TRADING badge next to the "Intraday desk" title instead of on
its own line below it, and drop the `st.divider()` between the title area and "Trading analysis"
to tighten the vertical space. (3) "inline period and filters" -- the Period radio's label was
rendering above the horizontal radio options (Streamlit's default), not beside them. (4) Add
Win %/Loss % columns to both the Hour / Trade / P&L and Daily ledger tables -- "it will helpful
to understand for next trade."

**What changed** (`dashboard/app.py`, `render_overview`):
- `= Net P&L (after charges)` → `Net P&L (after charges)` in the P&L page's Breakdown popover.
- Title row restructured to `title_column, status_column = st.columns([5, 2], vertical_alignment="bottom")`
  (title in one, the LIVE/PAPER badge markdown in the other) -- same pattern used for title+badge
  rows on every other page already. The `st.divider()` between it and "Trading analysis" was
  removed.
- Period filter restructured the same way the P&L statement page's Period filter already works:
  `period_label_column, period_radio_column = st.columns([1, 5], vertical_alignment="center")`,
  `period_label_column.markdown("**Period**")`, and the radio moved into `period_radio_column`
  with `label_visibility="collapsed"` -- label and options now sit on one line instead of two.
- **Hour / Trade / P&L**: `by_hour` now also aggregates `Wins=("pnl", lambda v: int((v > 0).sum()))`,
  then derives `Win % = Wins/Trades*100` and `Loss % = 100 - Win %` (rounded to 1 decimal),
  inserted between Trades and Net_PnL.
- **Daily ledger**: `Wins` per day can't come from a single-column aggregation (it needs
  event_kind AND pnl together), so it's computed separately from the closed-events-only subset,
  grouped by day, and merged back onto the daily table by day; `Win %`/`Loss %` are then derived
  from `Wins`/`Closed` (guarded against a day with zero closed trades, showing 0%/0% rather than
  dividing by zero) and inserted between Closed and PnL.
- Verified both computations against a small hand-built DataFrame outside Streamlit (mixed
  wins/losses/an open-only day) before wiring them in, since this page isn't covered by the
  automated test suite.
- Full suite re-run (353 passed) and dashboard restarted after each change.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Overview page redesign: combined, date-filtered Trading analysis section
**Asked:** "Redesign the Overview page. I like Strategy vs success ratio, hour / trade / P&L,
Daily ledger, combine all and add filters based on date parameter." Asked two clarifying
questions first (both answered with the recommended option): keep everything else on the page
(top metrics, Equity path chart, win/loss/profit-factor/drawdown, Current orders + Close-all
panel) exactly where it is, unfiltered; and use Period-preset radios (Today/This week/This
month/This year/All period) rather than a custom date-range picker, matching the P&L statement
page's existing pattern for consistency.

**What changed** (`dashboard/app.py`, `render_overview`): a new **"Trading analysis"** section
was added directly under the title/mode-badge divider, above everything else on the page. It has
its own `st.radio` Period filter (`key="overview_analysis_period"`) that computes a
`analysis_start` cutoff date the same way the P&L page's Period filter does, then filters
`trades` (by `exit_time`) and `activity` (by `timestamp`) into `trades_in_period`/
`activity_in_period`/`closed_activity_in_period` before rendering the three liked sections in one
place: **Strategy vs success ratio** (with a `{period} success ratio` metric card next to it),
**Hour / Trade / P&L**, and **Daily ledger**. Their previous standalone, unfiltered occurrences
further down the page were removed (selecting "All period" reproduces the old unfiltered view, so
nothing was lost, just consolidated) -- specifically the old `card_col`/`table_col` block, the old
`st.subheader("Daily ledger")` block, and the old `by_hour` computation/table (the `by_side`
table right next to it, which wasn't one of the three named sections, was left in place
unfiltered). Everything else on the page -- the two top metric rows, `render_control_center`,
Equity path chart, Rejection reasons, Average win/loss/Profit factor/Max drawdown, and Current
orders + Close-all-positions -- is untouched, in its original position, still unfiltered.
Full suite re-run (353 passed) and dashboard restarted; syntax-checked and the app starts
cleanly, but this page's actual rendering wasn't manually clicked through in a browser as part of
this change (no browser access in this environment) -- worth a quick look next time it's open.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Intratrading page: same Information-popover treatment, for dynamic content this time
**Asked:** Screenshot with a red box around 4 always-visible caption lines on the Intratrading
page (capital/risk summary, selected-stocks/strategy summary, the active strategy's rule text,
and "Scheduler is idle until auto trade is started") -- move these into an Information panel,
same as Live Monitor/P&L/Swing.

**What's different here vs. those three pages:** every other page's Information content was
static text, computed once. Intratrading's 4 lines are all dynamic (settings-derived, or
conditional on which strategy is selected, or on live scheduler state) and are computed at
different points across the function -- some past early-return branches (missing watchlist
selection, no Kite auth) that would otherwise prevent later lines from ever being known, and the
last line only exists inside a separate `@st.fragment(run_every="300s")` (`automatic_scheduler_fragment`).

**What changed** (`dashboard/app.py`, `render_automatic_trading`):
- Title row gained `info_column` (`st.columns([4, 1, 2], ...)`, was `[5, 2]`) with `info_popover_slot
  = info_column.empty()` -- the same "placeholder written to later in the script" mechanism
  `status_popover_slot` already uses in this exact function (confirmed it's called from 5+
  different branches/closures already, including from inside fragments).
- New local closure `render_info_panel(lines: list[str])` renders the current line list into
  `info_popover_slot` as an `.info-list` bullet popover -- called progressively as more lines
  become knowable: once with just the capital/risk summary (before the early-return checks, so
  at least that much shows even if the page stops there), again once the stocks/strategy summary
  and the strategy-specific rule are known, and a final time *inside* `automatic_scheduler_fragment`'s
  `else` branch (scheduler idle) with all 4 lines -- via closure over `info_lines`/`render_info_panel`,
  the same way the fragment already closes over other outer-scope state.
- The 4 `st.caption(...)` call sites are gone, replaced by building `capital_summary`,
  `stocks_summary`, `strategy_rule` (`None` for the one strategy branch that had no caption) and
  passing them to `render_info_panel`. The top-of-page subheading caption (not inside the user's
  red box) was left untouched.
- Full suite re-run (353 passed) and dashboard restarted after the changes.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Equal-width P&L cards; Information popovers on P&L and Swing pages
**Asked:** (1) Screenshot showing the 5-card P&L summary row with the hero card still wider than
the rest -- "no need of hero cart, all cart should be same size." (2) Add an "ℹ️ Information"
popover to the P&L statement's title row (same pattern as Live Monitor's), move its remaining
descriptive text into it as bold bullet points, and apply the same treatment to the Swing auto
trading page.

**What changed** (`dashboard/app.py`):
- **P&L summary row**: `st.columns([2, 1, 1, 1, 1], ...)` → `st.columns(5, ...)` -- all five cards
  (Net P&L after charges, Total P&L, Positions, Win rate, Avg P&L per trade) are now equal width.
- **P&L statement title row**: gained a `title_column, info_column = st.columns([6, 1], ...)`
  split with a new "ℹ️ Information" popover (bullet points, reusing `.info-list`) covering: what
  the page shows, what Gross P&L/Est. charges/After tax P&L mean, that Total P&L is always
  all-time while Period controls everything else, and how to use row selection. The
  now-redundant `st.caption("Select a row to see that trade's full activity...")` below the
  table was removed (folded into the popover instead).
- **Swing auto trading title row**: `title_column, status_column = st.columns([5, 2], ...)` →
  `title_column, info_column, status_column = st.columns([4, 1, 2], ...)`, with the same
  Information-popover pattern. Two previously always-visible captions were folded in as bullet
  points and removed from their old spots: the top-of-page strategy description ("Scans completed
  daily candles for a fresh EMA 9 cross above EMA 200...") and the safety note below the active
  positions table ("Stopping the scanner does not cancel these Zerodha-side protective orders...").
- Full suite re-run (353 passed) and dashboard restarted after the changes.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — P&L page: dropped subheading, Breakdown moved into the filter row, +2 cards (Win rate, Avg P&L per trade)
**Asked (screenshot-driven, in two steps):** (1) Suggested a 4th card to round out the 3-card
summary row from the previous entry -- agreed on "Win rate" (share of closed trades profitable
after charges) as genuinely new information, not just a relocated number. (2) A follow-up
screenshot showed the "Breakdown" popover (nested inside the hero card) rendering on its own row
below the cards, wasting vertical space, plus the page's top-of-page caption ("Every open
position (live) and every closed trade (realized)...") marked for removal. Asked to: drop that
caption, and move Breakdown up to sit in the filter row instead. Then asked for one more card;
suggested and added "Avg P&L per trade" (after charges) to read alongside Win rate -- one says
how often, the other how much.

**What changed** (`dashboard/app.py`, `render_pnl_statement_content`):
- Removed the page's top `st.caption(...)` subheading entirely.
- The filter row (`Show` / `Period` radios) gained a 5th column, `breakdown_column`. The
  `st.popover("Breakdown", ...)` block -- unchanged content (Gross P&L, Realized P&L, Unrealized
  P&L, Est. charges, = Net P&L) -- now renders into that column instead of being nested inside
  the hero metric card. Streamlit columns are layout placeholders that can be written to later in
  the script than where they're declared, so `breakdown_column` is declared up in the filter row
  (for correct visual position) but filled in further down, once the period figures it displays
  are actually computed -- no separate row, no wasted height.
- New **"Win rate"** card: `{wins}/{losses}` and `{rate}%` among `period_closed_rows`, judged on
  each row's `_net_pnl` (after charges), not gross P&L -- a trade that's gross-profitable but
  eaten alive by charges isn't a real win. Shows "No closed trades" when the period has none.
- New **"Avg P&L per trade"** card: `(period_realized_pnl - realized_charges_total) /
  closed_count` -- net, after-charges, average per closed trade in the period. Same "No closed
  trades" fallback.
- Summary row is now 5 cards (`[2, 1, 1, 1, 1]` column widths): Net P&L (after charges) [hero],
  Total P&L, Positions, Win rate, Avg P&L per trade.
- Full suite re-run (353 passed) and dashboard restarted after each step.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — P&L summary cards condensed from 7 to 3 (screenshot-driven)
**Asked:** Screenshot of the P&L statement's summary row -- 7 always-visible cards (Total P&L,
{period} P&L, Realized P&L, Unrealized P&L, Positions, Est. charges, Net P&L (after charges))
took up too much vertical space and were confusing (multiple similar-looking P&L numbers
competing for attention, e.g. Total P&L and This week P&L showing near-identical figures). Asked
for a plan first; recommended collapsing the supporting figures behind a popover and keeping only
the one number that matters plus light context, same declutter pattern as the Live Monitor
Information popover and the P&L page's own per-trade Charges breakdown popover -- approved as-is.

**What changed** (`dashboard/app.py`, `render_pnl_statement_content`): the two metric rows (5 +
2 cards) are now 3 cards: **"{Period} Net P&L (after charges)"** (hero, with a "Breakdown"
popover listing Gross P&L, Realized P&L, Unrealized P&L, and Est. charges as bullet points,
reusing the `.info-list` CSS class), **"Total P&L"** (all-time gross, unchanged figure/help
text), and **"Positions"** (unchanged). Realized P&L, Unrealized P&L, {period} gross P&L, and
Est. charges are no longer separate always-visible cards -- their numbers are unchanged, just
relocated into the Breakdown popover. Full suite re-run (353 passed) and dashboard restarted.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Fetch charges from the broker (Kite Connect Charges API) instead of only estimating locally
**Asked:** Follow-up to the charges feature above -- our own charges formula is a local
reimplementation of Zerodha's published rate card; the user asked whether we could instead fetch
the real number directly from the broker. Confirmed via kiteconnect's installed client source
(`connect.py:845`, route `/charges/orders`) and Zerodha's own docs
(kite.trade/docs/connect/v3/margins/) that Kite Connect has exactly this: `get_virtual_contract_note`,
a "Charges API" that takes a described order (real or hypothetical) and returns Zerodha's own
server-computed brokerage/STT/exchange-transaction/SEBI/stamp-duty/GST -- the same numbers used
to generate a real contract note, not an approximation. Implemented as a best-effort upgrade:
broker-verified when a live session is available, silently falling back to the existing local
estimate otherwise (no session, PAPER mode, API failure, or unexpected response shape).

**What changed:**
- New `app/broker/charges_api.py`: `ChargesAPI.fetch(orders)` calls
  `client.get_virtual_contract_note(orders)` and returns `None` (never raises) if there's no
  client, the order list is empty, the call fails, or the response isn't the same length as the
  request -- callers always have a clean "couldn't get it" signal to fall back on.
  `virtual_order(...)` builds one leg's request payload; `combine_broker_charges(buy, sell,
  dp_charges)` sums a round trip's two legs' `charges` dicts into our existing `ChargeBreakdown`
  dataclass (brokerage, transaction_tax→stt, exchange_turnover_charge→transaction_charges,
  sebi_turnover_charge→sebi_charges, stamp_duty, gst.total→gst) -- DP charges are NOT part of
  that API's response (it's a settlement-time fee, not an order-level one), so they're still
  supplied locally via `DELIVERY_DP_CHARGE_PER_SELL`, added on top regardless of which source the
  other six components came from. New `tests/test_charges_api.py` (7 tests).
- **`dashboard/app.py`, `render_pnl_statement_content`**: the Kite connection is now established
  whenever there's *any* trade (open or closed) to price/charge, not just when there are open
  positions (previously it only connected for the LTP lookup). New module-level
  `apply_broker_charges(rows, broker_client)`: batches every row's buy+sell legs into a single
  `get_virtual_contract_note` call (one network round trip for the whole table, not one per
  trade), then upgrades each row's charges fields in place when the call succeeds. New
  module-level `_BROKER_CHARGES_CACHE` memoizes a CLOSED trade's broker-verified result forever
  (both legs are final and never change) keyed by (exchange, tradingsymbol, product, quantity,
  buy price, sell price); an OPEN position is never cached, since its sell leg is today's moving
  LTP -- caching it would either never hit or serve a stale price's charges.
- **Consistency fix found while wiring this in**: the row-selection "Charges breakdown" popover
  previously always recomputed the LOCAL estimate's components via a helper, even when the
  row's headline "Est. charges" total had already been upgraded to the broker figure -- the
  popover's itemized numbers wouldn't have summed to the total shown next to it. Fixed by
  introducing one function, `update_row_charges(row, charges, source)`, as the single place that
  writes every charges-derived field on a row (`Est. charges`, `After tax P&L`, `_charges_total`,
  `_net_pnl`, and all 7 hidden components) from whichever `ChargeBreakdown` was actually used --
  called once when a row is first built (local estimate) and again by `apply_broker_charges` if
  it's later upgraded, so the popover, the column, and the aggregate cards always agree.
- The row-selection popover and its summary line now say **"broker-verified"** or **"estimated"**
  (plus "still open" or "final") so it's visible, per row, which source produced that number.
- `format_signed_currency` and the P&L%% helper (renamed `pnl_percentage`) were already/are now
  module-level (not nested inside a single page function), since both the row-building loop and
  the new broker-charges path need them.
- Full suite re-run (353 passed) and dashboard restarted after the changes.
- **Not yet verified against a real live session**: today is a non-trading Saturday with no
  active Kite access token in this environment, so the broker-verified path (as opposed to its
  unit tests, which mock the client) hasn't been exercised against Zerodha's actual API response
  shape yet. The response field names above are sourced from kiteconnect's own client code and
  Zerodha's published docs, but the first real trading-day check with a live session is the real
  confirmation this works end-to-end -- if the shape is off in some way the docs didn't capture,
  `ChargesAPI.fetch` returns `None` and the page falls back to the local estimate rather than
  showing wrong numbers or breaking the page.

**Files:** `app/broker/charges_api.py` (new), `dashboard/app.py`, `tests/test_charges_api.py`
(new), `CLAUDE.md`.

---

### 2026-09-19 — P&L table: combined Gross P&L, per-row Est. charges column, After tax P&L
**Asked:** Follow-up to the charges feature above -- on the P&L statement's main trades table,
combine "P&L"/"P&L %" into one column and rename it, add a per-row "Est. charges" column, and add
an "After tax P&L" column (same combined ₹amount (%) format as the renamed P&L column).

**What changed** (`dashboard/app.py`, `render_pnl_statement_content`):
- "P&L" and "P&L %" (previously two separate numeric columns) are now one combined text column,
  **"Gross P&L"** -- `₹X,XXX.XX (+Y.YY%)` -- reusing the same `"{amount} ({pct}%)"` format the
  Live Monitor page's position tables already use. Renamed (not left as "P&L") specifically so it
  reads clearly next to the new "After tax P&L" column.
- New **"Est. charges"** column: each row's `estimate_equity_charges(...).total` (from the charges
  feature above), shown as a plain ₹ number (`NumberColumn`), computed the same way for open
  (LTP-estimated) and closed (final) rows as the summary cards already do.
- New **"After tax P&L"** column: `Gross P&L − Est. charges`, same combined `₹amount (%)` text
  format as Gross P&L.
- `format_signed_currency` (previously local to `render_live_monitor_content` only) is now a
  module-level function so both pages share one implementation instead of two copies.
- Every row now also carries hidden numeric fields (`_pnl`, `_charges_total`, `_net_pnl`) so the
  aggregate cards and the per-row "Trade activity" expander (added in the charges feature above)
  read the same numbers the table displays, instead of recomputing them separately -- e.g.
  `unrealized_pnl` now sums `row["_pnl"]` and the period charges totals sum `row["_charges_total"]`
  rather than re-calling the charges function a second time per row.
- This reverses the "no table columns" choice made for the charges feature earlier today -- the
  user explicitly asked for these three specific columns this time, so the table now has 3 P&L-
  related columns (Gross P&L, Est. charges, After tax P&L) instead of the original 2 (P&L, P&L %).
- Full suite re-run (346 passed) and dashboard restarted after the changes.

**Files:** `dashboard/app.py`, `CLAUDE.md`.

---

### 2026-09-19 — Net P&L after brokerage + taxes (Zerodha charges) on the P&L statement page
**Asked:** The dashboard only ever showed gross P&L ((exit − entry) × quantity), so the user's
real P&L% (after Zerodha's brokerage, STT, transaction charges, SEBI charges, stamp duty, GST)
never matched what the dashboard showed. Requested: compute and show the real net P&L, based on
https://zerodha.com/charges/#tab-equities.

**Rates used** (fetched live from zerodha.com on 2026-09-19 -- equity cash market only, this app
never trades F&O): Delivery (SWING/CNC) -- ₹0 brokerage, STT 0.1% both legs, NSE transaction
charge 0.00307% of turnover, SEBI ₹10/crore of turnover, stamp duty 0.015% buy-side only, GST 18%
on (brokerage + SEBI + transaction charges). Intraday (INTRADAY/MIS) -- brokerage 0.03% or ₹20
per side whichever is lower, STT 0.025% sell-side only, same transaction/SEBI/GST rates, stamp
duty 0.003% buy-side only. Also added, per the user's explicit choice when asked: a flat ₹15.34
DP (Depository Participant) charge per scrip on every delivery sell (from zerodha.com/charges'
separate "Depository and other charges" tab, not the equities tab linked -- not applicable to
intraday, which never settles into a holding). These are government/exchange/broker rates that
can change -- re-verify at zerodha.com/charges if numbers look off, same spirit as the
MARKET_HOLIDAYS re-check note.

**What changed:**
- New `app/execution/charges.py`: pure function `estimate_equity_charges(position_type,
  buy_value, sell_value) -> ChargeBreakdown` (7 fields: brokerage, stt, transaction_charges,
  sebi_charges, stamp_duty, dp_charges, gst, plus a `.total` property). No I/O, no settings
  dependency -- same style as `app/market/trading_calendar.py`.
- **Correctness detail found during implementation** (not in the original ask, but necessary for
  "exact P&L"): intraday (MIS) positions can be SELL-side (short) entries -- confirmed via
  `app/execution/trading_pipeline.py:476` (`position_side = Side.BUY if signal.action ==
  SignalAction.BUY else Side.SELL`) -- unlike swing (CNC), which is always a BUY entry/SELL exit
  (`app/execution/swing_auto_trader.py:418,441`, since delivery can't be shorted). For a SELL-side
  position the *entry* order is the sell leg and the *exit/cover* is the buy leg -- the reverse of
  a normal long trade. `dashboard/app.py`'s new `_order_leg_values(side, entry_price, other_price,
  quantity)` maps entry/exit-or-LTP onto buy_value/sell_value based on `side`, not by assuming
  entry=buy -- otherwise STT and stamp duty (which depend on which literal order was the buy vs
  the sell) would be silently wrong for every short intraday trade.
- **P&L statement page** (`dashboard/app.py`, `render_pnl_statement_content`): every open/closed
  row now carries three hidden fields (`_position_type`, `_buy_value`, `_sell_value`) -- for open
  positions the sell leg is the current LTP (same quote already used for unrealized P&L, labeled
  "estimated" everywhere shown); for closed trades both legs are the real, final entry/exit
  prices. New `_row_charges(row)` helper computes each row's `ChargeBreakdown`. New period-aware
  aggregates `period_charges_total`/`period_net_pnl`/`period_net_pnl_pct` (realized leg scoped to
  the Period filter via the existing `period_closed_rows`, unrealized leg always current -- same
  split `period_pnl` already used). Two new metric cards below the existing 5: "Est. charges" and
  "Net P&L (after charges)"; "Total P&L"'s help text now notes it's the before-charges figure.
  Selecting a row in the trades table now shows a bold "Est. charges (estimated/final): ₹X → Net
  P&L: ₹Y" line plus a "Charges breakdown" popover (all 7 components as bullet points, reusing the
  `.info-list` CSS class added earlier today) inside the existing "Trade activity" expander -- no
  new columns were added to the main table, per the user's choice to keep it decluttered.
- New `tests/test_charges.py` (8 tests): delivery zero-brokerage/both-leg STT/DP-charge-only-when-
  sold, intraday sell-side-only STT/no-DP-charge/per-side brokerage cap, GST scoped to only
  brokerage+SEBI+transaction charges, transaction/SEBI charges scaling with total turnover, and
  `ChargeBreakdown.total` summing every component.
- Full suite re-run (346 passed) and dashboard restarted after the changes.

**Files:** `app/execution/charges.py` (new), `dashboard/app.py`, `tests/test_charges.py` (new),
`CLAUDE.md`.

---

### 2026-09-19 — Friendly broker-error text, Live Monitor Stop column rename, Information popover as bold bullets, hide LTP/SL on P&L table
**Asked (four things, screenshot-driven, in one turn):** (1) The Live Monitor "Needs attention"
popover showed a raw Python exception repr -- `broker position lookup failed: ('Connection
aborted.', RemoteDisconnected('Remote end closed connection without response'))` -- instead of a
user-friendly message. (2) Rename the "Stop" column to "Stop (stop dist%)" for clarity. (3) The
new "ℹ️ Information" popover (added earlier today) was hard to read as wrapped paragraphs -- make
it bullet points and increase the font weight. (4) On the P&L statement's open/closed table, hide
the LTP and SL Current Price columns.

**What changed:**
- **Friendly broker errors** (`app/execution/trailing_stop_agent.py`): new module-level
  `_describe_broker_error(error)` that recognizes raw network/connection exceptions (by type name
  -- `ConnectionError`, `RemoteDisconnected`, `ReadTimeout`, etc. -- and by scanning the message
  text for markers like "remotedisconnected"/"connection aborted"/"read timed out") and replaces
  them with "Lost connection to Zerodha -- this usually clears up on its own within a minute or
  two." (or an expired-token variant). Anything else -- including Kite's own already-readable
  `InputException` text like "Couldn't find that order_id" -- passes through unchanged, so the
  original JUBLPHARMA-style messages aren't affected. Wired into every place a raw exception was
  previously interpolated straight into `_intraday_broker_error`/`_swing_broker_error`/heartbeat
  `last_error`/the stop-trail failure message (8 call sites total). New
  `tests/test_describe_broker_error.py` (4 tests) covers the type-based match, the text-fallback
  match, the token-expiry case, and that a clean Kite message is left alone.
- **Live Monitor column rename** (`dashboard/app.py`): "Stop" → "Stop (stop dist%)" in both
  `build_rows` and `column_config` for the Intraday/Swing position tables.
- **Information popover readability** (`dashboard/app.py`): replaced the two `st.caption(...)`
  paragraphs (small, muted, low-contrast by Streamlit default) with a single `<ul class="info-
  list">` bullet list rendered via `st.markdown(..., unsafe_allow_html=True)`; added `.info-list`
  CSS (ink-colored, `font-weight: 600`, `.92rem`) so it reads clearly instead of as faint gray
  text.
- **P&L table decluttering** (`dashboard/app.py`): `column_config` for the open/closed trades
  table now hides `"LTP": None` and `"SL Current Price": None` (same technique already used to
  hide `_correlation_id`) -- the underlying values still drive the P&L math and open-position
  rows, only the table's own display is affected.
- Dashboard restarted; full suite re-run (338 passed) after the changes.

**Files:** `app/execution/trailing_stop_agent.py`, `dashboard/app.py`,
`tests/test_describe_broker_error.py`, `CLAUDE.md`.

---

### 2026-09-19 — P&L period filters, agent-start error clarity, NSE holiday list populated, Live Monitor captions collapsed into an Information popover
**Asked (four things, in one mid-turn batch):** (1) P&L statement's 5 metric cards were
confusing -- add date-range filters (Today by default, this week/month/year, all period), keep
Total P&L fixed/all-time, make the other cards re-render for the selected period; also widen the
cramped "Details" column in the trade-activity table. (2) Live Monitor's two long top-of-page
captions (screenshotted, red-boxed) consume too much space -- move them into a click-to-expand
"Information" popover like the existing status badge, same treatment on other pages if any have
the same problem, and move the page up. (3) Actually populate `MARKET_HOLIDAYS` in `.env` (left
empty on 2026-09-18 deliberately, pending real data). (4) When "Start agent now" is clicked on a
non-trading day or after hours, it currently just silently launches-and-immediately-exits with
no explanation -- show a clear, plain-language reason instead.

**What changed:**
- **P&L statement** (`dashboard/app.py`): new "Period" radio (Today/This week/This month/This
  year/All period, defaulting to Today) alongside the existing "Show" (All/Open/Closed) radio.
  "Total P&L" card stays all-time, unaffected by Period. The other four cards recompute against
  `closed_records_in_period` (closed trades whose exit date falls in the selected period; open
  positions are never period-filtered, since they're inherently "as of now" regardless of entry
  date) -- card 2 is now labeled `"{period} P&L"` (realized-in-period + always-current
  unrealized), card 3 is "Realized P&L" for the period, card 4 "Unrealized P&L" stays
  unconditional, card 5's closed count reflects the period. The trades table is filtered by the
  same period (via `period_closed_rows`). "Details" column in the trade-activity table: same
  fix as the Live Monitor "Calculation details" fix from earlier today -- no explicit width so
  Streamlit auto-sizes it from content instead of capping it at the fixed "large" preset.
- **Live Monitor**: both long captions (the page description, and the column-legend one) moved
  into a new `st.popover("ℹ️ Information", ...)` in the title row, next to the existing agent-
  status badge. A background search confirmed no other page has the same "long caption sitting
  directly under the title, always visible" pattern -- the only other 3+ line captions found are
  already inside their own expanders (Live Monitor's SL-M history section, P&L's trade-activity
  detail, Swing's scan-settings summary), which are already click-to-expand.
- **`.env`**: `MARKET_HOLIDAYS` populated with the actual 2026 NSE holiday list (16 dates),
  fetched and cross-checked against both Zerodha's and Groww's published calendars (independent
  sources agreed on all 16 dates). Verified live that it parses correctly and that Jan 26, 2026
  now correctly reads as a non-trading day.
- **Clear agent-start messaging**: new `describe_agent_start_blocked_reason(settings)` in
  `dashboard/app.py` -- returns a plain-language reason ("NSE is closed today..." /
  "Trading hours are over for today...") or `None`. Wired into both "Start agent now" buttons
  (Live Monitor's popover and the Kite-authentication page): the reason is shown as a caption
  and the button is disabled, instead of launching a process that immediately exits for the day
  with no visible explanation.

**Files:** `dashboard/app.py`, `.env`, `tests/test_agent_start_reason.py` (new, 5 tests). Full
suite: 334 passed. Dashboard restarted cleanly.

---

### 2026-09-19 — Live Monitor UI cleanup: dropped the overlapping eyebrow label, condensed position table columns, widened Calculation details
**Asked:** Three things from a screenshot of the Live Monitor page: (1) the small uppercase
"eyebrow" label above each page title was visibly overlapping/garbled at the very top of the
page -- remove it everywhere and move pages up; (2) the Intraday/Swing position tables have too
many columns -- add a green/red broker-status dot next to the symbol instead of a separate
column, and combine Stop+Stop distance %, ATR+ATR multiplier, and P&L+P&L% into one column each;
(3) the "Recent stop-loss updates" table's Calculation details column (the most important field)
is still cramped while Time/Symbol/From/To take more space than their content needs.

**Root cause of (1):** `[data-testid="stMainBlockContainer"] { padding-top: 1.25rem; }` left too
little clearance below Streamlit's own fixed header bar, so the first element on any page (the
eyebrow) rendered underneath/overlapping it.

**What changed:**
- Removed all 14 page-top eyebrow labels (`<div class="eyebrow">...</div>` directly before a
  page's `st.title(...)`) across the whole app. Left the one unrelated `eyebrow`-styled label
  inside the Scanner & signals page's "Signal engine" status card alone -- a different element,
  not implicated in the overlap and not a page title.
- `padding-top` raised from `1.25rem` to `3rem` -- enough to clear Streamlit's header (fixing
  the overlap) while remaining well short of Streamlit's spacious unstyled default.
- Live Monitor's Intraday/Swing position tables: `Symbol` now shows a 🟢/🔴/⚪ dot (matches
  broker / not found at broker / status unknown) instead of a separate "Broker status" column;
  `Stop` shows `₹trigger (distance%)`; `ATR (mult.)` shows `₹atr (×multiplier)`; `P&L` shows
  `₹pnl (pnl%)`; `Target 1` folds in "Target 1 hit" as a trailing checkmark; `Stop updates`
  folds in "Last stop move" as `count (old → new)`. Net: 19 columns down to 13. Row-selection
  (used to filter the stop-loss history below and to open the manual-exit panel) now strips the
  dot back off the displayed "🟢 NSE:X" value before matching it against the plain
  `record.symbol` used everywhere downstream.
- "Recent stop-loss updates" table: Streamlit's `column_config` only offers fixed
  `"small"/"medium"/"large"` presets or an exact pixel width -- there's no true "stretch to fill
  remaining space" option, so "large" was still a fixed cap regardless of how much room was
  actually available. Removed the explicit width from `Calculation details` entirely (kept
  `Time`/`Symbol`/`From`/`To` at `"small"`) so Streamlit auto-sizes it from its own content
  instead, letting it take up most of the table's width.

**Files:** `dashboard/app.py`. Full suite: 329 passed (no dashboard-rendering-layer tests exist
for these functions, per this session's earlier finding -- verified via syntax/import checks and
a live dashboard restart instead).

---

### 2026-09-19 — Trading only runs on actual trading days: skip weekends and configured holidays
**Asked:** After explaining the "Couldn't find that `order_id`" error for NSE:JUBLPHARMA (root
cause: Sept 19, 2026 is a Saturday, so the swing position's day-order-based protective stop had
already expired and Zerodha rejected both the re-arm and the subsequent modify attempt with
"markets are closed") -- the user asked to stop placing orders and stop even starting the
trailing-stop agent on weekends and holidays.

**What changed:**
- New `app/market/trading_calendar.py`: `is_trading_day(date, holidays)` (false on Saturday/
  Sunday or a configured holiday) and `parse_market_holidays(raw)` (parses a comma-separated
  `YYYY-MM-DD` list). New `Settings.market_holidays: str = ""` / `.env` key `MARKET_HOLIDAYS`
  -- deliberately left **empty by default** rather than pre-populated with a guessed NSE holiday
  calendar, since a wrong guess would silently skip a real trading day, which is worse than
  weekends-only coverage. User should copy the current year's list from NSE's official published
  calendar into `.env` if holiday coverage (not just weekends) is wanted.
- `TrailingStopAgent.run_once`: checks `is_trading_day` first, before even the existing
  shutdown-time check -- on a non-trading day it exits for the day immediately (same clean
  heartbeat-clearing exit already used for "past shutdown time", now shared via a new
  `_exit_for_the_day` helper), touching the broker not at all.
- `agent_launcher.maybe_autostart_trailing_agent`: also checks trading-day before spawning --
  stops the agent from being launched-then-immediately-exiting on login on a non-trading day.
  All three dashboard call sites (initial Kite connect, manual-token-paste, and the Live
  Monitor self-heal restart from 2026-09-18) now pass `market_holidays=settings.market_holidays`.
- Intratrading `run_automatic_cycle` and Swing `render_swing_content`: both now skip scanning
  entirely on a non-trading day (a calm "⚪ Market closed" badge / warning, not the alarming
  "🔴 Needs attention" halt_scan() styling used for real problems, since this is expected).

**Found & cleaned up while testing this:** a third stray process this session -- the actual
standalone `trailing_stop_agent.py` subprocess running old code, plus yet another duplicate
dashboard on port 8502. All stopped; the agent deliberately was **not** relaunched today (a real
Saturday) per the feature just added -- confirmed live against production settings/DB that
`maybe_autostart_trailing_agent` now correctly returns `None` today.

**Files:** `app/market/trading_calendar.py` (new), `app/config/settings.py`, `.env`,
`app/execution/trailing_stop_agent.py`, `app/execution/agent_launcher.py`, `dashboard/app.py`,
`tests/test_trading_calendar.py` (new, 8 tests), plus new tests in `test_trailing_stop_agent.py`
and `test_agent_launcher.py`. Also fixed 9 pre-existing test dates that happened to land on a
real Saturday (`2025-02-01`) and would otherwise have started failing today for an unrelated
reason once the new weekday check existed. Full suite: 329 passed (run today, a real Saturday,
as a live check that nothing is accidentally date-order-dependent).

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
