from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.market.candles import validate_ohlcv
from app.strategy.base import NoSignal, Strategy


@dataclass(frozen=True)
class HistoricalScanResult:
    matches: tuple[dict[str, object], ...]
    errors: tuple[str, ...]
    scanned: int


def scan_historical_watchlist(
    selected_date: date,
    selected_symbols: dict[str, int],
    strategy: Strategy,
    candle_loader: Callable[[int], pd.DataFrame],
) -> HistoricalScanResult:
    matches: list[dict[str, object]] = []
    errors: list[str] = []
    for symbol, instrument_token in selected_symbols.items():
        try:
            frame = _completed_frame_for_date(candle_loader(int(instrument_token)), selected_date)
            if frame.empty or frame.iloc[-1]["timestamp"].date() != selected_date:
                raise NoSignal("selected date has no completed daily candle")
            if hasattr(strategy, "evaluate"):
                evaluation = strategy.evaluate(symbol, frame, int(instrument_token))
                signal = evaluation.signal
            else:
                evaluation = None
                signal = strategy.generate_signal(symbol, frame)
        except NoSignal:
            continue
        except Exception as error:
            errors.append(f"{symbol}: {error}")
            continue
        matches.append(
            {
                "symbol": symbol,
                "instrument_token": int(instrument_token),
                "side": signal.side,
                "price": signal.price,
                "stop_loss": signal.stop_loss,
                "timestamp": signal.timestamp,
                "score": signal.score,
                "reason": signal.reason,
                "metadata": dict(signal.metadata),
                "ema9": getattr(evaluation, "ema9", None),
                "ema200": getattr(evaluation, "ema200", getattr(evaluation, "current_ema200", None)),
                "ema20": getattr(evaluation, "current_ema20", None),
                "atr": getattr(evaluation, "atr", None),
            }
        )
    matches.sort(key=lambda row: (row["timestamp"], row["symbol"]), reverse=True)
    return HistoricalScanResult(tuple(matches), tuple(errors), len(selected_symbols))


def _completed_frame_for_date(frame: pd.DataFrame, selected_date: date) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    result = validate_ohlcv(frame)
    timestamps = pd.to_datetime(result["timestamp"], errors="raise")
    local_timestamps = timestamps.dt.tz_convert("Asia/Kolkata") if timestamps.dt.tz is not None else timestamps
    result = result.loc[local_timestamps.dt.date <= selected_date].copy()
    return result.sort_values("timestamp").reset_index(drop=True)
