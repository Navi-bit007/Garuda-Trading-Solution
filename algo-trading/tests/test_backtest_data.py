from datetime import datetime

import pandas as pd

from app.market.backtest_data import KiteHistoricalDataLoader


class FakeMarketData:
    def __init__(self):
        self.calls = []

    def historical(self, instrument_token, start, end, interval):
        self.calls.append((instrument_token, start, end, interval))
        return [
            {
                "date": start,
                "open": 100,
                "high": 102,
                "low": 99,
                "close": 101,
                "volume": 1000,
            }
        ]


def test_kite_loader_chunks_normalizes_and_caches():
    market_data = FakeMarketData()
    loader = KiteHistoricalDataLoader(market_data, request_pause_seconds=0, sleeper=lambda _: None)
    start = datetime(2026, 1, 1)
    end = datetime(2026, 4, 15)

    first = loader.load(123, start, end, "5minute")
    second = loader.load(123, start, end, "5minute")

    assert len(market_data.calls) == 2
    assert list(first.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert first["timestamp"].is_monotonic_increasing
    assert first["timestamp"].is_unique
    pd.testing.assert_frame_equal(first, second)
