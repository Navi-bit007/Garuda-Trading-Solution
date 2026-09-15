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
from app.database.models import ActivityRecord, AgentHeartbeat, PositionRecord, TradeRecord
from app.database.repository import Repository
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
        self._broker_error = ""
        self.shutdown_time = getattr(settings, "agent_shutdown_time", time_of_day(15, 40))
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
        self._broker_error = ""
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
                self.repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", timestamp, last_error or self._broker_error, timestamp))
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
                existing.record = record
                continue
            instrument_token = record.instrument_token
            if instrument_token is None:
                instrument_token = self._resolve_instrument_token(symbol)
                if instrument_token is not None:
                    record = replace(record, instrument_token=instrument_token)
                    self.repository.save_position(record)
            atr_multiplier = record.atr_multiplier or float(getattr(self.settings, "trailing_atr_multiplier", 1.5))
            trailing_stop = TrailingStop(record.entry_price, record.stop_loss, atr_multiplier, record.side)
            self.positions[symbol] = AgentPosition(
                record=record,
                instrument_token=instrument_token,
                trailing_stop=trailing_stop,
                last_trailing_candle=record.entry_time,
            )

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
        return symbol.split(":", 1)[1].strip().upper() if ":" in symbol else symbol.strip().upper()

    @staticmethod
    def _exchange(symbol: str) -> str:
        return symbol.split(":", 1)[0].strip().upper() if ":" in symbol else "NSE"

    @staticmethod
    def _candles_frame(rows: list[dict]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        if "date" in frame.columns and "timestamp" not in frame.columns:
            frame = frame.rename(columns={"date": "timestamp"})
        return validate_ohlcv(frame)

    # -- intraday: tick-price-driven ATR ratchet -------------------------------

    def _process_intraday(self, timestamp: datetime) -> None:
        positions = [position for position in self.positions.values() if position.record.position_type == "INTRADAY"]
        if not positions:
            return
        try:
            self._reconcile_intraday_positions(positions, timestamp)
        except Exception as error:
            logger.exception("Intraday broker reconciliation failed")
            self._broker_error = f"intraday reconciliation failed: {error}"
        positions = [position for position in self.positions.values() if position.record.position_type == "INTRADAY"]
        if not positions:
            return
        keys = [f"{self._exchange(p.record.symbol)}:{self._tradingsymbol(p.record.symbol)}" for p in positions]
        try:
            quotes = self.market_data.ltp(keys)
        except Exception as error:
            logger.exception("LTP fetch failed for intraday positions")
            self._broker_error = f"LTP fetch failed: {error}"
            return
        for position, key in zip(positions, keys):
            quote = quotes.get(key)
            if not quote:
                continue
            try:
                price = float(quote["last_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            atr_value = self._resolve_atr(position, timestamp)
            if atr_value <= 0:
                continue
            candidate = position.trailing_stop.update(price, atr_value)
            if position.record.target_1_hit:
                entry_price = position.record.entry_price
                candidate = max(candidate, entry_price) if position.record.side == "BUY" else min(candidate, entry_price)
                position.trailing_stop.stop = candidate
            self._maybe_apply(position, candidate, price)

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
            self._broker_error = f"broker position lookup failed: {error}"
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
        exit_price = self._broker_exit_fill_price(record)
        multiplier = 1 if record.side == "BUY" else -1
        pnl = multiplier * (exit_price - record.entry_price) * record.quantity
        reason = "broker-side position closed; detected by the standalone trailing-stop agent"
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
        self.repository.delete_position(record.symbol)
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
        try:
            self._reconcile_swing_positions(timestamp)
        except Exception as error:
            logger.exception("Swing broker reconciliation failed")
            self._broker_error = f"swing reconciliation failed: {error}"
        for position in [p for p in self.positions.values() if p.record.position_type == "SWING"]:
            try:
                self._process_swing_position(position, timestamp)
            except Exception as error:
                logger.exception("Swing trailing update failed for %s", position.record.symbol)
                self._broker_error = f"swing trailing update failed for {position.record.symbol}: {error}"

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
            self._broker_error = f"broker position lookup failed: {error}"
            return
        broker_by_tradingsymbol = {str(entry.get("tradingsymbol")): entry for entry in broker_positions if int(entry.get("quantity", 0) or 0) != 0}
        for position in positions:
            record = position.record
            broker_position = broker_by_tradingsymbol.get(self._tradingsymbol(record.symbol))
            if broker_position is None:
                exit_price = self._broker_exit_fill_price(record)
                self.repository.save_trade(
                    TradeRecord(
                        symbol=record.symbol,
                        entry_time=record.entry_time,
                        exit_time=datetime.now(),
                        entry_price=record.entry_price,
                        exit_price=exit_price,
                        quantity=record.quantity,
                        pnl=(exit_price - record.entry_price) * record.quantity,
                        side=record.side,
                        position_type="SWING",
                        strategy_name=record.strategy_name,
                        exit_reason="broker-side position closed (protective stop or manual exit)",
                    )
                )
                self.repository.delete_position(record.symbol)
                self.positions.pop(record.symbol, None)
                continue
            self._reconcile_entry_price(position, broker_position)
            if self._protective_stop_needs_rearm(record.protective_order_id):
                self._rearm_protective_stop(position)

    def _broker_exit_fill_price(self, record: PositionRecord) -> float:
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
                    return average_price
        return record.stop_loss

    def _protective_stop_needs_rearm(self, order_id: str | None) -> bool:
        if not order_id:
            return True
        if self.broker_client is None or not hasattr(self.broker_client, "order_history"):
            return False
        try:
            history = self.broker_client.order_history(order_id)
        except Exception:
            logger.exception("order_history lookup failed for %s", order_id)
            return False
        if not history:
            return True
        status = str(history[-1].get("status", "")).strip().upper()
        return status in TERMINAL_INACTIVE_ORDER_STATUSES

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
            return
        updated = replace(record, protective_order_id=new_order_id)
        self.repository.save_position(updated)
        position.record = updated
        logger.info("Re-armed protective stop for %s at %.2f (order %s)", record.symbol, record.stop_loss, new_order_id)
        self.notifier.send(f"Re-armed protective stop for {record.symbol} at {record.stop_loss:.2f} (previous SL-M had expired)")

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
            candidate = compute_trend_breakout_stop(frame, tick_size)
        else:
            candidate = compute_ema_swing_stop(frame, self.atr_period, atr_multiplier, reference_price, tick_size)
        if candidate is None:
            return
        self._maybe_apply(position, candidate, reference_price)

    # -- shared apply/persist/notify path --------------------------------------

    def _maybe_apply(self, position: AgentPosition, candidate_stop: float, reference_price: float) -> None:
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
            self._apply_candidate_stop(position, candidate_stop, reference_price)

    def _apply_candidate_stop(self, position: AgentPosition, candidate_stop: float, reference_price: float) -> None:
        record = position.record
        exit_side = Side.SELL if record.side == "BUY" else Side.BUY
        product = "CNC" if record.position_type == "SWING" else "MIS"
        request = OrderRequest(record.symbol, exit_side, record.quantity, reference_price, candidate_stop, product, self._exchange(record.symbol))
        if not self._modify_with_retry(record.protective_order_id or "", request):
            return
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
                reason=f"trailing stop moved from {previous_stop:.2f} to {candidate_stop:.2f}",
            )
        )
        self.notifier.send(f"Trailing stop moved: {record.symbol} -> {candidate_stop:.2f}")

    def _modify_with_retry(self, order_id: str, request: OrderRequest) -> bool:
        attempts = self.modify_retry_attempts
        if not order_id:
            logger.error("No protective order id on file for %s; cannot trail stop", request.symbol)
            return False
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                self.orders.modify_protective_stop(order_id, request)
                return True
            except Exception as error:
                last_error = error
                logger.warning("modify_protective_stop failed for %s (attempt %s/%s): %s", request.symbol, attempt + 1, attempts, error)
                if attempt < attempts - 1:
                    sleep(min(self.modify_retry_backoff_seconds * (2**attempt), 5))
        logger.error("modify_protective_stop permanently failed for %s: %s", request.symbol, last_error)
        self.notifier.send(f"critical_unprotected: could not trail stop for {request.symbol}: {last_error}")
        return False
