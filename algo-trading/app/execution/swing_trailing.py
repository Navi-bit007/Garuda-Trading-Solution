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


def compute_trend_breakout_stop(candles: pd.DataFrame, tick_size: float) -> tuple[float, str] | None:
    """EMA20 trend-following candidate stop for the SWING_TREND_BREAKOUT strategy.

    Mirrors the trailing math in SwingAutoTrader.manage_position; callers own the
    dedupe/monotonic/modify-and-persist orchestration. Returns the candidate stop alongside a
    human-readable rendering of the calculation, for the dashboard's stop-update history.
    """
    frame = validate_ohlcv(candles)
    if frame.empty:
        return None
    ema20_value = float(ema(frame["close"], 20).iloc[-1])
    if not pd.notna(ema20_value) or ema20_value <= 0:
        return None
    candidate = round_down_to_tick(ema20_value, tick_size)
    calculation = f"EMA20 = ₹{ema20_value:.2f}, rounded down to tick ₹{tick_size:g} = ₹{candidate:.2f}"
    return candidate, calculation


def compute_ema_swing_stop(
    candles: pd.DataFrame,
    atr_period: int,
    atr_multiplier: float,
    reference_price: float,
    tick_size: float,
) -> tuple[float, str] | None:
    """ATR-based candidate stop for the EMA 9/200 swing strategy.

    Mirrors the trailing math in SwingAutoTrader.trail_position; callers own the
    dedupe/monotonic/modify-and-persist orchestration. Returns the candidate stop alongside a
    human-readable rendering of the calculation, for the dashboard's stop-update history.
    """
    frame = validate_ohlcv(candles)
    if len(frame) < atr_period:
        return None
    atr_value = float(atr(frame, atr_period).iloc[-1])
    if not pd.notna(atr_value) or atr_value <= 0:
        return None
    distance = atr_multiplier * atr_value
    candidate_stop = reference_price - distance
    if candidate_stop <= 0:
        return None
    candidate = round_down_to_tick(candidate_stop, tick_size)
    calculation = (
        f"ATR{atr_period} ₹{atr_value:.2f} × mult {atr_multiplier:.2f} = ₹{distance:.2f}; "
        f"Close ₹{reference_price:.2f} − ₹{distance:.2f} = ₹{candidate_stop:.2f}, rounded down to tick ₹{tick_size:g} = ₹{candidate:.2f}"
    )
    return candidate, calculation
