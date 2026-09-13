from __future__ import annotations

from collections.abc import Mapping
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import math
from typing import Any, Iterable

import pandas as pd

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side, SignalAction, TradingMode
from app.database.models import ActivityRecord, PositionRecord, TradeRecord
from app.execution.order_manager import OrderManager
from app.execution.position_manager import Position, PositionManager
from app.execution.trailing_stop import TrailingStop
from app.market.candles import TickCandleBuilder, validate_ohlcv
from app.market.indicators import atr, ema
from app.market.scanner import Nifty500Scanner
from app.monitoring.notifications import Notifier
from app.risk.daily_limits import DailyLimits
from app.risk.exposure import Exposure
from app.risk.risk_manager import RiskManager
from app.strategy.base import NoSignal
from app.strategy.base import Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class PipelineEvent:
    kind: str
    symbol: str
    timestamp: datetime
    price: float | None = None
    order_id: str | None = None
    reason: str = ""
    side: str | None = None
    quantity: int | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    pnl: float | None = None


@dataclass
class ManagedPosition:
    position: Position
    trailing_stop: TrailingStop
    protective_order_id: str | None = None
    exit_on_ema9_close: bool = False
    move_stop_to_breakeven: bool = False
    target_1_hit: bool = False


@dataclass(frozen=True)
class ClosedPosition:
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    exit_price: float
    pnl: float
    closed_at: datetime
    order_id: str | None
    reason: str


