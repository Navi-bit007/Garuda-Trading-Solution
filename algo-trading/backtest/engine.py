from __future__ import annotations

import pandas as pd

from app.strategy.base import Strategy
from backtest.simulator import Simulator, Trade


class BacktestEngine:
    def __init__(self, simulator: Simulator | None = None):
        self.simulator = simulator or Simulator()

    def run(self, symbol: str, candles: pd.DataFrame, strategy: Strategy, quantity: int = 1) -> list[Trade]:
        if candles["timestamp"].duplicated().any():
            raise ValueError("backtest data timestamps must be unique")
        return self.simulator.run(symbol, candles, strategy, quantity)
