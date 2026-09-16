from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import sleep
from typing import Callable

import pandas as pd

from app.market.candles import validate_ohlcv
from app.broker.market_data import MarketData


# Kite historical-data request limits vary by interval. Keeping the limits here makes
# range splitting explicit and easy to test without coupling the simulator to Kite.
MAX_REQUEST_DAYS = {
    "minute": 60,
    "3minute": 100,
    "5minute": 100,
    "10minute": 100,
    "15minute": 200,
    "30minute": 200,
    "60minute": 400,
    "day": 2000,
}


@dataclass(frozen=True)
class HistoricalRequest:
    instrument_token: int
    start: datetime
    end: datetime
    interval: str


class KiteHistoricalDataLoader:
    """Fetch and normalize historical candles from Kite Connect.

    The loader is deliberately independent of Streamlit and trading execution. A caller can
    provide a sleep function for production pacing or a no-op in tests.
    """

    def __init__(
        self,
        market_data: MarketData,
        request_pause_seconds: float = 0.35,
        sleeper: Callable[[float], None] = sleep,
    ):
        self.market_data = market_data
        self.request_pause_seconds = max(0.0, float(request_pause_seconds))
        self.sleeper = sleeper
        self._cache: dict[HistoricalRequest, pd.DataFrame] = {}

    def load(
        self,
        instrument_token: int,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> pd.DataFrame:
        if end < start:
            raise ValueError("historical end must be on or after start")
        if interval not in MAX_REQUEST_DAYS:
            raise ValueError(f"unsupported Kite historical interval: {interval}")
        request = HistoricalRequest(int(instrument_token), start, end, interval)
        cached = self._cache.get(request)
        if cached is not None:
            return cached.copy()

        frames: list[pd.DataFrame] = []
        cursor = start
        maximum_days = MAX_REQUEST_DAYS[interval]
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=maximum_days))
            rows = self.market_data.historical(request.instrument_token, cursor, chunk_end, interval)
            if rows:
                frames.append(self._normalize(rows))
            cursor = chunk_end + timedelta(microseconds=1)
            if cursor <= end and self.request_pause_seconds:
                self.sleeper(self.request_pause_seconds)

        if not frames:
            result = self._empty_frame()
        else:
            result = pd.concat(frames, ignore_index=True)
            result = validate_ohlcv(result).drop_duplicates("timestamp", keep="last").reset_index(drop=True)
        self._cache[request] = result
        return result.copy()

    @staticmethod
    def _normalize(rows: list[dict]) -> pd.DataFrame:
        frame = pd.DataFrame(rows)
        if "date" in frame.columns and "timestamp" not in frame.columns:
            frame = frame.rename(columns={"date": "timestamp"})
        return validate_ohlcv(frame)

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
