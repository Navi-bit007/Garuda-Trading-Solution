from datetime import time

import pandas as pd

from app.database.models import WatchlistRecord
from dashboard import app as dashboard_app


def test_current_date_refresh_returns_first_candle_instead_of_stale_later_result(monkeypatch):
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    previous = [
        {
            "timestamp": pd.Timestamp.combine(today - pd.Timedelta(days=20 - index), time(9, 15)),
            "open": 100.0,
            "high": 101.0,
            "low": 100.0,
            "close": 100.5,
            "volume": 100.0,
        }
        for index in range(20)
    ]
    frame = pd.DataFrame(
        previous
        + [
            {"timestamp": pd.Timestamp.combine(today, time(9, 15)), "open": 100.0, "high": 101.0, "low": 100.0, "close": 100.6, "volume": 200.0},
            {"timestamp": pd.Timestamp.combine(today, time(15, 10)), "open": 100.0, "high": 120.0, "low": 90.0, "close": 120.0, "volume": 100.0},
        ]
    )

    class FakeKite:
        def quote(self, instruments):
            return {"NSE:AAA": {"instrument_token": 7, "last_price": 101.0}}

    class FakeClient:
        client = FakeKite()

    requested_lookbacks = []
    progress_updates = []

    monkeypatch.setattr(dashboard_app, "connect_kite", lambda settings, access_token: FakeClient())
    monkeypatch.setattr(
        dashboard_app,
        "load_live_candles_from_client",
        lambda client, token, interval, lookback_days, include_current=False: requested_lookbacks.append(lookback_days) or frame,
    )
    class Settings:
        market_open = time(9, 15)

    rows, errors = dashboard_app.load_dynamic_watchlist_rows(
        Settings(),
        "token",
        WatchlistRecord("alice", "Morning", {"NSE:AAA": 7}),
        today,
        progress_callback=lambda completed, total, symbol: progress_updates.append((completed, total, symbol)),
    )

    assert errors == []
    assert requested_lookbacks == [2]
    assert progress_updates == [(1, 1, "NSE:AAA")]
    assert rows.iloc[0]["candle_time"] == pd.Timestamp.combine(today, time(9, 15))
    assert rows.iloc[0]["current_price"] == 101.0


def test_current_date_refresh_rejects_symbols_without_live_price(monkeypatch):
    today = pd.Timestamp.now(tz="Asia/Kolkata").date()
    frame = pd.DataFrame(
        [{
            "timestamp": pd.Timestamp.combine(today, time(9, 15)),
            "open": 100.0,
            "high": 101.0,
            "low": 100.0,
            "close": 100.5,
            "volume": 100.0,
        }]
    )

    class FakeKite:
        def quote(self, instruments):
            return {"NSE:AAA": {"instrument_token": 7}}

    class FakeClient:
        client = FakeKite()

    monkeypatch.setattr(dashboard_app, "connect_kite", lambda settings, access_token: FakeClient())
    monkeypatch.setattr(
        dashboard_app,
        "load_live_candles_from_client",
        lambda client, token, interval, lookback_days, include_current=False: frame,
    )

    class Settings:
        market_open = time(9, 15)

    rows, errors = dashboard_app.load_dynamic_watchlist_rows(
        Settings(),
        "token",
        WatchlistRecord("alice", "Morning", {"NSE:AAA": 7}),
        today,
    )

    assert errors == []
    assert rows.empty
