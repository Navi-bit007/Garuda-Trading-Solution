from __future__ import annotations

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import Strategy
from app.strategy.signal import Signal


class OpeningRangeBreakoutStrategy(Strategy):
    name = "opening_range_breakout"

    def __init__(self, opening_bars: int = 3):
        self.opening_bars = opening_bars
        self.warmup_period = opening_bars + 1

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if len(candles) <= self.opening_bars:
            raise ValueError("not enough completed candles for opening range")
        opening = candles.iloc[: self.opening_bars]
        current = candles.iloc[-1]
        high, low = float(opening["high"].max()), float(opening["low"].min())
        action = SignalAction.BUY if current["close"] > high else SignalAction.SELL if current["close"] < low else SignalAction.HOLD
        stop = low if action == SignalAction.BUY else high if action == SignalAction.SELL else None
        return Signal(symbol, action, current["timestamp"].to_pydatetime(), float(current["close"]), stop, "opening range breakout")
