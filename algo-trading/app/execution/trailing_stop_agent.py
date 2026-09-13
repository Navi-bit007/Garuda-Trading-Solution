from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from threading import Event, Lock, Thread, current_thread
from time import sleep
from typing import Any

import pandas as pd

from app.broker.market_data import MarketData
from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side
from app.database.models import AgentHeartbeat, PositionRecord, TradeRecord
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
        last_error = ""
        try:
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
            try:
                self.repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", timestamp, last_error, timestamp))
            except Exception:
                logger.exception("Failed to record trailing-stop agent heartbeat")

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
        keys = [f"{self._exchange(p.record.symbol)}:{self._tradingsymbol(p.record.symbol)}" for p in positions]
        try:
            quotes = self.market_data.ltp(keys)
        except Exception:
            logger.exception("LTP fetch failed for intraday positions")
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
        except Exception:
            logger.exception("Swing broker reconciliation failed")
        for position in [p for p in self.positions.values() if p.record.position_type == "SWING"]:
            try:
                self._process_swing_position(position, timestamp)
            except Exception:
                logger.exception("Swing trailing update failed for %s", position.record.symbol)

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
        except Exception:
            logger.exception("Broker position lookup failed during swing reconciliation")
            return
        open_tradingsymbols = {str(entry.get("tradingsymbol")) for entry in broker_positions if int(entry.get("quantity", 0) or 0) != 0}
        for position in positions:
            record = position.record
            if self._tradingsymbol(record.symbol) not in open_tradingsymbols:
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
        position.record = replace(record, stop_loss=candidate_stop)
        position.trailing_stop.stop = candidate_stop
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
