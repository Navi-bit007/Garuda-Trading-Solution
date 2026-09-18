from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, time as time_of_day, timedelta
from threading import Event, Lock, Thread, current_thread
from time import sleep
from typing import Any

import pandas as pd

from app.broker.market_data import MarketData
from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side
from app.database.models import ActivityRecord, AgentHeartbeat, DecisionLogRecord, PositionRecord, TradeRecord
from app.database.repository import Repository
from app.execution.exit_actions import close_position_at_market, correlation_id_for, exchange_of, tradingsymbol_of
from app.execution.swing_trailing import compute_ema_swing_stop, compute_trend_breakout_stop
from app.execution.trailing_stop import TrailingStop
from app.market.candles import validate_ohlcv
from app.market.indicators import atr as compute_atr
from app.monitoring.notifications import Notifier

logger = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 15.0
DEFAULT_SWING_RECOMPUTE_SECONDS = 300.0
DEFAULT_ATR_REFRESH_SECONDS = 300.0
DEFAULT_ATR_PERIOD = 14
DEFAULT_SWING_LOOKBACK_DAYS = 600
MAX_MODIFY_ATTEMPTS = 3
# Statuses that mean the exchange has definitively dropped the order (day-order expiry at
# market close, a manual cancel, or a broker rejection) -- anything else (TRIGGER PENDING,
# OPEN, ...) is treated as still live, since we'd rather risk one extra check next cycle than
# place a duplicate protective order on ambiguous/transient data.
TERMINAL_INACTIVE_ORDER_STATUSES = {"CANCELLED", "REJECTED"}
# Zerodha caps the number of times a single order can be modified (25, confirmed live) --
# after that every modify_order() call rejects with this message regardless of how valid the
# new trigger price is. There's no way to reset that count on the same order_id, so once it's
# hit, the only way to keep trailing is to cancel the capped order and place a fresh one.
MODIFICATION_LIMIT_ERROR_TEXT = "maximum allowed order modification"


@dataclass
class AgentPosition:
    record: PositionRecord
    instrument_token: int | None
    trailing_stop: TrailingStop
    last_trailing_candle: datetime
    lock: Lock = field(default_factory=Lock)