class TradingPipeline:
    """Deterministic tick-to-order pipeline with PAPER as the default boundary."""

    def __init__(self, settings: Any, token_to_symbol: dict[int, str], strategy: Strategy, broker_client: Any = None, activity_repository: Any = None):
        if settings.trading_mode == TradingMode.LIVE and broker_client is None:
            raise ValueError("LIVE pipeline requires an authenticated broker client")
        self.settings = settings
        self.activity_repository = activity_repository
        self.notifier = Notifier(
            bool(getattr(settings, "enable_telegram", False)),
            getattr(settings, "telegram_bot_token", "").get_secret_value() if hasattr(getattr(settings, "telegram_bot_token", ""), "get_secret_value") else str(getattr(settings, "telegram_bot_token", "")),
            str(getattr(settings, "telegram_chat_id", "")),
        )
        self.strategy = strategy
        self.scanner = Nifty500Scanner(token_to_symbol)
        self.orders = OrderAPI(settings.trading_mode, broker_client)
        self.order_manager = OrderManager(self.orders, settings.trading_mode)
        self.positions = PositionManager()
        self.managed_positions: dict[str, ManagedPosition] = {}
        self.recent_closed_positions: list[ClosedPosition] = []
        self.candle_builders = {symbol: TickCandleBuilder() for symbol in token_to_symbol.values()}
        self.history: dict[str, list[dict]] = defaultdict(list)
        self.last_prices: dict[str, float] = {}
        self.last_atr: dict[str, float] = {}
        self.limits = DailyLimits(settings.initial_capital, settings.max_trades_per_day)
        self.risk = RiskManager(
            settings.initial_capital,
            settings.max_open_positions,
            self.limits,
            Exposure(settings.initial_capital, settings.max_capital_deployment),
        )
        self.halted = False
        self._restore_daily_limits()
        self._restore_positions()

    def halt_entries(self) -> None:
        self.halted = True

    def resume_entries(self) -> None:
        self.halted = False

    def on_ticks(self, ticks: Iterable[dict]) -> list[PipelineEvent]:
        tick_list = [normalised for tick in ticks if (normalised := self._normalise_tick(tick)) is not None]
        events: list[PipelineEvent] = []
        completed_symbols: set[str] = set()
        closed_symbols: set[str] = set()
        latest_timestamp: datetime | None = None
        for tick in tick_list:
            symbol = self.scanner.token_to_symbol.get(int(tick.get("instrument_token", 0)))
            if symbol is None or "last_price" not in tick:
                continue
            price = float(tick["last_price"])
            timestamp = self._tick_timestamp(tick)
            latest_timestamp = max(latest_timestamp, timestamp) if latest_timestamp else timestamp
            self.last_prices[symbol] = price
            trailing_events = self._update_trailing_stop(symbol, price, timestamp)
            events.extend(trailing_events)
            if any(event.kind in ("exit_submitted", "critical_unprotected") for event in trailing_events):
                closed_symbols.add(symbol)
            completed = self.candle_builders[symbol].update(tick)
            if completed is not None:
                self.history[symbol].append(completed)
                self.history[symbol] = self.history[symbol][-500:]
                completed_symbols.add(symbol)

        if latest_timestamp and latest_timestamp.time() < self.settings.force_exit:
            candidates = set(self.scanner.scan(tick_list))
            for symbol in sorted(completed_symbols & candidates - closed_symbols):
                events.append(self._evaluate_completed_candles(symbol))
        if latest_timestamp and latest_timestamp.time() >= self.settings.force_exit:
            events.extend(self.close_at_force_exit(latest_timestamp))
        return [event for event in events if event is not None]

    def monitor_ticks(self, ticks: Iterable[dict]) -> list[PipelineEvent]:
        """Update managed positions without evaluating new strategy entries."""
        tick_list = [normalised for tick in ticks if (normalised := self._normalise_tick(tick)) is not None]
        events: list[PipelineEvent] = []
        latest_timestamp: datetime | None = None
        for tick in tick_list:
            symbol = self.scanner.token_to_symbol.get(int(tick.get("instrument_token", 0)))
            if symbol is None or "last_price" not in tick:
                continue
            price = float(tick["last_price"])
            timestamp = self._tick_timestamp(tick)
            latest_timestamp = max(latest_timestamp, timestamp) if latest_timestamp else timestamp
            self.last_prices[symbol] = price
            events.extend(self._update_trailing_stop(symbol, price, timestamp))
        if latest_timestamp and latest_timestamp.time() >= self.settings.force_exit:
            events.extend(self.close_at_force_exit(latest_timestamp))
        return [event for event in events if event is not None]

    def monitor_candle_closes(self, candles_by_symbol: dict[str, pd.DataFrame]) -> list[PipelineEvent]:
        """Close automatic long positions when a completed candle closes below EMA9."""
        events: list[PipelineEvent] = []
        for symbol, managed in list(self.managed_positions.items()):
            if not managed.exit_on_ema9_close or managed.position.side != Side.BUY:
                continue
            candles = candles_by_symbol.get(symbol)
            if candles is None or candles.empty:
                continue
            frame = validate_ohlcv(candles)
            if len(frame) < 9:
                continue
            latest = frame.iloc[-1]
            candle_close = float(latest["close"])
            ema9_value = float(ema(frame["close"], 9).iloc[-1])
            candle_timestamp = latest["timestamp"].to_pydatetime()
            if candle_timestamp <= managed.position.entry_time:
                continue
            if candle_close < ema9_value:
                events.append(self._close_position(symbol, candle_close, candle_timestamp, "candle close below EMA9"))
        return [event for event in events if event is not None]

    def submit_manual_entry(
        self,
        symbol: str,
        price: float,
        stop_loss: float,
        timestamp: datetime | None = None,
        quantity: int | None = None,
        action: SignalAction = SignalAction.BUY,
        target_1: float | None = None,
        target_2: float | None = None,
    ) -> PipelineEvent:
        """Submit a user-selected BUY or SELL through the same risk and order gates."""
        signal_timestamp = timestamp or datetime.now()
        if symbol not in self.scanner.token_to_symbol.values():
            return self._event("entry_rejected", symbol, signal_timestamp, price, reason="symbol is outside the active universe", side=action.value)
        if action not in (SignalAction.BUY, SignalAction.SELL):
            return self._event("entry_rejected", symbol, signal_timestamp, price, reason="manual action must be BUY or SELL")
        stop_is_invalid = stop_loss >= price if action == SignalAction.BUY else stop_loss <= price
        if price <= 0 or stop_loss <= 0 or stop_is_invalid:
            stop_direction = "below" if action == SignalAction.BUY else "above"
            return self._event("entry_rejected", symbol, signal_timestamp, price, reason=f"stop loss must be positive and {stop_direction} entry price", side=action.value, entry_price=price, stop_loss=stop_loss)
        if quantity is not None and quantity <= 0:
            return self._event("entry_rejected", symbol, signal_timestamp, price, reason="quantity must be positive", side=action.value, entry_price=price, stop_loss=stop_loss)
        return self._submit_entry(
            Signal(symbol, action, signal_timestamp, price, stop_loss, "manual entry", target_1=target_1, target_2=target_2),
            quantity,
            enforce_entry_window=False,
        )

    def submit_strategy_entry(self, signal: Signal, quantity: int | None = None) -> PipelineEvent:
        """Submit a qualified strategy signal through the same risk and order gates."""
        if not signal.is_entry:
            return self._event("entry_rejected", signal.symbol, signal.timestamp, signal.price, reason="signal is not an entry", side=signal.action.value)
        return self._submit_entry(signal, quantity, enforce_entry_window=True)

    def close_at_force_exit(self, timestamp: datetime) -> list[PipelineEvent]:
        events = []
        for symbol in list(self.managed_positions):
            price = self.last_prices.get(symbol)
            if price is not None:
                events.append(self._close_position(symbol, price, timestamp, "configured force exit"))
        return events

    def close_all_positions(self, timestamp: datetime | None = None) -> list[PipelineEvent]:
        close_timestamp = timestamp or datetime.now()
        events = []
        for symbol in list(self.managed_positions):
            price = self.last_prices.get(symbol, self.managed_positions[symbol].position.entry_price)
            events.append(self._close_position(symbol, price, close_timestamp, "emergency close all"))
        return events

    def sync_broker_positions(self) -> list[PipelineEvent]:
        if self.settings.trading_mode != TradingMode.LIVE or self.orders.client is None:
            return []
        broker_positions = self.orders.client.positions().get("net", [])
        broker_quantities = {
            str(position.get("tradingsymbol", "")).strip().upper(): int(position.get("quantity", 0) or 0)
            for position in broker_positions
        }
        events: list[PipelineEvent] = []
        for symbol, managed in list(self.managed_positions.items()):
            tradingsymbol = symbol.split(":", 1)[-1].strip().upper()
            if broker_quantities.get(tradingsymbol, 0) != 0:
                continue
            exit_price, exit_order_id, fill_reason = self._broker_exit_fill(managed)
            reason = f"broker-side position closed; {fill_reason}"
            pnl = managed.position.unrealized_pnl(exit_price)
            self.limits.record_trade(pnl)
            self.positions.remove(symbol)
            del self.managed_positions[symbol]
            if self.activity_repository is not None and hasattr(self.activity_repository, "delete_position"):
                self.activity_repository.delete_position(symbol)
            closed = ClosedPosition(
                symbol,
                managed.position.side,
                managed.position.quantity,
                managed.position.entry_price,
                exit_price,
                pnl,
                datetime.now(),
                exit_order_id,
                reason,
            )
            self.recent_closed_positions = [closed, *self.recent_closed_positions[:19]]
            self._record_trade_history(managed, closed, reason)
            events.append(
                self._event(
                    "broker_exit_detected",
                    symbol,
                    closed.closed_at,
                    exit_price,
                    exit_order_id,
                    reason,
                    Side.SELL.value if managed.position.side == Side.BUY else Side.BUY.value,
                    managed.position.quantity,
                    managed.position.entry_price,
                    pnl=pnl,
                )
            )
        return events

    def submit_manual_exit(self, symbol: str, price: float, timestamp: datetime | None = None) -> PipelineEvent:
        """Close one open position through the configured broker boundary."""
        signal_timestamp = timestamp or datetime.now()
        if price <= 0:
            return self._event("exit_rejected", symbol, signal_timestamp, price, reason="exit price must be positive")
        if symbol not in self.managed_positions:
            return self._event("exit_rejected", symbol, signal_timestamp, price, reason="no open position for symbol")
        return self._close_position(symbol, price, signal_timestamp, "manual exit")

    def extend_universe(self, token_to_symbol: dict[int, str]) -> None:
        """Add symbols without dropping positions already managed by the dashboard."""
        self.scanner.token_to_symbol.update(token_to_symbol)
        for symbol in token_to_symbol.values():
            self.candle_builders.setdefault(symbol, TickCandleBuilder())

    def _evaluate_completed_candles(self, symbol: str) -> PipelineEvent:
        frame = validate_ohlcv(pd.DataFrame(self.history[symbol]))
        try:
            signal = self.strategy.generate_signal(symbol, frame)
        except NoSignal as error:
            return self._event("signal_skipped", symbol, frame["timestamp"].iloc[-1].to_pydatetime(), reason=str(error) or "strategy produced no signal")
        except ValueError as error:
            return self._event("signal_skipped", symbol, frame["timestamp"].iloc[-1].to_pydatetime(), reason=str(error))
        if signal.action != SignalAction.BUY:
            return self._event("signal_held", symbol, signal.timestamp, signal.price, reason=signal.reason, side=signal.action.value)
        return self._submit_entry(signal, enforce_entry_window=True)

    def _submit_entry(self, signal: Signal, requested_quantity: int | None = None, enforce_entry_window: bool = False) -> PipelineEvent:
        symbol = signal.symbol
        if self.halted:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="new entries are halted", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss)
        if symbol in self.managed_positions:
            return self._event("entry_skipped", symbol, signal.timestamp, signal.price, reason="position already open", side=signal.action.value)
        if self.activity_repository is not None and hasattr(self.activity_repository, "has_submitted_signal") and self.activity_repository.has_submitted_signal(symbol, signal.action.value, signal.timestamp):
            return self._event("entry_skipped", symbol, signal.timestamp, signal.price, reason="signal already submitted for this candle", side=signal.action.value)
        if enforce_entry_window and (signal.timestamp.time() < self.settings.entry_start or signal.timestamp.time() > self.settings.entry_end):
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="outside configured entry window", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss)
        if signal.price <= 0:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="entry price must be positive", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss)
        if signal.stop_loss is None:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="BUY signal has no stop loss", side=signal.action.value, entry_price=signal.price)
        if signal.stop_loss <= 0:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="stop loss must be positive", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss)
        stop_is_invalid = signal.stop_loss >= signal.price if signal.action == SignalAction.BUY else signal.stop_loss <= signal.price
        if stop_is_invalid:
            stop_direction = "below" if signal.action == SignalAction.BUY else "above"
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason=f"{signal.action.value} stop loss must be {stop_direction} entry price", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss)
        if requested_quantity is not None and requested_quantity <= 0:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="quantity must be positive", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss, quantity=requested_quantity)
        maximum_quantity = self.risk.quantity(signal.price)
        quantity = maximum_quantity if requested_quantity is None else requested_quantity
        if quantity > maximum_quantity:
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason=f"quantity exceeds capital deployment limit of {maximum_quantity}", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss, quantity=quantity)
        current_exposure = sum(position.position.entry_price * position.position.quantity for position in self.managed_positions.values())
        if not self.risk.approve_entry(len(self.managed_positions), current_exposure, signal.price, quantity):
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason="entry limits rejected entry", side=signal.action.value, entry_price=signal.price, stop_loss=signal.stop_loss, quantity=quantity)
        try:
            order_id = self.order_manager.submit_signal(signal, quantity)
            protective_order_id = self.order_manager.submit_protective_stop(signal, quantity)
        except Exception as error:
            if "order_id" in locals():
                try:
                    exit_side = Side.SELL if signal.action == SignalAction.BUY else Side.BUY
                    self.orders.place(OrderRequest(symbol, exit_side, quantity, signal.price))
                except Exception:
                    pass
            return self._event("entry_rejected", symbol, signal.timestamp, signal.price, reason=f"broker order rejected: {error}", side=signal.action.value, quantity=quantity, entry_price=signal.price, stop_loss=signal.stop_loss)
        position_side = Side.BUY if signal.action == SignalAction.BUY else Side.SELL
        position = Position(symbol, position_side, quantity, signal.price, signal.stop_loss, signal.timestamp, signal.target_1, signal.target_2)
        self.positions.add(position)
        self.managed_positions[symbol] = ManagedPosition(
            position,
            TrailingStop(signal.price, signal.stop_loss, self.settings.trailing_atr_multiplier, position_side.value),
            protective_order_id,
            getattr(self.strategy, "name", "") == "PRE_SPIKE_MOMENTUM",
            bool(signal.metadata.get("move_stop_to_breakeven_after_target_1", False)),
            False,
        )
        if self.activity_repository is not None and hasattr(self.activity_repository, "save_position"):
            self.activity_repository.save_position(
                PositionRecord(symbol, position_side.value, quantity, signal.price, signal.stop_loss, signal.timestamp, signal.target_1, signal.target_2, protective_order_id, False, atr_multiplier=self.settings.trailing_atr_multiplier, trading_mode=self.settings.trading_mode.value)
            )
        strategy_stop_multiplier = float(getattr(self.strategy, "stop_atr", self.settings.trailing_atr_multiplier))
        if strategy_stop_multiplier > 0:
            self.last_atr[symbol] = abs(signal.price - signal.stop_loss) / strategy_stop_multiplier
        return self._event("entry_submitted", symbol, signal.timestamp, signal.price, order_id, signal.reason, signal.action.value, quantity, signal.price, signal.stop_loss)

    def _update_trailing_stop(self, symbol: str, price: float, timestamp: datetime) -> list[PipelineEvent]:
        managed = self.managed_positions.get(symbol)
        if managed is None:
            return []
        target_2_reached = (
            managed.move_stop_to_breakeven
            and managed.position.target_2 is not None
            and (price >= managed.position.target_2 if managed.position.side == Side.BUY else price <= managed.position.target_2)
        )
        if target_2_reached:
            return [self._close_position(symbol, price, timestamp, "target 2 reached")]
        target_1_reached = (
            managed.position.target_1 is not None
            and (price >= managed.position.target_1 if managed.position.side == Side.BUY else price <= managed.position.target_1)
        )
        if managed.move_stop_to_breakeven:
            if target_1_reached and not managed.target_1_hit:
                return self._move_stop_to_breakeven(managed, timestamp, price)
        else:
            if target_1_reached:
                return [self._close_position(symbol, price, timestamp, "target 1 reached")]
        if self.settings.trading_mode == TradingMode.LIVE:
            # The resting broker-side SL-M order (kept current by the standalone
            # scripts/run_trailing_stop_agent.py process) is the sole trailing-stop trigger in
            # LIVE mode; sync_broker_positions() detects the eventual fill. PAPER/backtest has
            # no real broker order to hand off to, so the pipeline keeps simulating it below.
            return []
        if not managed.move_stop_to_breakeven:
            atr_value = self._atr_value(symbol)
            if atr_value > 0:
                managed.trailing_stop.update(price, atr_value)
        stop_triggered = price <= managed.trailing_stop.stop if managed.position.side == Side.BUY else price >= managed.trailing_stop.stop
        if stop_triggered:
            return [self._close_position(symbol, price, timestamp, "trailing stop")]
        return []

    def _move_stop_to_breakeven(self, managed: ManagedPosition, timestamp: datetime, market_price: float) -> list[PipelineEvent]:
        position = managed.position
        exit_side = Side.SELL if position.side == Side.BUY else Side.BUY
        if self.settings.trading_mode != TradingMode.LIVE:
            # PAPER/backtest has no real resting broker order to hand off to the trailing-stop
            # agent, so the pipeline keeps moving its own simulated protective stop.
            try:
                self.orders.modify_protective_stop(
                    managed.protective_order_id or "",
                    OrderRequest(position.symbol, exit_side, position.quantity, market_price, position.entry_price),
                )
            except Exception as error:
                return [self._event("breakeven_rejected", position.symbol, timestamp, position.entry_price, reason=f"protective stop could not move to breakeven: {error}", side=exit_side.value, quantity=position.quantity, entry_price=position.entry_price, stop_loss=position.stop_loss)]
        position.stop_loss = position.entry_price
        managed.trailing_stop.stop = position.entry_price
        managed.target_1_hit = True
        if self.activity_repository is not None and hasattr(self.activity_repository, "save_position"):
            self.activity_repository.save_position(
                PositionRecord(
                    position.symbol,
                    position.side.value,
                    position.quantity,
                    position.entry_price,
                    position.stop_loss,
                    position.entry_time,
                    position.target_1,
                    position.target_2,
                    managed.protective_order_id,
                    True,
                    atr_multiplier=self.settings.trailing_atr_multiplier,
                    trading_mode=self.settings.trading_mode.value,
                )
            )
        return [self._event("breakeven_activated", position.symbol, timestamp, position.entry_price, reason="target 1 reached; protective stop moved to breakeven", side=exit_side.value, quantity=position.quantity, entry_price=position.entry_price, stop_loss=position.stop_loss)]

    def _close_position(self, symbol: str, price: float, timestamp: datetime, reason: str) -> PipelineEvent:
        managed = self.managed_positions[symbol]
        exit_side = Side.SELL if managed.position.side == Side.BUY else Side.BUY
        try:
            order_id = self.orders.place(OrderRequest(symbol, exit_side, managed.position.quantity, price))
        except Exception as error:
            return self._event("exit_rejected", symbol, timestamp, price, reason=f"broker order rejected: {error}", side=exit_side.value, quantity=managed.position.quantity, entry_price=managed.position.entry_price)
        cancellation_error = None
        try:
            self.orders.cancel(managed.protective_order_id or "")
        except Exception as error:
            cancellation_error = error
        pnl = managed.position.unrealized_pnl(price)
        self.limits.record_trade(pnl)
        self.positions.remove(symbol)
        del self.managed_positions[symbol]
        if self.activity_repository is not None and hasattr(self.activity_repository, "delete_position"):
            self.activity_repository.delete_position(symbol)
        if cancellation_error is not None:
            reason = f"{reason}; protective stop cancellation failed: {cancellation_error}"
        closed = ClosedPosition(symbol, managed.position.side, managed.position.quantity, managed.position.entry_price, price, pnl, timestamp, order_id, reason)
        self.recent_closed_positions = [closed, *self.recent_closed_positions[:19]]
        self._record_trade_history(managed, closed, reason)
        return self._event("critical_unprotected" if cancellation_error is not None else "exit_submitted", symbol, timestamp, price, order_id, reason, exit_side.value, managed.position.quantity, managed.position.entry_price, pnl=pnl)

    def _record_trade_history(self, managed: ManagedPosition, closed: ClosedPosition, exit_reason: str) -> None:
        if self.activity_repository is None or not hasattr(self.activity_repository, "save_trade"):
            return
        self.activity_repository.save_trade(
            TradeRecord(
                symbol=closed.symbol,
                entry_time=managed.position.entry_time or closed.closed_at,
                exit_time=closed.closed_at,
                entry_price=closed.entry_price,
                exit_price=closed.exit_price,
                quantity=closed.quantity,
                pnl=closed.pnl,
                side=closed.side.value,
                position_type="INTRADAY",
                strategy_name=getattr(self.strategy, "name", ""),
                exit_reason=exit_reason,
            )
        )

    def _broker_exit_fill(self, managed: ManagedPosition) -> tuple[float, str | None, str]:
        order_id = managed.protective_order_id
        if order_id and hasattr(self.orders.client, "order_history"):
            try:
                history = self.orders.client.order_history(order_id) or []
            except Exception:
                history = []
            for record in reversed(history):
                status = str(record.get("status", "")).upper()
                try:
                    average_price = float(record.get("average_price", 0) or 0)
                except (TypeError, ValueError):
                    average_price = 0
                if status in {"COMPLETE", "COMPLETED", "FILLED"} and average_price > 0:
                    return average_price, order_id, "protective SL-M fill price received from Zerodha"
        fallback_price = self.last_prices.get(managed.position.symbol, managed.position.entry_price)
        return fallback_price, order_id, "protective fill price unavailable; using the last broker quote"

    def _restore_positions(self) -> None:
        if self.activity_repository is None or not hasattr(self.activity_repository, "load_positions"):
            return
        for record in self.activity_repository.load_positions():
            if record.symbol not in self.scanner.token_to_symbol.values():
                continue
            position_side = Side(record.side)
            position = Position(record.symbol, position_side, record.quantity, record.entry_price, record.stop_loss, record.entry_time, record.target_1, record.target_2)
            self.positions.add(position)
            self.managed_positions[record.symbol] = ManagedPosition(
                position,
                TrailingStop(record.entry_price, record.stop_loss, self.settings.trailing_atr_multiplier, position_side.value),
                record.protective_order_id,
                getattr(self.strategy, "name", "") == "PRE_SPIKE_MOMENTUM",
                getattr(self.strategy, "name", "") == "high_conviction_long",
                record.target_1_hit,
            )

    def _restore_daily_limits(self) -> None:
        if self.activity_repository is None or not hasattr(self.activity_repository, "load_daily_trade_stats"):
            return
        trades, pnl = self.activity_repository.load_daily_trade_stats(date.today().isoformat())
        self.limits.trades = trades
        self.limits.realized_pnl = pnl

    def _event(
        self,
        kind: str,
        symbol: str,
        timestamp: datetime,
        price: float | None = None,
        order_id: str | None = None,
        reason: str = "",
        side: str | None = None,
        quantity: int | None = None,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        pnl: float | None = None,
    ) -> PipelineEvent:
        event = PipelineEvent(kind, symbol, timestamp, price, order_id, reason, side, quantity, entry_price, stop_loss, pnl)
        if self.activity_repository is not None:
            self.activity_repository.save_activity(
                ActivityRecord(
                    event_kind=event.kind,
                    symbol=event.symbol,
                    timestamp=event.timestamp,
                    mode=self.settings.trading_mode.value,
                    price=event.price,
                    order_id=event.order_id,
                    side=event.side,
                    quantity=event.quantity,
                    entry_price=event.entry_price,
                    stop_loss=event.stop_loss,
                    pnl=event.pnl,
                    reason=event.reason,
                )
            )
        self.notifier.send(f"{event.kind} {event.symbol} side={event.side or '-'} price={event.price or '-'} reason={event.reason or '-'}")
        return event

    def _atr_value(self, symbol: str) -> float:
        frame = validate_ohlcv(pd.DataFrame(self.history[symbol])) if self.history[symbol] else pd.DataFrame()
        period = int(getattr(self.strategy, "atr_period", 14))
        if len(frame) > period:
            value = float(atr(frame, period).iloc[-1])
            if pd.notna(value) and value > 0:
                self.last_atr[symbol] = value
        return self.last_atr.get(symbol, 0.0)

    def _normalise_tick(self, tick: dict) -> dict | None:
        if not isinstance(tick, Mapping):
            return None
        try:
            token = int(tick.get("instrument_token", 0))
            if token not in self.scanner.token_to_symbol or "last_price" not in tick:
                return None
            price = float(tick["last_price"])
            volume_value = tick.get("volume_traded", tick.get("volume", 0))
            if volume_value is None:
                volume_value = tick.get("volume", 0)
            volume = float(volume_value)
            timestamp = self._tick_timestamp(tick)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(price) or price <= 0 or not math.isfinite(volume) or volume < 0:
            return None
        return {
            **tick,
            "instrument_token": token,
            "last_price": price,
            "volume_traded": volume,
            "timestamp": timestamp,
        }

    @staticmethod
    def _tick_timestamp(tick: dict) -> datetime:
        value = tick.get("timestamp") or datetime.now()
        return pd.Timestamp(value).to_pydatetime()
