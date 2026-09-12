from __future__ import annotations

import pandas as pd

from app.market.indicators import ema


def classify_regime(candles: pd.DataFrame, period: int = 50) -> str:
    if len(candles) < period:
        return "UNKNOWN"
    average = ema(candles["close"], period).iloc[-1]
    close = candles["close"].iloc[-1]
    return "BULL" if close > average else "BEAR"
