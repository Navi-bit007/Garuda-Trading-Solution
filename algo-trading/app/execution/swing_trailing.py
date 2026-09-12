from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

import pandas as pd

from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema


def round_down_to_tick(price: float, tick_size: float) -> float:
    if price <= 0 or tick_size <= 0:
        raise ValueError("price and tick size must be positive")
    price_decimal = Decimal(str(price))
    tick_decimal = Decimal(str(tick_size))
    rounded = (price_decimal / tick_decimal).to_integral_value(rounding=ROUND_FLOOR) * tick_decimal
    return float(rounded)


def compute_trend_breakout_stop(candles: pd.DataFrame, tick_size: float) -> float | None:
    """EMA20 trend-following candidate stop for the SWING_TREND_BREAKOUT strategy.

    Mirrors the trailing math in SwingAutoTrader.manage_position; callers own the
    dedupe/monotonic/modify-and-persist orchestration.
    """
    frame = validate_ohlcv(candles)
    if frame.empty:
        return None
    ema20_value = float(ema(frame["close"], 20).iloc[-1])
    if not pd.notna(ema20_value) or ema20_value <= 0:
        return None
    return round_down_to_tick(ema20_value, tick_size)


def compute_ema_swing_stop(
    candles: pd.DataFrame,
    atr_period: int,
    atr_multiplier: float,
    reference_price: float,
    tick_size: float,
) -> float | None:
    """ATR-based candidate stop for the EMA 9/200 swing strategy.

    Mirrors the trailing math in SwingAutoTrader.trail_position; callers own the
    dedupe/monotonic/modify-and-persist orchestration.
    """
    frame = validate_ohlcv(candles)
    if len(frame) < atr_period:
        return None
    atr_value = float(atr(frame, atr_period).iloc[-1])
    if not pd.notna(atr_value) or atr_value <= 0:
        return None
    candidate_stop = reference_price - atr_multiplier * atr_value
    if candidate_stop <= 0:
        return None
    return round_down_to_tick(candidate_stop, tick_size)
