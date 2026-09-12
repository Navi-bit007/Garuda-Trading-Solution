from __future__ import annotations

import pandas as pd

from app.config.constants import SignalAction
from app.market.indicators import atr, ema
from app.strategy.base import Strategy
from app.strategy.signal import Signal


class EmaTrendStrategy(Strategy):
    name = "ema_trend"

    def __init__(
        self,
        entry_ema: int = 200,
        exit_ema: int = 9,
        atr_period: int = 14,
        stop_atr: float = 1.5,
        min_gap_percent: float | None = None,
        confirmation: str = "candle_close",
        signal_mode: str | None = None,
        signal_window_candles: int = 3,
        fast: int | None = None,
        slow: int | None = None,
        strategy_name: str | None = None,
    ):
        legacy_periods = fast is not None or slow is not None
        if legacy_periods:
            if fast is None or slow is None:
                raise ValueError("fast and slow EMA periods must be provided together")
            if fast >= slow:
                raise ValueError("fast EMA period must be less than slow EMA period")
            entry_ema, exit_ema = slow, fast
        if min_gap_percent is None:
            min_gap_percent = 0.0 if legacy_periods else 1.0
        if min(entry_ema, exit_ema, atr_period, signal_window_candles) < 1 or stop_atr <= 0:
            raise ValueError("EMA periods, signal window, and stop multiplier must be positive")
        if confirmation != "candle_close":
            raise ValueError("EMA confirmation must be candle_close")
        if signal_mode is not None and signal_mode not in ("crossover", "trend"):
            raise ValueError("EMA signal mode must be crossover or trend")
        if min_gap_percent < 0:
            raise ValueError("minimum EMA gap must be non-negative")
        self.entry_ema = entry_ema
        self.exit_ema = exit_ema
        self.legacy_periods = legacy_periods
        self.fast = exit_ema
        self.slow = entry_ema
        self.atr_period = atr_period
        self.stop_atr = stop_atr
        self.confirmation = confirmation
        self.signal_mode = signal_mode or ("trend" if legacy_periods else "crossover")
        self.signal_window_candles = signal_window_candles
        self.min_gap_percent = min_gap_percent
        self.minimum_score = 80
        self.name = strategy_name or type(self).name
        self.warmup_period = max(entry_ema, exit_ema) + atr_period

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if len(candles) < self.warmup_period:
            raise ValueError("not enough completed candles for strategy")
        entry_line = ema(candles["close"], self.entry_ema)
        exit_line = ema(candles["close"], self.exit_ema)
        volatility = atr(candles, self.atr_period)
        current = len(candles) - 1
        action = SignalAction.HOLD
        current_entry = float(entry_line.iloc[current])
        current_exit = float(exit_line.iloc[current])
        current_price = float(candles["close"].iloc[current])
        gap_percent = abs(current_exit - current_entry) / current_entry * 100 if current_entry else 0.0
        gap_passed = gap_percent >= self.min_gap_percent
        bullish_trend = current_price > current_entry and current_exit > current_entry
        bearish_trend = current_price < current_entry and current_exit < current_entry
        bullish_crossover_index = None
        bearish_crossover_index = None
        if self.signal_mode == "crossover":
            window_start = max(1, current - self.signal_window_candles + 1)
            for index in range(current, window_start - 1, -1):
                if bullish_crossover_index is None and candles["close"].iloc[index - 1] <= entry_line.iloc[index - 1] and candles["close"].iloc[index] > entry_line.iloc[index]:
                    bullish_crossover_index = index
                if bearish_crossover_index is None and candles["close"].iloc[index - 1] >= exit_line.iloc[index - 1] and candles["close"].iloc[index] < exit_line.iloc[index]:
                    bearish_crossover_index = index
            buy_trigger = bullish_crossover_index is not None and bullish_trend
            sell_trigger = bearish_crossover_index is not None and bearish_trend
        else:
            buy_trigger = bullish_trend
            sell_trigger = bearish_trend
        mode_label = "EMA crossover" if self.signal_mode == "crossover" else "EMA trend alignment"
        reason = f"no confirmed {mode_label}" if self.legacy_periods else f"no confirmed candle-close {mode_label}"
        if buy_trigger and gap_passed:
            action = SignalAction.BUY
            age = current - bullish_crossover_index if bullish_crossover_index is not None else 0
            reason = f"bullish {mode_label}" if self.legacy_periods else f"fresh bullish candle-close {mode_label} ({age} candle(s) ago): price and exit EMA above entry EMA"
        elif sell_trigger and gap_passed:
            action = SignalAction.SELL
            age = current - bearish_crossover_index if bearish_crossover_index is not None else 0
            reason = f"bearish {mode_label}" if self.legacy_periods else f"fresh bearish candle-close {mode_label} ({age} candle(s) ago): price and exit EMA below entry EMA"
        price = current_price
        stop_distance = float(volatility.iloc[current] * self.stop_atr)
        stop_loss = price - stop_distance if action == SignalAction.BUY else price + stop_distance if action == SignalAction.SELL else None
        score = 100 if action in (SignalAction.BUY, SignalAction.SELL) else 0
        signal_index = current
        if action == SignalAction.BUY and bullish_crossover_index is not None:
            signal_index = bullish_crossover_index
        elif action == SignalAction.SELL and bearish_crossover_index is not None:
            signal_index = bearish_crossover_index
        return Signal(
            symbol,
            action,
            candles["timestamp"].iloc[signal_index].to_pydatetime(),
            price,
            stop_loss,
            reason,
            score=score,
            entry_price=price,
        )
