from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from math import floor
from time import monotonic, sleep
from typing import Callable

import pandas as pd

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import NSE_TICK_SIZE, Side, TradingMode
from app.database.models import PositionRecord, TradeRecord
from app.database.repository import Repository
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
    no_signal: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SwingSymbolEvaluation:
    """The outcome of evaluating a single symbol -- lets a caller (e.g. a parallel scan that
    submits an order the moment a qualifying candidate is found, rather than waiting for the
    whole universe to finish) act on one symbol without needing the full SwingScanResult."""

    symbol: str
    candidate: SwingCandidate | None = None
    pending_candidate: SwingCandidate | None = None
    no_signal_reasons: tuple[str, ...] = ()
    insufficient_history: bool = False
    error: str | None = None


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

    def evaluate_symbol(self, symbol: str, instrument_token: int, candle_loader: Callable[[int], pd.DataFrame]) -> SwingSymbolEvaluation:
        """Evaluate one symbol in isolation -- the per-symbol body of scan()'s loop, pulled out
        so a caller can run it the moment that symbol's candles are ready (e.g. from a
        ThreadPoolExecutor future) instead of only after every other symbol has also been
        fetched and scored. Mutates self.pending_breakouts for SWING_TREND_BREAKOUT exactly as
        scan() did -- callers must call this from a single thread (candle *fetching* can be
        parallelized, but this evaluation step is stateful and not thread-safe).
        """
        try:
            candles = candle_loader(instrument_token)
            if isinstance(self.strategy, SwingTrendBreakoutStrategy):
                candidate = None
                no_signal_reasons: list[str] = []
                pending = self.pending_breakouts.pop(symbol, None)
                if pending is not None:
                    confirmed_signal = self.strategy.confirm_entry(pending, candles)
                    if confirmed_signal is not None:
                        candidate = SwingCandidate(symbol, int(instrument_token), pending, confirmed_signal)
                    else:
                        no_signal_reasons.append("breakout did not confirm on the next completed candle")
                evaluation = self.strategy.evaluate(symbol, candles)
                pending_candidate = None
                if evaluation.qualified:
                    self.pending_breakouts[symbol] = evaluation
                    pending_candidate = SwingCandidate(symbol, int(instrument_token), evaluation)
                else:
                    no_signal_reasons.append("; ".join(evaluation.rejection_reasons) or "conditions not met")
                return SwingSymbolEvaluation(symbol, candidate=candidate, pending_candidate=pending_candidate, no_signal_reasons=tuple(no_signal_reasons))
            evaluation = self.strategy.evaluate(symbol, candles, instrument_token)
        except NoSignal as no_signal_error:
            return SwingSymbolEvaluation(symbol, no_signal_reasons=(str(no_signal_error) or "no qualifying signal",))
        except ValueError as error:
            if "not enough completed daily candles" in str(error):
                return SwingSymbolEvaluation(symbol, insufficient_history=True)
            return SwingSymbolEvaluation(symbol, error=str(error))
        except Exception as error:
            return SwingSymbolEvaluation(symbol, error=str(error))
        return SwingSymbolEvaluation(symbol, candidate=SwingCandidate(symbol, int(instrument_token), evaluation))

    def scan(
        self,
        selected_symbols: dict[str, int],
        candle_loader: Callable[[int], pd.DataFrame],
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> SwingScanResult:
        candidates: list[SwingCandidate] = []
        pending_candidates: list[SwingCandidate] = []
        errors: list[str] = []
        insufficient_history: list[str] = []
        no_signal: list[tuple[str, str]] = []
        total = len(selected_symbols)
        for index, (symbol, instrument_token) in enumerate(selected_symbols.items(), start=1):
            if on_progress is not None:
                on_progress(index, total, symbol)
            result = self.evaluate_symbol(symbol, instrument_token, candle_loader)
            if result.error is not None:
                errors.append(f"{symbol}: {result.error}")
                continue
            if result.insufficient_history:
                insufficient_history.append(symbol)
                continue
            if result.candidate is not None:
                candidates.append(result.candidate)
            if result.pending_candidate is not None:
                pending_candidates.append(result.pending_candidate)
            for reason in result.no_signal_reasons:
                no_signal.append((symbol, reason))
        candidates.sort(key=lambda item: item.signal.timestamp, reverse=True)
        return SwingScanResult(
            tuple(candidates),
            tuple(errors),
            len(selected_symbols),
            tuple(insufficient_history),
            tuple(pending_candidates),
            self.strategy_name,
            tuple(no_signal),
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
            return SwingOrderResult(
                candidate.symbol,
                "rejected",
                f"amount limit ₹{amount_limit:,.2f} is smaller than one share at ₹{signal.price:,.2f} (quantity limit {quantity_limit})",
            )
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
            order_id = self._place_market_order(exchange, tradingsymbol, quantity, signal.price)
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
                exit_order_id = self._place_market_exit(pending_entry.candidate.symbol, filled_quantity, pending_entry.candidate.signal.price)
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
                position = self.active_positions[symbol]
                # The standalone trailing-stop agent reconciles every LIVE swing position in the
                # same shared `positions` table independently of this session -- if it already
                # detected and recorded this exact close (deleting the row), doing so again here
                # would write a second, duplicate trades/activity row for the same close. Only
                # drop this session's own in-memory tracking in that case.
                if self.repository is not None and not any(
                    record.symbol == symbol for record in self.repository.load_positions()
                ):
                    del self.active_positions[symbol]
                    continue
                exit_price = self._broker_exit_fill_price(position)
                del self.active_positions[symbol]
                self._delete_position(symbol)
                self._record_trade_history(position, position.quantity, exit_price, "broker-side position closed (protective stop or manual exit)")

    def _broker_exit_fill_price(self, position: SwingPosition) -> float:
        order_id = position.protective_order_id
        if order_id and hasattr(self.client, "order_history"):
            try:
                history = self.client.order_history(order_id) or []
            except Exception:
                history = []
            for record in reversed(history):
                status = str(record.get("status", "")).upper()
                try:
                    average_price = float(record.get("average_price", 0) or 0)
                except (TypeError, ValueError):
                    average_price = 0
                if status in {"COMPLETE", "COMPLETED", "FILLED"} and average_price > 0:
                    return average_price
        return position.stop_loss

    @staticmethod
    def _tradingsymbol(symbol: str) -> str:
        return symbol.split(":", 1)[1] if ":" in symbol else symbol

    @staticmethod
    def _exchange(symbol: str) -> str:
        return symbol.split(":", 1)[0] if ":" in symbol else "NSE"

    def _place_market_order(self, exchange: str, tradingsymbol: str, quantity: int, price: float) -> str:
        return self.orders.place(OrderRequest(f"{exchange}:{tradingsymbol}", Side.BUY, quantity, price, product="CNC", exchange=exchange))

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

    def _place_market_exit(self, symbol: str, quantity: int, price: float) -> str:
        return self.orders.place(OrderRequest(symbol, Side.SELL, quantity, price, product="CNC", exchange=self._exchange(symbol)))

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
                trading_mode=self.mode.value,
            )
        )

    def _delete_position(self, symbol: str) -> None:
        if self.repository is None:
            return
        self.repository.delete_position(symbol)

    def _record_trade_history(self, position: SwingPosition, quantity: int, exit_price: float, exit_reason: str) -> None:
        if self.repository is None:
            return
        self.repository.save_trade(
            TradeRecord(
                symbol=position.symbol,
                entry_time=position.entry_timestamp,
                exit_time=datetime.now(),
                entry_price=position.entry_price,
                exit_price=exit_price,
                quantity=quantity,
                pnl=(exit_price - position.entry_price) * quantity,
                side=Side.BUY.value,
                position_type="SWING",
                strategy_name=position.strategy_name,
                exit_reason=exit_reason,
            )
        )

    @staticmethod
    def _round_down_to_tick(price: float, tick_size: float) -> float:
        if price <= 0 or tick_size <= 0:
            raise ValueError("price and tick size must be positive")
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))
        rounded = (price_decimal / tick_decimal).to_integral_value(rounding=ROUND_FLOOR) * tick_decimal
        return float(rounded)

