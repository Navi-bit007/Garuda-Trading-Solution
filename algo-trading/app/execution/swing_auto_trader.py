from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from math import floor
from time import monotonic, sleep
from typing import Callable

import pandas as pd

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import NSE_TICK_SIZE, Side, TradingMode
from app.database.models import PositionRecord
from app.database.repository import Repository
from app.execution.swing_trailing import compute_ema_swing_stop, compute_trend_breakout_stop
from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema
from app.strategy.base import NoSignal
from app.strategy.ema_9_200_swing import Ema9200SwingEvaluation, Ema9200SwingStrategy
from app.strategy.signal import Signal
from app.strategy.swing_trend_breakout import SwingTrendBreakoutEvaluation, SwingTrendBreakoutStrategy


@dataclass(frozen=True)
class SwingCandidate:
    symbol: str
    instrument_token: int
    evaluation: Ema9200SwingEvaluation | SwingTrendBreakoutEvaluation
    signal_override: Signal | None = None

    @property
    def signal(self) -> Signal | None:
        return self.signal_override or self.evaluation.signal


@dataclass(frozen=True)
class SwingScanResult:
    candidates: tuple[SwingCandidate, ...]
    errors: tuple[str, ...]
    scanned: int
    insufficient_history: tuple[str, ...] = ()
    pending_candidates: tuple[SwingCandidate, ...] = ()
    strategy_name: str = "EMA 9/200 swing"


@dataclass
class SwingPosition:
    symbol: str
    instrument_token: int
    quantity: int
    entry_price: float
    stop_loss: float
    order_id: str
    protective_order_id: str | None
    entry_timestamp: object
    last_trailing_candle: object
    target_1: float | None = None
    partial_profit_booked: bool = False
    strategy_name: str = "EMA 9/200 swing"


@dataclass(frozen=True)
class SwingOrderResult:
    symbol: str
    status: str
    reason: str
    quantity: int = 0
    order_id: str | None = None
    protective_order_id: str | None = None


@dataclass(frozen=True)
class PendingSwingEntry:
    candidate: SwingCandidate
    quantity: int
    order_id: str
    stop_loss: float


