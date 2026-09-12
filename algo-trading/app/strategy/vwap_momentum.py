from __future__ import annotations

import pandas as pd

from app.config.constants import SignalAction
from app.market.indicators import atr, vwap
from app.strategy.base import Strategy
from app.strategy.signal import Signal


class VwapMomentumStrategy(Strategy):
    name = "vwap_momentum"

    def __init__(self, atr_period: int = 14, stop_atr: float = 1.5):
        self.atr_period = atr_period
        self.stop_atr = stop_atr
        self.warmup_period = atr_period + 1

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if len(candles) < self.atr_period + 2:
            raise ValueError("not enough completed candles for strategy")
        line = vwap(candles)
        volatility = atr(candles, self.atr_period)
        current = len(candles) - 1
        previous = current - 1
        close = candles["close"]
        action = SignalAction.HOLD
        reason = "no confirmed VWAP cross"
        if close.iloc[previous] <= line.iloc[previous] and close.iloc[current] > line.iloc[current]:
            action, reason = SignalAction.BUY, "confirmed bullish VWAP cross"
        elif close.iloc[previous] >= line.iloc[previous] and close.iloc[current] < line.iloc[current]:
            action, reason = SignalAction.SELL, "confirmed bearish VWAP cross"
        price = float(close.iloc[current])
        distance = float(volatility.iloc[current] * self.stop_atr)
        stop = price - distance if action == SignalAction.BUY else price + distance if action == SignalAction.SELL else None
        return Signal(symbol, action, candles["timestamp"].iloc[current].to_pydatetime(), price, stop, reason)
