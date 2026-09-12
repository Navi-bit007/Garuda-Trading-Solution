from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import Strategy
from app.strategy.signal import Signal


class StrategySelector:
    def __init__(self, strategies: Iterable[Strategy]):
        self.strategies = tuple(strategies)
        if not self.strategies:
            raise ValueError("at least one strategy is required")

    def select(self, symbol: str, candles: pd.DataFrame) -> Signal:
        signals = [strategy.generate_signal(symbol, candles) for strategy in self.strategies]
        entries = [signal for signal in signals if signal.action != SignalAction.HOLD]
        if not entries:
            return signals[0]
        actions = {signal.action for signal in entries}
        if len(actions) != 1:
            return Signal(symbol, SignalAction.HOLD, signals[0].timestamp, signals[0].price, reason="strategies disagree")
        return entries[0]