class TrailingStopAgent:
    """Standalone, broker-synced trailing stop-loss for every open intraday and swing position.

    Zerodha's Kite Connect GTT API has no server-side trailing trigger (only fixed-price
    "single"/"two-leg" triggers), so this process replaces it: it watches live price for each
    open position and walks the resting SL-M order at the broker as price moves favorably.

    This is the sole owner of ``OrderAPI.modify_protective_stop``/``.cancel`` for an
    already-open position's protective stop. Placing the stop at entry and cancelling it at a
    deliberate exit stay with the code that opens/closes the trade (``TradingPipeline``,
    ``SwingAutoTrader``) -- those are one-shot actions, not concurrent with trailing.
    """

    def __init__(
        self,
        settings: Any,
        repository: Repository,
        orders: OrderAPI,
        market_data: MarketData,
        broker_client: Any = None,
        notifier: Notifier | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        swing_recompute_seconds: float = DEFAULT_SWING_RECOMPUTE_SECONDS,
        atr_refresh_seconds: float = DEFAULT_ATR_REFRESH_SECONDS,
        atr_period: int = DEFAULT_ATR_PERIOD,
        modify_retry_attempts: int = MAX_MODIFY_ATTEMPTS,
        modify_retry_backoff_seconds: float = 1.0,
    ):
        self.settings = settings
        self.repository = repository
        self.orders = orders
        self.market_data = market_data
        self.broker_client = broker_client
        self.notifier = notifier or Notifier()
        self.modify_retry_attempts = modify_retry_attempts
        self.modify_retry_backoff_seconds = modify_retry_backoff_seconds
        self.poll_seconds = poll_seconds
        self.swing_recompute_seconds = swing_recompute_seconds
        self.atr_refresh_seconds = atr_refresh_seconds
        self.atr_period = atr_period
        self.positions: dict[str, AgentPosition] = {}
        self._atr_cache: dict[str, tuple[datetime, float]] = {}
        self._last_swing_recompute_at: datetime | None = None
        self._stop_event = Event()
        self._thread: Thread | None = None
        self.user_id = str(getattr(settings, "user_id", "default"))
        # Tracked separately, not as one shared flag: intraday reconciliation runs every cycle
        # (DEFAULT_POLL_SECONDS) so it can safely reset its own error each time it re-checks, but
        # swing reconciliation only gets a chance to re-check every swing_recompute_seconds -- a
        # single shared flag reset every cycle would make a genuine, still-unresolved swing
        # problem (like a position sitting unprotected) disappear from the heartbeat within
        # seconds, long before the next swing check could confirm whether it's actually fixed.
        self._intraday_broker_error = ""
        self._swing_broker_error = ""
        self.shutdown_time = getattr(settings, "agent_shutdown_time", time_of_day(15, 40))
        # Zerodha charges an auto square-off penalty for any MIS (intraday) position still open
        # past its own RMS cutoff -- force_exit is always validated to fall before shutdown_time
        # (see Settings.agent_shutdown_after_force_exit), so every open intraday position gets a
        # market exit here well before the agent itself stops watching for the day.
        self.force_exit_time = getattr(settings, "force_exit", time_of_day(15, 15))
        # Without a floor, any improvement at all -- even a paisa -- sends a broker-side SL-M
        # modify, which is what burns through Zerodha's 25-modifications-per-order cap on noise
        # rather than real moves. Expressed as a percentage of the reference price (matching how
        # "Stop distance %" is already shown on the dashboard), not a fixed rupee amount, so it
        # scales sensibly across a Rs.50 stock and a Rs.5,000 one.
        self.min_stop_improvement_pct = float(getattr(settings, "min_stop_improvement_pct", 0.25))
        self.shut_down_for_the_day = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop_event.clear()
        self._thread = Thread(target=self._run, name="trailing-stop-agent", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop_event.set()
        if self._thread is not None and self._thread is not current_thread():
            self._thread.join(timeout=timeout)
        stopped = not self.running
        if stopped:
            self._thread = None
        return stopped

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Trailing stop agent cycle failed")
            self._stop_event.wait(self.poll_seconds)

    def run_once(self, now: datetime | None = None) -> None:
        timestamp = now or datetime.now()
        if timestamp.time() >= self.shutdown_time:
            # There's nothing left to do once the trading day is over -- Zerodha's own SL-M
            # orders are day orders that expire at market close regardless of whether this
            # process is alive, and swing stops only need re-arming once trading resumes
            # tomorrow (handled fresh by _reconcile_swing_positions on that day's first cycle).
            # Rather than sit idle overnight burning an OS process and racing tomorrow's Kite
            # token expiry, exit for the day; the dashboard's auto-start-on-login relaunches a
            # fresh process the next time the user actually opens the app and signs in.
            logger.info("Past today's shutdown time (%s); trailing stop agent is exiting for the day", self.shutdown_time)
            self.shut_down_for_the_day = True
            self._stop_event.set()
            try:
                # Clear rather than leave a stale heartbeat behind -- an aged-out heartbeat
                # reads on the dashboard as "stalled: positions may not be protected, check the
                # process", which is alarming for what is actually an intentional, clean
                # end-of-day exit. No heartbeat at all reads as the calmer "not started yet".
                self.repository.clear_agent_heartbeat("trailing_stop_agent")
            except Exception:
                logger.exception("Failed to clear the heartbeat on end-of-day shutdown")
            return
        last_error = ""
        try:
            self._refresh_broker_session()
            self.load_positions()
            self._process_intraday(timestamp)
            due = self._last_swing_recompute_at is None or (timestamp - self._last_swing_recompute_at).total_seconds() >= self.swing_recompute_seconds
            if due:
                self._process_swing(timestamp)
                self._last_swing_recompute_at = timestamp
        except Exception as error:
            last_error = str(error)
            raise
        finally:
            # A broker call failing inside _process_intraday/_process_swing (e.g. an expired
            # Kite session) is caught and logged there so one bad symbol can't block the rest of
            # the cycle -- but that means it would otherwise never reach here, and the heartbeat
            # would keep reporting a clean "RUNNING" state while every broker call is silently
            # failing. Surface it on the heartbeat too so a stale/expired session is visible on
            # the dashboard instead of looking identical to a healthy agent.
            try:
                combined_error = last_error or self._intraday_broker_error or self._swing_broker_error
                self.repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", timestamp, combined_error, timestamp))
            except Exception:
                logger.exception("Failed to record trailing-stop agent heartbeat")

    def _refresh_broker_session(self) -> None:
        """Pick up a newly generated Kite access token without needing a process restart.

        Zerodha access tokens expire once every trading day; this agent is meant to run for as
        long as any position (especially a multi-day swing one) stays open, so it will always
        outlive its token. Rather than requiring a manual kill-and-relaunch each morning, check
        the same `kite_session` row the dashboard writes to on every login and hot-swap the
        broker client's token the moment it changes.
        """
        if self.broker_client is None or not hasattr(self.broker_client, "set_access_token"):
            return
        try:
            latest_token = self.repository.load_kite_access_token(self.user_id)
        except Exception:
            logger.exception("Failed to check for a refreshed Kite access token")
            return
        current_token = getattr(self.broker_client, "access_token", None)
        if latest_token and latest_token != current_token:
            self.broker_client.set_access_token(latest_token)
            logger.info("Trailing-stop agent picked up a refreshed Kite access token")
            self.notifier.send("Trailing-stop agent refreshed its Kite session with a newly generated access token")

    # -- position bookkeeping -------------------------------------------------

    def load_positions(self) -> None:
        # PAPER-mode positions have no real broker order to protect -- and the shared `positions`
        # table carries no other guarantee that a row is safe for this process to act on -- so
        # anything not explicitly tagged LIVE is left alone entirely.
        records = {
            record.symbol: record
            for record in self.repository.load_positions()
            if record.trading_mode == "LIVE"
        }
        for symbol in list(self.positions):
            if symbol not in records:
                del self.positions[symbol]
        for symbol, record in records.items():
            existing = self.positions.get(symbol)
            if existing is not None:
                record, instrument_token = self._ensure_instrument_token(symbol, record)
                existing.record = record
                existing.instrument_token = instrument_token
                continue
            record, instrument_token = self._ensure_instrument_token(symbol, record)
            atr_multiplier = record.atr_multiplier or float(getattr(self.settings, "trailing_atr_multiplier", 1.5))
            trailing_stop = TrailingStop(record.entry_price, record.stop_loss, atr_multiplier, record.side)
            self.positions[symbol] = AgentPosition(
                record=record,
                instrument_token=instrument_token,
                trailing_stop=trailing_stop,
                last_trailing_candle=record.entry_time,
            )

    def _ensure_instrument_token(self, symbol: str, record: PositionRecord) -> tuple[PositionRecord, int | None]:
        """Resolve and persist the instrument token if the position doesn't have one yet.

        Called every load_positions() cycle for a still-unresolved position, not just once --
        the lookup is a live Kite API call (see _resolve_instrument_token) that can fail on a
        transient network blip (a dropped connection, a momentary timeout). Retrying only on
        first load meant that one bad moment left a position permanently blind to price/ATR --
        and therefore stuck with a frozen stop-loss -- for the rest of its lifetime, since
        nothing ever tried the lookup again.
        """
        if record.instrument_token is not None:
            return record, record.instrument_token
        instrument_token = self._resolve_instrument_token(symbol)
        if instrument_token is not None:
            record = replace(record, instrument_token=instrument_token)
            self.repository.save_position(record)
        return record, instrument_token

    def _resolve_instrument_token(self, symbol: str) -> int | None:
        if self.broker_client is None or not hasattr(self.broker_client, "instruments"):
            return None
        tradingsymbol = self._tradingsymbol(symbol)
        try:
            instruments = self.broker_client.instruments(self._exchange(symbol))
        except Exception:
            logger.exception("Instrument lookup failed for %s", symbol)
            return None
        for instrument in instruments:
            if str(instrument.get("tradingsymbol", "")).strip().upper() == tradingsymbol:
                try:
                    return int(instrument["instrument_token"])
                except (KeyError, TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _tradingsymbol(symbol: str) -> str:
        return tradingsymbol_of(symbol)

    @staticmethod
    def _exchange(symbol: str) -> str:
        return exchange_of(symbol)

    @staticmethod
    def _candles_frame(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        if "date" in frame.columns and "timestamp" not in frame.columns:
            frame = frame.rename(columns={"date": "timestamp"})
        return validate_ohlcv(frame)

    @staticmethod
    def _correlation_id(record: PositionRecord) -> str:
        """A stable id spanning one position's whole life, for joining every decision-log row
        (entry, each stop trail, target hit, exit) even though `protective_order_id` itself
        changes (an overnight re-arm, or a modification-cap replacement). `entry_time` never
        changes once a position is open, so symbol+entry_time is the closest thing this app has
        to a trade id.
        """
        return correlation_id_for(record)

    def _log_decision(
        self,
        record: PositionRecord,
        event_type: str,
        decision: str,
        rationale: str,
        inputs: dict[str, object] | None = None,
        outputs: dict[str, object] | None = None,
    ) -> None:
        self.repository.save_decision(
            DecisionLogRecord(
                timestamp=datetime.now(),
                symbol=record.symbol,
                event_type=event_type,
                strategy_name=record.strategy_name or "",
                mode=record.trading_mode,
                decision=decision,
                rationale=rationale,
                inputs=inputs or {},
                outputs=outputs or {},
                correlation_id=self._correlation_id(record),
                source_table="positions",
                source_id=record.symbol,
            )
        )

    # -- intraday: tick-price-driven ATR ratchet -------------------------------

    def _process_intraday(self, timestamp: datetime) -> None:
        positions = [position for position in self.positions.values() if position.record.position_type == "INTRADAY"]
        if not positions:
            return
        # Reset right before actually attempting this cycle's intraday broker calls, not
        # unconditionally at the top of run_once -- this method runs every poll_seconds, so a
        # persisting problem re-sets itself immediately below if it's still there.
        self._intraday_broker_error = ""
        try:
            self._reconcile_intraday_positions(positions, timestamp)
        except Exception as error:
            logger.exception("Intraday broker reconciliation failed")
            self._intraday_broker_error = f"intraday reconciliation failed: {error}"
        positions = [position for position in self.positions.values() if position.record.position_type == "INTRADAY"]
        if not positions:
            return
        keys = [f"{self._exchange(p.record.symbol)}:{self._tradingsymbol(p.record.symbol)}" for p in positions]
        try:
            quotes = self.market_data.ltp(keys)
        except Exception as error:
            logger.exception("LTP fetch failed for intraday positions")
            self._intraday_broker_error = f"LTP fetch failed: {error}"
            return
        force_exit_due = timestamp.time() >= self.force_exit_time
        for position, key in zip(positions, keys):
            quote = quotes.get(key)
            price = None
            if quote:
                try:
                    price = float(quote["last_price"])
                except (KeyError, TypeError, ValueError):
                    price = None
            if price is not None and price <= 0:
                price = None
            if force_exit_due:
                self._force_exit_position(position, price or position.record.entry_price, timestamp)
                continue
            if price is None:
                continue
            self._check_target_1(position, price)
            atr_value = self._resolve_atr(position, timestamp)
            if atr_value <= 0:
                continue
            previous_stop = position.record.stop_loss
            side = position.record.side
            raw_candidate = position.trailing_stop.update(price, atr_value)
            distance = position.trailing_stop.atr_multiplier * atr_value
            operator = "−" if side == "BUY" else "+"
            candidate = raw_candidate
            floored_note = ""
            if position.record.target_1_hit:
                entry_price = position.record.entry_price
                floored = max(raw_candidate, entry_price) if side == "BUY" else min(raw_candidate, entry_price)
                if floored != raw_candidate:
                    floored_note = f"; floored to breakeven entry ₹{entry_price:.2f} (target 1 hit)"
                candidate = floored
                position.trailing_stop.stop = candidate
            # "moved from X to Y" is the actual new stop (post breakeven-floor, if that kicked
            # in); "Calc" is always the raw ATR arithmetic that produced it, so the two numbers
            # can legitimately differ by design, not by error, when the floor overrides it.
            calculation = (
                f"trailing stop moved from ₹{previous_stop:.2f} to ₹{candidate:.2f} - "
                f"Calc: ATR14 ₹{atr_value:.2f} × mult {position.trailing_stop.atr_multiplier:.2f} = ₹{distance:.2f}; "
                f"Last ₹{price:.2f} {operator} ₹{distance:.2f} = ₹{raw_candidate:.2f}{floored_note}"
            )
            self._maybe_apply(position, candidate, price, calculation)

    def _check_target_1(self, position: AgentPosition, price: float) -> None:
        """Flip target_1_hit the moment live price reaches the strategy's target-1 level.

        Nothing else does this for a position this agent manages: TradingPipeline.monitor_ticks()
        is the only other code that ever sets this flag, and it only runs while the separate
        "Intraday desk" dashboard page happens to be open in an active session. Without this
        check, target_1_hit -- and the breakeven floor a few lines below that depends on it --
        would sit permanently False for every LIVE position, no matter how far price ran.
        """
        record = position.record
        if record.target_1_hit or record.target_1 is None:
            return
        reached = price >= record.target_1 if record.side == "BUY" else price <= record.target_1
        if not reached:
            return
        updated = replace(record, target_1_hit=True)
        self.repository.save_position(updated)
        position.record = updated
        logger.info("%s reached target 1 (%.2f); stop now floors at breakeven", record.symbol, record.target_1)
        self.notifier.send(f"{record.symbol} reached target 1 (₹{record.target_1:.2f}) -- stop now floors at breakeven")
        self._log_decision(
            updated,
            event_type="target_hit",
            decision="FLOOR_TO_BREAKEVEN",
            rationale=f"Live price ₹{price:.2f} reached target 1 (₹{record.target_1:.2f}); stop now floors at breakeven entry ₹{record.entry_price:.2f} instead of trailing below it.",
            inputs={"price": price, "target_1": record.target_1, "entry_price": record.entry_price},
        )

    def _force_exit_position(self, position: AgentPosition, price: float, timestamp: datetime) -> None:
        """Market-close an intraday position once the configured force-exit time (Risk &
        settings) has passed, and cancel its resting SL-M -- a stale protective order left
        behind after a market exit could otherwise fire against a position that no longer
        exists. Without this, an intraday position that never hit its stop would sit open past
        Zerodha's own RMS cutoff, and Zerodha charges an auto square-off penalty for closing it
        instead of the client doing so first.
        """
        record = position.record
        reason = "Force close; Auto Square off"
        outcome = close_position_at_market(
            self.repository,
            self.orders,
            record,
            price,
            timestamp,
            reason=reason,
            event_kind="force_exit",
            decision="FORCE_CLOSE",
            notifier=self.notifier,
        )
        if not outcome.success:
            logger.error("Force-exit market order failed for %s: %s", record.symbol, outcome.error)
            self._intraday_broker_error = f"force-exit order failed for {record.symbol}: {outcome.error}"
            return
        if outcome.cancel_error:
            logger.error(
                "Failed to cancel protective stop %s while force-exiting %s: %s",
                record.protective_order_id,
                record.symbol,
                outcome.cancel_error,
            )
        self.positions.pop(record.symbol, None)
        exit_side = Side.SELL if record.side == "BUY" else Side.BUY
        logger.info("force_exit %s side=%s price=%.2f pnl=%.2f", record.symbol, exit_side.value, price, outcome.pnl)

    def _reconcile_intraday_positions(self, positions: list[AgentPosition], timestamp: datetime) -> None:
        """Detect an intraday position that closed at the broker -- a protective SL-M fill, a
        manual exit placed directly at Zerodha, or an entry order that was accepted (and
        briefly persisted as a position) but then rejected moments later -- without any other
        code path in this app catching it.

        Previously the only code that did this for intraday positions was
        TradingPipeline.sync_broker_positions(), and it only ran while a Streamlit dashboard
        session happened to be open and rerunning. This process is always running once
        launched, independent of the dashboard, so it's the right place for the safety net.
        Checked every cycle (unlike swing, which is reconciled once a day) since an intraday
        position can close at any moment during the session. Mirrors
        _reconcile_swing_positions.
        """
        if self.broker_client is None or not hasattr(self.broker_client, "positions"):
            return
        try:
            broker_positions = self.broker_client.positions().get("net", [])
        except Exception as error:
            logger.exception("Broker position lookup failed during intraday reconciliation")
            self._intraday_broker_error = f"broker position lookup failed: {error}"
            return
        broker_by_tradingsymbol = {str(entry.get("tradingsymbol", "")).strip().upper(): entry for entry in broker_positions}
        for position in positions:
            # One symbol's lookup/DB error must never block reconciliation of the others in
            # this cycle -- otherwise a single persistently-failing position (e.g. a broker
            # order_history call that keeps erroring for it) would leave every other closed
            # position stuck showing as open indefinitely, not just the one at fault.
            try:
                self._reconcile_one_intraday_position(position, broker_by_tradingsymbol, timestamp)
            except Exception:
                logger.exception("Reconciliation failed for %s; will retry next cycle", position.record.symbol)

    def _reconcile_one_intraday_position(self, position: AgentPosition, broker_by_tradingsymbol: dict, timestamp: datetime) -> None:
        record = position.record
        broker_position = broker_by_tradingsymbol.get(self._tradingsymbol(record.symbol))
        broker_quantity = int(broker_position.get("quantity", 0) or 0) if broker_position else 0
        if broker_quantity != 0:
            self._reconcile_entry_price(position, broker_position)
            return
        exit_price, reason = self._broker_exit_fill(record)
        multiplier = 1 if record.side == "BUY" else -1
        pnl = multiplier * (exit_price - record.entry_price) * record.quantity
        # Atomically claim this close before recording it -- the dashboard's own TradingPipeline
        # independently polls the broker and can notice this exact same close at nearly the same
        # moment. Only the caller whose claim actually removes the `positions` row proceeds to
        # write a trade/activity record; the loser (None) just drops its own in-memory tracking,
        # with nothing left to race against.
        if self.repository.claim_position_close(record.symbol) is None:
            self.positions.pop(record.symbol, None)
            return
        self.repository.save_trade(
            TradeRecord(
                symbol=record.symbol,
                entry_time=record.entry_time,
                exit_time=timestamp,
                entry_price=record.entry_price,
                exit_price=exit_price,
                quantity=record.quantity,
                pnl=pnl,
                side=record.side,
                position_type="INTRADAY",
                strategy_name=record.strategy_name,
                exit_reason=reason,
            )
        )
        self.positions.pop(record.symbol, None)
        exit_side = "SELL" if record.side == "BUY" else "BUY"
        self.repository.save_activity(
            ActivityRecord(
                event_kind="broker_exit_detected",
                symbol=record.symbol,
                timestamp=timestamp,
                mode="LIVE",
                price=exit_price,
                order_id=record.protective_order_id,
                side=exit_side,
                quantity=record.quantity,
                entry_price=record.entry_price,
                stop_loss=record.stop_loss,
                pnl=pnl,
                reason=reason,
            )
        )
        logger.info("broker_exit_detected %s side=%s price=%.2f pnl=%.2f", record.symbol, exit_side, exit_price, pnl)
        self.notifier.send(f"broker_exit_detected {record.symbol} side={exit_side} price={exit_price:.2f} pnl={pnl:.2f} reason={reason}")
        self._log_decision(
            record,
            event_type="exit",
            decision="EXIT",
            rationale=reason,
            inputs={"exit_side": exit_side},
            outputs={"exit_price": exit_price, "pnl": pnl},
        )
        self.repository.link_decision_outcome(self._correlation_id(record), {"exit_price": exit_price, "pnl": pnl, "exit_reason": reason})

    def _reconcile_entry_price(self, position: AgentPosition, broker_position: dict) -> None:
        """Correct a persisted position's entry price to match Zerodha's own average price.

        A MARKET entry can fill a little away from the price it was signalled/logged at, and
        nothing else ever revisits a position's entry price once it's open -- without this, the
        dashboard's monitoring page and PnL keep showing a stale figure for as long as the
        position stays open, even after the reconciliation flagged the actual accepted fill.
        Runs every cycle, independent of whether a dashboard session is open.
        """
        record = position.record
        try:
            broker_entry_price = float(broker_position.get("average_price", 0) or 0)
        except (TypeError, ValueError):
            return
        if broker_entry_price <= 0 or abs(broker_entry_price - record.entry_price) < 0.01:
            return
        logger.info("Correcting entry price for %s from %.2f to broker average price %.2f", record.symbol, record.entry_price, broker_entry_price)
        updated = replace(record, entry_price=broker_entry_price)
        self.repository.save_position(updated)
        position.record = updated

    def _resolve_atr(self, position: AgentPosition, timestamp: datetime) -> float:
        symbol = position.record.symbol
        cached = self._atr_cache.get(symbol)
        if cached is not None and (timestamp - cached[0]).total_seconds() < self.atr_refresh_seconds:
            return cached[1]
        fallback = cached[1] if cached is not None else 0.0
        if position.instrument_token is None:
            return fallback
        try:
            rows = self.market_data.historical(position.instrument_token, timestamp - timedelta(days=5), timestamp, "15minute")
            frame = self._candles_frame(rows)
            if len(frame) <= self.atr_period:
                return fallback
            value = float(compute_atr(frame, self.atr_period).iloc[-1])
        except Exception:
            logger.exception("ATR refresh failed for %s", symbol)
            return fallback
        if pd.notna(value) and value > 0:
            self._atr_cache[symbol] = (timestamp, value)
            return value
        return fallback

    # -- swing: daily-candle-driven trail --------------------------------------

    def _process_swing(self, timestamp: datetime) -> None:
        if not any(position.record.position_type == "SWING" for position in self.positions.values()):
            return
        # Reset right before actually attempting this cycle's swing broker calls -- this method
        # only runs once every swing_recompute_seconds (unlike intraday's every-poll cadence), so
        # a genuine, still-unresolved problem (e.g. a position sitting unprotected) must persist
        # on the heartbeat across the many intraday-only cycles in between, not just the single
        # cycle it was first detected on.
        self._swing_broker_error = ""
        try:
            self._reconcile_swing_positions(timestamp)
        except Exception as error:
            logger.exception("Swing broker reconciliation failed")
            self._swing_broker_error = f"swing reconciliation failed: {error}"
        for position in [p for p in self.positions.values() if p.record.position_type == "SWING"]:
            try:
                self._process_swing_position(position, timestamp)
            except Exception as error:
                logger.exception("Swing trailing update failed for %s", position.record.symbol)
                self._swing_broker_error = f"swing trailing update failed for {position.record.symbol}: {error}"

    def _reconcile_swing_positions(self, timestamp: datetime) -> None:
        """Detect an exchange-expired overnight SL-M and re-place it, and drop positions the
        broker no longer holds (closed by a fill nothing else caught, e.g. while no dashboard
        was open). NSE "regular" orders -- including the SL-M this app places -- are day
        orders: Zerodha cancels them at market close, so a swing (multi-day) position's stop
        would otherwise sit unprotected every morning until something re-arms it.
        """
        positions = [position for position in self.positions.values() if position.record.position_type == "SWING"]
        if not positions or self.broker_client is None or not hasattr(self.broker_client, "positions"):
            return
        try:
            broker_positions = self.broker_client.positions().get("net", [])
        except Exception as error:
            logger.exception("Broker position lookup failed during swing reconciliation")
            self._swing_broker_error = f"broker position lookup failed: {error}"
            return
        broker_by_tradingsymbol = {str(entry.get("tradingsymbol")): entry for entry in broker_positions if int(entry.get("quantity", 0) or 0) != 0}
        held_tradingsymbols = self._broker_held_tradingsymbols()
        for position in positions:
            record = position.record
            tradingsymbol = self._tradingsymbol(record.symbol)
            broker_position = broker_by_tradingsymbol.get(tradingsymbol)
            if broker_position is not None:
                self._reconcile_entry_price(position, broker_position)
                if self._protective_stop_needs_rearm(record.protective_order_id):
                    self._rearm_protective_stop(position)
                continue
            if held_tradingsymbols is None:
                # The holdings() lookup itself failed this cycle -- there's no reliable way to
                # tell "genuinely closed" from "settled into a holding" right now, so skip this
                # position (including its re-arm check) rather than risk closing one that's still
                # owned. Retried next cycle.
                continue
            if tradingsymbol in held_tradingsymbols:
                # A CNC buy drops out of positions() once it settles into a holding (commonly the
                # next trading day or two) even though it's still fully owned -- kite.holdings()
                # is what actually reflects that. It doesn't carry a comparable average_price to
                # reconcile against, but the daily re-arm check doesn't depend on `broker_position`
                # at all (it only reads order_history via the stored protective_order_id and a
                # fresh LTP quote) -- and this is exactly the case (a position a day or more past
                # entry) the re-arm check exists for, so it must still run here.
                if self._protective_stop_needs_rearm(record.protective_order_id):
                    self._rearm_protective_stop(position)
                continue
            exit_price, exit_reason = self._broker_exit_fill(record)
            pnl = (exit_price - record.entry_price) * record.quantity
            # Same atomic claim as the intraday path -- SwingAutoTrader's own reconciliation can
            # independently notice this exact same broker-side close at nearly the same moment.
            if self.repository.claim_position_close(record.symbol) is None:
                self.positions.pop(record.symbol, None)
                continue
            self.repository.save_trade(
                TradeRecord(
                    symbol=record.symbol,
                    entry_time=record.entry_time,
                    exit_time=datetime.now(),
                    entry_price=record.entry_price,
                    exit_price=exit_price,
                    quantity=record.quantity,
                    pnl=pnl,
                    side=record.side,
                    position_type="SWING",
                    strategy_name=record.strategy_name,
                    exit_reason=exit_reason,
                )
            )
            self.positions.pop(record.symbol, None)
            self._log_decision(
                record,
                event_type="exit",
                decision="EXIT",
                rationale=exit_reason,
                outputs={"exit_price": exit_price, "pnl": pnl},
            )
            self.repository.link_decision_outcome(self._correlation_id(record), {"exit_price": exit_price, "pnl": pnl, "exit_reason": exit_reason})

    def _broker_held_tradingsymbols(self) -> set[str] | None:
        """Tradingsymbols Zerodha's kite.holdings() still shows as genuinely owned.

        positions() only reflects the current day's activity -- a CNC buy drops out of it once
        it settles into a holding (commonly T+1 or T+2), even though nothing was ever sold. Using
        positions() alone to decide "did this close?" would eventually flag every swing position
        as closed a few days after entry, purely from settlement. Returns None (rather than an
        empty set) when the lookup itself fails, so callers can tell "confirmed not held" apart
        from "couldn't check this cycle" and avoid treating a lookup failure as a closed position.
        """
        if self.broker_client is None or not hasattr(self.broker_client, "holdings"):
            return set()
        try:
            holdings = self.broker_client.holdings()
        except Exception:
            logger.exception("Broker holdings lookup failed during swing reconciliation")
            return None
        held: set[str] = set()
        for entry in holdings:
            try:
                total_quantity = int(entry.get("quantity", 0) or 0) + int(entry.get("t1_quantity", 0) or 0)
            except (TypeError, ValueError):
                continue
            if total_quantity > 0:
                held.add(str(entry.get("tradingsymbol", "")).strip().upper())
        return held

    def _broker_exit_fill(self, record: PositionRecord) -> tuple[float, str]:
        """Find the real fill price for a position discovered closed at the broker, and a
        plain-language reason for the P&L page -- checked in order of confidence:

        1. The tracked protective SL-M itself shows a completed fill -> a genuine stop-loss hit.
        2. A different completed order for the same tradingsymbol today (the confirmed cause of
           a real incident: the position was closed manually at Zerodha, not via the tracked
           SL-M, so its order_history never shows a fill) -> found by scanning today's order
           book for the exit side, excluding the tracked order id.
        3. Neither is found -> fall back to the last recorded stop price, but say so plainly
           rather than silently presenting a stale price as the real exit (that silence is
           exactly what previously made the P&L page show a wrong, unsynced number).
        """
        order_id = record.protective_order_id
        if order_id and self.broker_client is not None and hasattr(self.broker_client, "order_history"):
            try:
                history = self.broker_client.order_history(order_id) or []
            except Exception:
                history = []
            for entry in reversed(history):
                status = str(entry.get("status", "")).upper()
                try:
                    average_price = float(entry.get("average_price", 0) or 0)
                except (TypeError, ValueError):
                    average_price = 0
                if status in {"COMPLETE", "COMPLETED", "FILLED"} and average_price > 0:
                    return average_price, "Closed due to stop-loss hit (SL-M filled at the broker)"
        if self.broker_client is not None and hasattr(self.broker_client, "orders"):
            tradingsymbol = self._tradingsymbol(record.symbol)
            exit_side = "SELL" if record.side == "BUY" else "BUY"
            try:
                todays_orders = self.broker_client.orders() or []
            except Exception:
                todays_orders = []
            candidates = [
                order
                for order in todays_orders
                if str(order.get("tradingsymbol", "")).strip().upper() == tradingsymbol
                and str(order.get("order_id")) != str(order_id)
                and str(order.get("transaction_type", "")).upper() == exit_side
                and str(order.get("status", "")).upper() in {"COMPLETE", "COMPLETED", "FILLED"}
            ]
            if candidates:
                latest = max(candidates, key=lambda order: str(order.get("order_timestamp", "")))
                try:
                    average_price = float(latest.get("average_price", 0) or 0)
                except (TypeError, ValueError):
                    average_price = 0
                if average_price > 0:
                    return average_price, "Closed at broker (a different order than the tracked SL-M -- likely a manual square-off)"
        return record.stop_loss, "Closed at broker; exact fill price unavailable, using the last recorded stop as an estimate"

    def _protective_stop_needs_rearm(self, order_id: str | None) -> bool:
        """True if the protective stop needs a fresh order placed today.

        Deliberately checks today's order book (orders()) rather than order_history(order_id):
        Kite's order_history endpoint reliably answers for an order from earlier today, but for
        one placed on a previous day it can raise "Couldn't find that order_id" instead of
        returning its terminal status -- confirmed live against a real multi-day swing position,
        whose stop had silently gone unprotected for days because that failure was being read as
        "no re-arm needed". Kite's regular order book (and a "regular" SL-M's day-order lifetime)
        are both scoped to the current trading day, so checking membership there is the reliable
        signal regardless of how old the order id is.
        """
        if not order_id:
            return True
        if self.broker_client is None or not hasattr(self.broker_client, "orders"):
            return False
        try:
            todays_orders = self.broker_client.orders()
        except Exception:
            logger.exception("Order book lookup failed while checking whether %s needs re-arming", order_id)
            return False
        for order in todays_orders:
            if str(order.get("order_id")) == str(order_id):
                status = str(order.get("status", "")).strip().upper()
                return status in TERMINAL_INACTIVE_ORDER_STATUSES
        # Not present in today's order book at all -- true both for an order the exchange
        # cancelled today and for one placed on any previous day (the normal case for a
        # multi-day swing holding), since a "regular" SL-M is a day order either way and this
        # position needs a freshly placed one today regardless of why yesterday's is gone.
        return True

    def _rearm_protective_stop(self, position: AgentPosition) -> None:
        record = position.record
        reference_price = record.entry_price
        try:
            key = f"{self._exchange(record.symbol)}:{self._tradingsymbol(record.symbol)}"
            quote = self.market_data.ltp([key]).get(key)
            if quote:
                reference_price = float(quote.get("last_price", 0)) or reference_price
        except Exception:
            logger.exception("LTP lookup failed while re-arming stop for %s", record.symbol)
        exit_side = Side.SELL if record.side == "BUY" else Side.BUY
        request = OrderRequest(record.symbol, exit_side, record.quantity, reference_price, record.stop_loss, "CNC", self._exchange(record.symbol))
        try:
            new_order_id = self.orders.place_protective_stop(request)
        except Exception as error:
            logger.error("Failed to re-arm protective stop for %s: %s", record.symbol, error)
            self.notifier.send(f"critical_unprotected: could not re-arm overnight-expired stop for {record.symbol}: {error}")
            # A silently swallowed failure here means a position sits genuinely unprotected while
            # the dashboard keeps showing a healthy green "RUNNING" banner -- this needs to surface
            # as clearly as any other broker-call failure, not less, since the consequence is worse.
            self._swing_broker_error = f"could not re-arm the protective stop for {record.symbol}: {error}"
            return
        updated = replace(record, protective_order_id=new_order_id)
        self.repository.save_position(updated)
        position.record = updated
        logger.info("Re-armed protective stop for %s at %.2f (order %s)", record.symbol, record.stop_loss, new_order_id)
        self.notifier.send(f"Re-armed protective stop for {record.symbol} at {record.stop_loss:.2f} (previous SL-M had expired)")
        reason = (
            f"Overnight SL-M expired as a day order (previous order {record.protective_order_id or 'unknown'}); "
            f"re-armed a fresh protective stop {new_order_id} at the same trigger price -- the stop level itself did not change."
        )
        # Previously this whole re-arm was invisible outside a Telegram notification: nothing in
        # `activity` or the decision log recorded that a *new* SL-M order now exists at the
        # broker, even though the Live monitor page's own "Broker status" already shows it as
        # the resting order -- exactly the gap that made a freshly re-armed stop look
        # unexplained there.
        self.repository.save_activity(
            ActivityRecord(
                event_kind="stop_rearmed",
                symbol=record.symbol,
                timestamp=datetime.now(),
                mode="LIVE",
                price=record.stop_loss,
                order_id=new_order_id,
                side=exit_side.value,
                quantity=record.quantity,
                entry_price=record.entry_price,
                stop_loss=record.stop_loss,
                reason=reason,
            )
        )
        self._log_decision(
            updated,
            event_type="stop_rearmed",
            decision="REARM_STOP",
            rationale=reason,
            outputs={"new_order_id": new_order_id, "stop_loss": record.stop_loss},
        )

    def _process_swing_position(self, position: AgentPosition, timestamp: datetime) -> None:
        record = position.record
        if position.instrument_token is None or not record.protective_order_id:
            return
        end_of_yesterday = datetime(timestamp.year, timestamp.month, timestamp.day)
        rows = self.market_data.historical(position.instrument_token, end_of_yesterday - timedelta(days=DEFAULT_SWING_LOOKBACK_DAYS), end_of_yesterday, "day")
        frame = self._candles_frame(rows)
        if frame.empty:
            return
        latest_timestamp = frame.iloc[-1]["timestamp"]
        if latest_timestamp <= position.last_trailing_candle:
            return
        position.last_trailing_candle = latest_timestamp
        tick_size = self.orders.tick_size(record.symbol, self._exchange(record.symbol))
        reference_price = float(frame.iloc[-1]["close"])
        atr_multiplier = record.atr_multiplier or 2.0
        if record.strategy_name == "SWING_TREND_BREAKOUT":
            result = compute_trend_breakout_stop(frame, tick_size)
        else:
            result = compute_ema_swing_stop(frame, self.atr_period, atr_multiplier, reference_price, tick_size)
        if result is None:
            return
        candidate, calc_detail = result
        calculation = f"trailing stop moved from ₹{record.stop_loss:.2f} to ₹{candidate:.2f} - Calc: {calc_detail}"
        self._maybe_apply(position, candidate, reference_price, calculation)

    # -- shared apply/persist/notify path --------------------------------------

    def _maybe_apply(self, position: AgentPosition, candidate_stop: float, reference_price: float, calculation: str) -> None:
        if candidate_stop is None or candidate_stop <= 0:
            return
        with position.lock:
            record = position.record
            if record.side == "BUY":
                if candidate_stop <= record.stop_loss or candidate_stop >= reference_price:
                    return
            else:
                if candidate_stop >= record.stop_loss or candidate_stop <= reference_price:
                    return
            # A genuine improvement that's too small to matter (a paisa-level ATR wobble) still
            # burns one of Zerodha's 25 modifications on this order for no real protection
            # benefit -- skip sending it to the broker at all unless it clears the configured
            # minimum step (Risk & settings). Nothing is persisted or logged for a skip: this is
            # deliberately silent, not a failure, so it doesn't add decision-log noise either.
            min_step = reference_price * (self.min_stop_improvement_pct / 100.0)
            if abs(candidate_stop - record.stop_loss) < min_step:
                return
            self._apply_candidate_stop(position, candidate_stop, reference_price, calculation)

    def _apply_candidate_stop(self, position: AgentPosition, candidate_stop: float, reference_price: float, calculation: str) -> None:
        record = position.record
        exit_side = Side.SELL if record.side == "BUY" else Side.BUY
        product = "CNC" if record.position_type == "SWING" else "MIS"
        request = OrderRequest(record.symbol, exit_side, record.quantity, reference_price, candidate_stop, product, self._exchange(record.symbol))
        error_message = self._modify_with_retry(record.protective_order_id or "", request)
        if error_message and self._is_modification_limit_error(error_message):
            error_message = self._replace_protective_stop(position, request)
        if error_message:
            # Previously swallowed entirely (only logged to a file + a Telegram notifier
            # message no one necessarily has configured) -- so the dashboard's own "Trailing-stop
            # agent" banner kept reading as a healthy green RUNNING state while a position's stop
            # was silently stuck. Routing it into the same _intraday_broker_error/_swing_broker_error
            # fields the heartbeat already reports surfaces it there instead, with no new UI needed.
            friendly = f"Could not update the broker-side stop for {record.symbol}: {error_message}"
            if record.position_type == "SWING":
                self._swing_broker_error = friendly
            else:
                self._intraday_broker_error = friendly
            self._log_decision(
                record,
                event_type="stop_trailed",
                decision="TRAIL_STOP_FAILED",
                rationale=f"{calculation} -- but the broker rejected the update: {error_message}",
                inputs={"reference_price": reference_price, "attempted_stop": candidate_stop},
            )
            return
        record = position.record  # _replace_protective_stop may have swapped in a fresh order id
        self.repository.update_stop_price(record.symbol, candidate_stop)
        previous_stop = record.stop_loss
        position.record = replace(record, stop_loss=candidate_stop)
        position.trailing_stop.stop = candidate_stop
        self.repository.save_activity(
            ActivityRecord(
                event_kind="stop_trailed",
                symbol=record.symbol,
                timestamp=datetime.now(),
                mode="LIVE",
                price=candidate_stop,
                order_id=record.protective_order_id,
                side=exit_side.value,
                quantity=record.quantity,
                entry_price=record.entry_price,
                stop_loss=candidate_stop,
                reason=calculation,
                previous_stop=previous_stop,
            )
        )
        self.notifier.send(f"Trailing stop moved: {record.symbol} -> {candidate_stop:.2f}")
        self._log_decision(
            record,
            event_type="stop_trailed",
            decision="TRAIL_STOP",
            rationale=calculation,
            inputs={"reference_price": reference_price, "previous_stop": previous_stop},
            outputs={"new_stop": candidate_stop},
        )

    def _modify_with_retry(self, order_id: str, request: OrderRequest) -> str:
        """Returns an empty string on success, otherwise a human-readable failure reason."""
        attempts = self.modify_retry_attempts
        if not order_id:
            logger.error("No protective order id on file for %s; cannot trail stop", request.symbol)
            return "no protective stop order is on file for this position"
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self.orders.modify_protective_stop(order_id, request)
                return ""
            except Exception as error:
                last_error = error
                logger.warning("modify_protective_stop failed for %s (attempt %s/%s): %s", request.symbol, attempt + 1, attempts, error)
                if self._is_modification_limit_error(error):
                    # Every further attempt on this same order_id fails identically -- the cap
                    # is per-order, not transient -- so retrying here only burns the backoff
                    # window before _apply_candidate_stop falls back to replacing the order.
                    break
                if attempt < attempts - 1:
                    sleep(min(self.modify_retry_backoff_seconds * (2**attempt), 5))
        if not self._is_modification_limit_error(last_error):
            logger.error("modify_protective_stop permanently failed for %s: %s", request.symbol, last_error)
            self.notifier.send(f"critical_unprotected: could not trail stop for {request.symbol}: {last_error}")
        return str(last_error) or "unknown error"

    @staticmethod
    def _is_modification_limit_error(error: Exception | str | None) -> bool:
        return MODIFICATION_LIMIT_ERROR_TEXT in str(error).lower()

    def _replace_protective_stop(self, position: AgentPosition, request: OrderRequest) -> str:
        """Cancel the current protective SL-M and place a fresh one in its place.

        Called only once Zerodha's per-order modification cap (25) has been hit -- there's no
        API to reset that count on the existing order, but a brand-new order id starts back at
        zero. Cancels first (rather than placing the replacement first) so the stale-priced
        order can never sit resting alongside the new one and fill at the wrong level.
        """
        record = position.record
        old_order_id = record.protective_order_id
        try:
            if old_order_id:
                self.orders.cancel(old_order_id)
        except Exception:
            logger.exception("Failed to cancel modification-capped protective stop %s for %s", old_order_id, record.symbol)
        try:
            new_order_id = self.orders.place_protective_stop(request)
        except Exception as error:
            logger.error("Failed to place a replacement protective stop for %s after hitting the modification cap: %s", record.symbol, error)
            self.notifier.send(f"critical_unprotected: could not replace the modification-capped stop for {record.symbol}: {error}")
            return str(error) or "unknown error"
        updated = replace(record, protective_order_id=new_order_id)
        self.repository.save_position(updated)
        position.record = updated
        logger.info("Replaced modification-capped protective stop for %s with fresh order %s", record.symbol, new_order_id)
        self.notifier.send(f"{record.symbol}: replaced its protective stop with a fresh order after hitting Zerodha's 25-modification cap")
        self._log_decision(
            updated,
            event_type="protective_stop_replaced",
            decision="REPLACE_ORDER",
            rationale=f"Order {old_order_id} hit Zerodha's 25-modification cap; cancelled it and placed fresh order {new_order_id} at the same trigger price to keep trailing.",
            inputs={"old_order_id": old_order_id, "trigger_price": request.stop_loss},
            outputs={"new_order_id": new_order_id},
        )
        return ""
