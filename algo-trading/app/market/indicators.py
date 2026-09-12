from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    if period < 1:
        raise ValueError("period must be positive")
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(frame: pd.DataFrame) -> pd.Series:
    previous_close = frame["close"].shift(1)
    return pd.concat(
        [frame["high"] - frame["low"], (frame["high"] - previous_close).abs(), (frame["low"] - previous_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    if period < 1:
        raise ValueError("period must be positive")
    return true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def vwap(frame: pd.DataFrame) -> pd.Series:
    typical_price = (frame["high"] + frame["low"] + frame["close"]) / 3
    session = frame["timestamp"].dt.date
    cumulative_value = (typical_price * frame["volume"]).groupby(session).cumsum()
    cumulative_volume = frame["volume"].groupby(session).cumsum()
    return cumulative_value / cumulative_volume.replace(0, float("nan"))


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    if period < 1:
        raise ValueError("period must be positive")
    change = series.diff()
    gains = change.clip(lower=0)
    losses = -change.clip(upper=0)
    average_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    relative_strength = average_gain / average_loss.replace(0, float("nan"))
    result = 100 - (100 / (1 + relative_strength))
    return result.where(average_loss.ne(0), 100.0)


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    if period < 1:
        raise ValueError("period must be positive")
    upward_move = frame["high"].diff()
    downward_move = -frame["low"].diff()
    plus_dm = upward_move.where((upward_move > downward_move) & (upward_move > 0), 0.0)
    minus_dm = downward_move.where((downward_move > upward_move) & (downward_move > 0), 0.0)
    smoothed_tr = true_range(frame).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    smoothed_plus = plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    smoothed_minus = minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * smoothed_plus / smoothed_tr.replace(0, float("nan"))
    minus_di = 100 * smoothed_minus / smoothed_tr.replace(0, float("nan"))
    denominator = (plus_di + minus_di).replace(0, float("nan"))
    directional_index = 100 * (plus_di - minus_di).abs() / denominator
    return directional_index.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
