from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")


def validate_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Missing OHLCV columns: {sorted(missing)}")
    result = frame.loc[:, REQUIRED_COLUMNS].copy()
    result["timestamp"] = pd.to_datetime(result["timestamp"], errors="raise")
    numeric_columns = ["open", "high", "low", "close", "volume"]
    result[numeric_columns] = result[numeric_columns].apply(pd.to_numeric, errors="raise")
    if result["timestamp"].duplicated().any():
        raise ValueError("OHLCV timestamps must be unique")
    if (result["high"] < result[["open", "close"]].max(axis=1)).any():
        raise ValueError("high must be at least open and close")
    if (result["low"] > result[["open", "close"]].min(axis=1)).any():
        raise ValueError("low must be at most open and close")
    return result.sort_values("timestamp").reset_index(drop=True)


def load_csv(path: str) -> pd.DataFrame:
    return validate_ohlcv(pd.read_csv(path))


@dataclass
class TickCandleBuilder:
    interval: str = "1min"
    current: dict | None = None
    previous_cumulative_volume: float | None = None

    def update(self, tick: dict) -> dict | None:
        timestamp = pd.Timestamp(tick.get("timestamp") or pd.Timestamp.now()).floor(self.interval)
        price = float(tick["last_price"])
        cumulative_volume = float(tick.get("volume_traded", tick.get("volume", 0)))
        if self.previous_cumulative_volume is None:
            volume = 0.0
        else:
            volume = max(0.0, cumulative_volume - self.previous_cumulative_volume)
        self.previous_cumulative_volume = cumulative_volume
        if self.current is None:
            self.current = {
                "timestamp": timestamp,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": volume,
            }
            return None
        if timestamp == self.current["timestamp"]:
            self.current["high"] = max(self.current["high"], price)
            self.current["low"] = min(self.current["low"], price)
            self.current["close"] = price
            self.current["volume"] += volume
            return None
        completed = self.current
        self.current = {
            "timestamp": timestamp,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume,
        }
        return completed
