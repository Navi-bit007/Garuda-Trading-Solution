from __future__ import annotations

import pandas as pd

from app.config.constants import SignalAction
from app.market.indicators import atr
from app.strategy.base import Strategy
from app.strategy.signal import Signal


class AtrMomentumStrategy(Strategy):
    name = "atr_momentum"

    def __init__(self, period: int = 14, threshold: float = 0.5):
        self.period = period
        self.threshold = threshold
        self.warmup_period = period + 1

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if len(candles) < self.period + 1:
            raise ValueError("not enough completed candles for ATR momentum")
        volatility = atr(candles, self.period).iloc[-1]
        change = float(candles["close"].iloc[-1] - candles["open"].iloc[-1])
        action = SignalAction.BUY if change > self.threshold * volatility else SignalAction.SELL if change < -self.threshold * volatility else SignalAction.HOLD
        return Signal(symbol, action, candles["timestamp"].iloc[-1].to_pydatetime(), float(candles["close"].iloc[-1]), None, "ATR-normalized momentum")
