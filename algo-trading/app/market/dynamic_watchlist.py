from __future__ import annotations

from datetime import date, time

import pandas as pd

from app.market.indicators import vwap


DYNAMIC_WATCHLIST_NAME = "Dynamic - Low Near Open"
DYNAMIC_INTERVAL = "5minute"
DYNAMIC_LOOKBACK_DAYS = 2
DYNAMIC_MAX_WORKERS = 4
DYNAMIC_CANDLE_MINUTES = 5
DYNAMIC_AUTO_REFRESH_CANDLES = 3


def session_candles(frame: pd.DataFrame, session_date: date, market_open: time) -> pd.DataFrame:
    """Keep completed candles from the requested Indian-market session."""
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    timestamps = pd.to_datetime(result["timestamp"], errors="raise")
    if timestamps.dt.tz is not None:
        timestamps = timestamps.dt.tz_convert("Asia/Kolkata")
    result["timestamp"] = timestamps
    return result.loc[
        (timestamps.dt.date == session_date)
        & (timestamps.dt.time >= market_open)
    ].reset_index(drop=True)


def first_session_candles(frame: pd.DataFrame, session_date: date, market_open: time) -> pd.DataFrame:
    """Keep the exact opening candle of the requested session and later candles for context."""
    if frame.empty:
        return frame.copy()
    result = frame.copy()
    timestamps = pd.to_datetime(result["timestamp"], errors="raise")
    if timestamps.dt.tz is not None:
        timestamps = timestamps.dt.tz_convert("Asia/Kolkata")
    result["timestamp"] = timestamps
    result = result.sort_values("timestamp").reset_index(drop=True)
    timestamps = result["timestamp"]
    if "avg_volume_20" not in result.columns:
        slot = result["timestamp"].dt.strftime("%H:%M")
        result["avg_volume_20"] = result.groupby(slot, sort=False)["volume"].transform(
            lambda values: values.rolling(20, min_periods=20).mean().shift(1)
        )
    if "vwap" not in result.columns:
        result["vwap"] = vwap(result)
    current_session = result.loc[
        (timestamps.dt.date == session_date)
        & (timestamps.dt.time == market_open)
    ]
    if current_session.empty:
        return result.iloc[0:0].copy()
    first_timestamp = current_session["timestamp"].min()
    return result.loc[
        (result["timestamp"] >= first_timestamp)
        & (result["timestamp"].dt.date == session_date)
    ].reset_index(drop=True)


def evaluate_dynamic_candle(
    frame: pd.DataFrame,
    current_price: float | None = None,
    require_breakout: bool = False,
) -> dict[str, object] | None:
    """Evaluate the first session candle using Zerodha's low-near-open rule."""
    if frame.empty:
        return None

    first = frame.iloc[0]
    open_price = float(first["open"])
    low_price = float(first["low"])
    current_price = float(first["close"]) if current_price is None else float(current_price)
    if open_price <= 0:
        return None

    if not (low_price >= open_price * 0.999 and current_price > open_price):
        return None

    return {
        "candle_time": first["timestamp"],
        "open": open_price,
        "low": low_price,
        "close": first.get("close"),
        "volume": first.get("volume"),
        "avg_volume_20": first.get("avg_volume_20"),
        "vwap": first.get("vwap"),
        "first_candle_high": first.get("high"),
        "current_price": current_price,
    }


def filter_dynamic_watchlist(
    source_symbols: dict[str, int],
    candles_by_symbol: dict[str, pd.DataFrame],
    current_prices: dict[str, float] | None = None,
    require_breakout: bool = False,
) -> pd.DataFrame:
    """Return symbols whose first candle meets the low-near-open rule."""
    rows: list[dict[str, object]] = []
    for symbol, instrument_token in source_symbols.items():
        result = evaluate_dynamic_candle(
            candles_by_symbol.get(symbol, pd.DataFrame()),
            (current_prices or {}).get(symbol),
            require_breakout,
        )
        if result is not None:
            rows.append({"symbol": symbol, "instrument_token": int(instrument_token), **result})
    if not rows:
        return pd.DataFrame(
            columns=[
                "symbol",
                "instrument_token",
                "candle_time",
                "open",
                "low",
                "close",
                "volume",
                "avg_volume_20",
                "vwap",
                "first_candle_high",
                "current_price",
            ]
        )
    return pd.DataFrame(rows).sort_values("current_price", ascending=False).reset_index(drop=True)


def auto_refresh_slot(now, market_open: time) -> int | None:
    """Return the active five-minute window for the first three candles."""
    current = pd.Timestamp(now)
    if current.tzinfo is None:
        current = current.tz_localize("Asia/Kolkata")
    else:
        current = current.tz_convert("Asia/Kolkata")
    opening = current.normalize() + pd.Timedelta(
        hours=market_open.hour,
        minutes=market_open.minute,
        seconds=market_open.second,
    )
    elapsed_seconds = (current - opening).total_seconds()
    if elapsed_seconds < 0 or elapsed_seconds >= DYNAMIC_AUTO_REFRESH_CANDLES * DYNAMIC_CANDLE_MINUTES * 60:
        return None
    return int(elapsed_seconds // (DYNAMIC_CANDLE_MINUTES * 60))