class SwingAutoTrader:
    ENTRY_FILL_TIMEOUT_SECONDS = 10.0
    ENTRY_FILL_POLL_SECONDS = 0.5

    def __init__(
        self,
        client,
        mode: TradingMode = TradingMode.LIVE,
        trailing_atr_multiplier: float = 2.0,
        strategy_name: str = "EMA 9/200 swing",
        repository: Repository | None = None,
    ):
        if trailing_atr_multiplier <= 0:
            raise ValueError("trailing ATR multiplier must be positive")
        self.client = client
        self.mode = mode
        self.trailing_atr_multiplier = trailing_atr_multiplier
        self.strategy_name = ""
        self.strategy = Ema9200SwingStrategy()
        self.orders = OrderAPI(mode, client)
        self.repository = repository
        self.tick_sizes: dict[int, float] = {}
        self.active_positions: dict[str, SwingPosition] = {}
        self.submitted_signal_keys: set[str] = set()
        self.broker_open_symbols: set[str] = set()
        self.pending_entries: dict[str, PendingSwingEntry] = {}
        self.pending_breakouts: dict[str, SwingTrendBreakoutEvaluation] = {}
        self.set_strategy(strategy_name)

    def set_strategy(self, strategy_name: str) -> None:
        if strategy_name == "SWING_TREND_BREAKOUT":
            self.strategy = SwingTrendBreakoutStrategy()
            self.strategy_name = strategy_name
            self.pending_breakouts.clear()
            return
        if strategy_name in ("EMA 9/200 swing", "EMA_9_200_SWING"):
            self.strategy = Ema9200SwingStrategy()
            self.strategy_name = "EMA 9/200 swing"
            self.pending_breakouts.clear()
            return
        raise ValueError(f"unsupported swing strategy: {strategy_name}")

    def scan(
        self,
        selected_symbols: dict[str, int],
        candle_loader: Callable[[int], pd.DataFrame],
    ) -> SwingScanResult:
        candidates: list[SwingCandidate] = []
        pending_candidates: list[SwingCandidate] = []
        errors: list[str] = []
        insufficient_history: list[str] = []
        for symbol, instrument_token in selected_symbols.items():
            try:
                candles = candle_loader(instrument_token)
                if isinstance(self.strategy, SwingTrendBreakoutStrategy):
                    pending = self.pending_breakouts.pop(symbol, None)
                    if pending is not None:
                        confirmed_signal = self.strategy.confirm_entry(pending, candles)
                        if confirmed_signal is not None:
                            candidates.append(SwingCandidate(symbol, int(instrument_token), pending, confirmed_signal))
                    evaluation = self.strategy.evaluate(symbol, candles)
                    if evaluation.qualified:
                        self.pending_breakouts[symbol] = evaluation
                        pending_candidates.append(SwingCandidate(symbol, int(instrument_token), evaluation))
                    continue
                evaluation = self.strategy.evaluate(symbol, candles, instrument_token)
            except NoSignal:
                continue
            except ValueError as error:
                if "not enough completed daily candles" in str(error):
                    insufficient_history.append(symbol)
                    continue
                errors.append(f"{symbol}: {error}")
                continue
            except Exception as error:
                errors.append(f"{symbol}: {error}")
                continue
            candidates.append(SwingCandidate(symbol, int(instrument_token), evaluation))
        candidates.sort(key=lambda item: item.signal.timestamp, reverse=True)
        return SwingScanResult(
            tuple(candidates),
            tuple(errors),
            len(selected_symbols),
            tuple(insufficient_history),
            tuple(pending_candidates),
            self.strategy_name,
        )

    def submit_candidate(self, candidate: SwingCandidate, amount_limit: float, quantity_limit: int) -> SwingOrderResult:
        if amount_limit <= 0 or quantity_limit <= 0:
            raise ValueError("amount and quantity limits must be positive")
        if candidate.signal is None:
            return SwingOrderResult(candidate.symbol, "skipped", "candidate is awaiting confirmation")
        tradingsymbol = self._tradingsymbol(candidate.symbol)
        if candidate.symbol in self.active_positions or tradingsymbol in self.broker_open_symbols:
            return SwingOrderResult(candidate.symbol, "skipped", "position is already open")
        signal = candidate.signal
        signal_key = f"{candidate.symbol}:{signal.timestamp.isoformat()}"
        pending_entry = self.pending_entries.get(signal_key)
        if pending_entry is not None:
            return self._complete_pending_entry(signal_key, pending_entry)
        if signal_key in self.submitted_signal_keys:
            return SwingOrderResult(candidate.symbol, "skipped", "signal was already submitted")
        quantity = min(quantity_limit, floor(amount_limit / signal.price))
        if quantity < 1:
            return SwingOrderResult(candidate.symbol, "rejected", "amount limit is smaller than one share")
        if self.mode != TradingMode.LIVE:
            return SwingOrderResult(candidate.symbol, "rejected", "swing orders require LIVE trading mode", quantity)

        exchange = self._exchange(candidate.symbol)
        try:
            tick_size = self.orders.refresh_tick_size(candidate.symbol, exchange)
        except ValueError as error:
            return SwingOrderResult(
                candidate.symbol,
                "rejected",
                f"verified instrument tick size unavailable; no BUY submitted: {error}",
                quantity,
            )
        stop_loss = self._round_down_to_tick(signal.stop_loss, tick_size)
        try:
            order_id = self._place_market_order(exchange, tradingsymbol, quantity)
        except Exception as error:
            return SwingOrderResult(candidate.symbol, "rejected", f"broker order rejected: {error}", quantity)

        pending_entry = PendingSwingEntry(candidate, quantity, order_id, stop_loss)
        self.pending_entries[signal_key] = pending_entry
        return self._complete_pending_entry(signal_key, pending_entry)

    def _complete_pending_entry(self, signal_key: str, pending_entry: PendingSwingEntry) -> SwingOrderResult:
        tradingsymbol = self._tradingsymbol(pending_entry.candidate.symbol)
        exchange = self._exchange(pending_entry.candidate.symbol)
        filled_quantity = self._wait_for_entry_holding(
            pending_entry.order_id,
            tradingsymbol,
            pending_entry.quantity,
        )
        if filled_quantity <= 0:
            return SwingOrderResult(
                pending_entry.candidate.symbol,
                "entry_pending",
                "CNC BUY submitted; waiting for the broker to confirm the filled holding before placing SL-M",
                pending_entry.quantity,
                pending_entry.order_id,
            )

        try:
            protective_order_id = self._place_protective_stop(
                exchange,
                tradingsymbol,
                filled_quantity,
                pending_entry.stop_loss,
                pending_entry.candidate.signal.price,
            )
        except Exception as error:
            try:
                exit_order_id = self._place_market_exit(pending_entry.candidate.symbol, filled_quantity)
            except Exception as exit_error:
                self.pending_entries.pop(signal_key, None)
                self._register_position(
                    pending_entry.candidate,
                    filled_quantity,
                    pending_entry.order_id,
                    None,
                    pending_entry.stop_loss,
                )
                return SwingOrderResult(
                    pending_entry.candidate.symbol,
                    "critical_unprotected",
                    f"SL-M was rejected after the holding was confirmed, and automatic SELL also failed: {exit_error}",
                    filled_quantity,
                    pending_entry.order_id,
                )
            self.pending_entries.pop(signal_key, None)
            return SwingOrderResult(
                pending_entry.candidate.symbol,
                "flattened_after_protective_stop_failure",
                f"SL-M was rejected after the holding was confirmed; automatic SELL submitted: {error}",
                filled_quantity,
                pending_entry.order_id,
                exit_order_id,
            )

        self.pending_entries.pop(signal_key, None)
        self._register_position(
            pending_entry.candidate,
            filled_quantity,
            pending_entry.order_id,
            protective_order_id,
            pending_entry.stop_loss,
        )
        return SwingOrderResult(
            pending_entry.candidate.symbol,
            "submitted",
            "CNC entry filled and Zerodha-side SL-M stop submitted",
            filled_quantity,
            pending_entry.order_id,
            protective_order_id,
        )

    def _wait_for_entry_holding(self, order_id: str, tradingsymbol: str, requested_quantity: int) -> int:
        deadline = monotonic() + self.ENTRY_FILL_TIMEOUT_SECONDS
        while True:
            try:
                status, filled_quantity = self._entry_order_status(order_id)
                holding_quantity = self._holding_quantity(tradingsymbol)
            except Exception:
                status, filled_quantity, holding_quantity = "", 0, 0
            confirmed_quantity = min(requested_quantity, filled_quantity, holding_quantity)
            if confirmed_quantity > 0:
                return confirmed_quantity
            if status in {"REJECTED", "CANCELLED"} or monotonic() >= deadline:
                return 0
            sleep(self.ENTRY_FILL_POLL_SECONDS)

    def _entry_order_status(self, order_id: str) -> tuple[str, int]:
        history = self.client.order_history(order_id)
        if not history:
            return "", 0
        latest = history[-1]
        return str(latest.get("status", "")).upper(), int(latest.get("filled_quantity", 0) or 0)

    def _holding_quantity(self, tradingsymbol: str) -> int:
        for position in self.client.positions().get("net", []):
            if str(position.get("tradingsymbol")) == tradingsymbol:
                return max(0, int(position.get("quantity", 0) or 0))
        return 0

    def manage_position(self, symbol: str, candles: pd.DataFrame, current_price: float | None = None) -> SwingOrderResult | None:
        """Manage a trend-breakout position from completed daily candles."""
        position = self.active_positions.get(symbol)
        if position is None or position.strategy_name != "SWING_TREND_BREAKOUT":
            return None
        frame = validate_ohlcv(candles)
        if frame.empty:
            return None
        latest = frame.iloc[-1]
        latest_timestamp = latest["timestamp"]
        close = float(latest["close"])
        market_price = float(current_price) if current_price is not None else close

        if market_price <= position.stop_loss:
            return SwingOrderResult(symbol, "stop_triggered", "configured stop was reached; broker-side SL-M remains the exit boundary", position.quantity, protective_order_id=position.protective_order_id)

        if position.target_1 is not None and not position.partial_profit_booked and market_price >= position.target_1:
            partial_quantity = min(position.quantity, max(1, floor(position.quantity * 0.4)))
            order_id = self._place_market_exit(symbol, partial_quantity)
            position.quantity -= partial_quantity
            position.partial_profit_booked = True
            if position.quantity > 0 and position.protective_order_id is not None:
                self._modify_protective_stop(symbol, position.protective_order_id, position.quantity, position.stop_loss, market_price)
            self._save_position(position)
            return SwingOrderResult(symbol, "partial_profit_booked", f"booked 40% at 2R; remaining quantity {position.quantity}", partial_quantity, order_id, position.protective_order_id)

        if latest_timestamp > position.entry_timestamp and close < float(ema(frame["close"], 20).iloc[-1]):
            order_id = self._place_market_exit(symbol, position.quantity)
            quantity = position.quantity
            del self.active_positions[symbol]
            self._delete_position(symbol)
            try:
                self._cancel_protective_stop(position.protective_order_id)
            except Exception as error:
                return SwingOrderResult(
                    symbol,
                    "critical_unprotected",
                    f"market exit submitted but protective stop cancellation failed: {error}",
                    quantity,
                    order_id,
                    position.protective_order_id,
                )
            return SwingOrderResult(symbol, "exited", "completed daily close below EMA20", quantity, order_id, position.protective_order_id)

        candidate_stop = compute_trend_breakout_stop(frame, self._position_tick_size(position))
        if candidate_stop is not None and position.protective_order_id is not None and position.quantity > 0 and position.last_trailing_candle < latest_timestamp and position.stop_loss < candidate_stop < market_price:
            self._modify_protective_stop(symbol, position.protective_order_id, position.quantity, candidate_stop, market_price)
            position.stop_loss = candidate_stop
            position.last_trailing_candle = latest_timestamp
            self._save_position(position)
            return SwingOrderResult(symbol, "stop_trailed", f"EMA20 trailing stop moved to {candidate_stop:.2f}", position.quantity, protective_order_id=position.protective_order_id)
        position.last_trailing_candle = max(position.last_trailing_candle, latest_timestamp)
        return None

    def trail_position(self, symbol: str, candles: pd.DataFrame, current_price: float | None = None) -> float | None:
        position = self.active_positions.get(symbol)
        if position is None or position.protective_order_id is None:
            return None
        frame = validate_ohlcv(candles)
        if len(frame) < self.strategy.atr_period:
            return None
        latest = frame.iloc[-1]
        latest_timestamp = latest["timestamp"]
        if latest_timestamp <= position.last_trailing_candle:
            return None
        reference_price = float(latest["close"])
        if current_price is not None:
            reference_price = min(reference_price, float(current_price))
        candidate_stop = compute_ema_swing_stop(
            frame,
            self.strategy.atr_period,
            self.trailing_atr_multiplier,
            reference_price,
            self._position_tick_size(position),
        )
        if candidate_stop is None:
            position.last_trailing_candle = latest_timestamp
            return None
        if candidate_stop <= position.stop_loss or candidate_stop >= reference_price:
            position.last_trailing_candle = latest_timestamp
            return None
        self._modify_protective_stop(
            symbol,
            position.protective_order_id,
            position.quantity,
            candidate_stop,
            reference_price,
        )
        position.stop_loss = candidate_stop
        position.last_trailing_candle = latest_timestamp
        self._save_position(position)
        return candidate_stop

    def sync_broker_positions(self) -> None:
        if self.mode != TradingMode.LIVE:
            return
        broker_positions = self.client.positions().get("net", [])
        open_symbols = {
            str(position.get("tradingsymbol"))
            for position in broker_positions
            if int(position.get("quantity", 0) or 0) != 0
        }
        self.broker_open_symbols = open_symbols
        for symbol in list(self.active_positions):
            if self._tradingsymbol(symbol) not in open_symbols:
                del self.active_positions[symbol]
                self._delete_position(symbol)

    @staticmethod
    def _tradingsymbol(symbol: str) -> str:
        return symbol.split(":", 1)[1] if ":" in symbol else symbol

    @staticmethod
    def _exchange(symbol: str) -> str:
        return symbol.split(":", 1)[0] if ":" in symbol else "NSE"

    def _place_market_order(self, exchange: str, tradingsymbol: str, quantity: int) -> str:
        return self.client.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=Side.BUY.value,
            quantity=quantity,
            product="CNC",
            order_type="MARKET",
            market_protection=-1,
        )

    def _place_protective_stop(
        self,
        exchange: str,
        tradingsymbol: str,
        quantity: int,
        trigger_price: float,
        reference_price: float,
    ) -> str:
        return self.orders.place_protective_stop(
            OrderRequest(
                f"{exchange}:{tradingsymbol}",
                Side.SELL,
                quantity,
                reference_price,
                trigger_price,
                "CNC",
                exchange,
            )
        )

    def _place_market_exit(self, symbol: str, quantity: int) -> str:
        return self.client.place_order(
            variety="regular",
            exchange=self._exchange(symbol),
            tradingsymbol=self._tradingsymbol(symbol),
            transaction_type=Side.SELL.value,
            quantity=quantity,
            product="CNC",
            order_type="MARKET",
            market_protection=-1,
        )

    def _register_position(
        self,
        candidate: SwingCandidate,
        quantity: int,
        order_id: str,
        protective_order_id: str | None,
        stop_loss: float,
    ) -> None:
        signal = candidate.signal
        self.submitted_signal_keys.add(f"{candidate.symbol}:{signal.timestamp.isoformat()}")
        self.broker_open_symbols.add(self._tradingsymbol(candidate.symbol))
        self.active_positions[candidate.symbol] = SwingPosition(
            symbol=candidate.symbol,
            instrument_token=candidate.instrument_token,
            quantity=quantity,
            entry_price=signal.price,
            stop_loss=stop_loss,
            order_id=order_id,
            protective_order_id=protective_order_id,
            entry_timestamp=signal.timestamp,
            last_trailing_candle=signal.timestamp,
            target_1=signal.target_1,
            strategy_name=self.strategy_name,
        )
        self._save_position(self.active_positions[candidate.symbol])

    def _save_position(self, position: SwingPosition) -> None:
        if self.repository is None:
            return
        self.repository.save_position(
            PositionRecord(
                symbol=position.symbol,
                side=Side.BUY.value,
                quantity=position.quantity,
                entry_price=position.entry_price,
                stop_loss=position.stop_loss,
                entry_time=position.entry_timestamp,
                target_1=position.target_1,
                protective_order_id=position.protective_order_id,
                instrument_token=position.instrument_token,
                position_type="SWING",
                atr_multiplier=self.trailing_atr_multiplier,
                strategy_name=position.strategy_name,
            )
        )

    def _delete_position(self, symbol: str) -> None:
        if self.repository is None:
            return
        self.repository.delete_position(symbol)

    @staticmethod
    def _round_down_to_tick(price: float, tick_size: float) -> float:
        if price <= 0 or tick_size <= 0:
            raise ValueError("price and tick size must be positive")
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))
        rounded = (price_decimal / tick_decimal).to_integral_value(rounding=ROUND_FLOOR) * tick_decimal
        return float(rounded)

    def _modify_protective_stop(
        self,
        symbol: str,
        order_id: str,
        quantity: int,
        trigger_price: float,
        reference_price: float,
    ) -> None:
        self.orders.modify_protective_stop(
            order_id,
            OrderRequest(
                symbol,
                Side.SELL,
                quantity,
                reference_price,
                trigger_price,
                "CNC",
                self._exchange(symbol),
            ),
        )

    def _position_tick_size(self, position: SwingPosition) -> float:
        return self.orders.tick_size(position.symbol, self._exchange(position.symbol))

    def _cancel_protective_stop(self, order_id: str | None) -> None:
        if order_id is None:
            return
        self.client.cancel_order(variety="regular", order_id=order_id)
