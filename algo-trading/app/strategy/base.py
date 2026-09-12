from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from app.strategy.signal import Signal


class NoSignal(Exception):
    pass


class Strategy(ABC):
    name: str
    warmup_period: int = 1

    @abstractmethod
    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        """Generate a signal from candles through the final completed bar only."""
