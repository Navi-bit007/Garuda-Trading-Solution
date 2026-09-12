from datetime import date

import pandas as pd

from app.market.historical_scan import scan_historical_watchlist
from app.strategy.ema_9_200_swing import Ema9200SwingStrategy


def daily_frame() -> pd.DataFrame:
    dates = pd.date_range("2025-01-01", periods=202, freq="D")
    closes = [100.0] * 200 + [130.0, 140.0]
    return pd.DataFrame(
        {
            "timestamp": dates,
            "open": closes,
            "high": [value + 2.0 for value in closes],
            "low": [value - 2.0 for value in closes],
            "close": closes,
            "volume": [100_000.0] * len(closes),
        }
    )


def test_historical_scan_uses_selected_date_and_returns_all_matches():
    selected_date = date(2025, 7, 20)
    result = scan_historical_watchlist(
        selected_date,
        {"NSE:AAA": 1, "NSE:BBB": 2},
        Ema9200SwingStrategy(),
        lambda token: daily_frame(),
    )

    assert result.scanned == 2
    assert {match["symbol"] for match in result.matches} == {"NSE:AAA", "NSE:BBB"}
    assert result.matches[0]["timestamp"].date() == selected_date
    assert result.matches[0]["price"] == 130.0
    assert result.errors == ()


def test_historical_scan_keeps_symbol_errors_local():
    def load_candles(token: int) -> pd.DataFrame:
        if token == 2:
            raise RuntimeError("historical data unavailable")
        return daily_frame()

    result = scan_historical_watchlist(
        date(2025, 7, 20),
        {"NSE:AAA": 1, "NSE:BBB": 2},
        Ema9200SwingStrategy(),
        load_candles,
    )

    assert [match["symbol"] for match in result.matches] == ["NSE:AAA"]
    assert result.errors == ("NSE:BBB: historical data unavailable",)
