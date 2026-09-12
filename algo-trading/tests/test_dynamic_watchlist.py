from datetime import date, time
from math import isclose

import pandas as pd

from app.market.dynamic_watchlist import auto_refresh_slot, evaluate_dynamic_candle, filter_dynamic_watchlist, first_session_candles, session_candles


def completed_frame(current_open=100.0, current_close=101.0, current_volume=150.0, current_low=100.0):
    timestamps = pd.date_range("2026-09-07 09:15", periods=21, freq="5min")
    rows = [
        {
            "timestamp": timestamp,
            "open": 100.0,
            "high": 101.0,
            "low": 100.0,
            "close": 100.5,
            "volume": 100.0,
        }
        for timestamp in timestamps
    ]
    rows[-1].update(open=current_open, high=current_close, low=current_low, close=current_close, volume=current_volume)
    frame = pd.DataFrame(rows)
    frame["avg_volume_20"] = 50.0
    frame["vwap"] = 100.4
    return frame


def test_dynamic_filter_uses_low_near_open_and_current_price():
    result = filter_dynamic_watchlist({"NSE:AAA": 7}, {"NSE:AAA": completed_frame()})

    assert list(result["symbol"]) == ["NSE:AAA"]
    assert result.iloc[0]["low"] == 100.0
    assert result.iloc[0]["current_price"] == 100.5
    assert result.iloc[0]["first_candle_high"] == 101.0
    assert result.iloc[0]["close"] == 100.5
    assert result.iloc[0]["avg_volume_20"] == 50.0
    assert result.iloc[0]["vwap"] == 100.4


def test_dynamic_filter_requires_only_low_near_open_and_current_price():
    low_candle = completed_frame()
    low_candle.loc[0, "low"] = 99.8
    price_candle = completed_frame()
    price_candle.loc[0, "close"] = 99.9
    candles = {"NSE:LOW": low_candle, "NSE:PRICE": price_candle}

    result = filter_dynamic_watchlist(
        {symbol: index for index, symbol in enumerate(candles, start=1)},
        candles,
    )

    assert result.empty


def test_dynamic_filter_uses_live_current_price_without_close_or_vwap_filters():
    frame = completed_frame()
    frame.loc[0, "close"] = 99.0
    frame.loc[0, "vwap"] = 200.0

    result = filter_dynamic_watchlist(
        {"NSE:AAA": 7},
        {"NSE:AAA": frame},
        {"NSE:AAA": 101.0},
    )

    assert list(result["symbol"]) == ["NSE:AAA"]


def test_dynamic_filter_ignores_volume_and_vwap_confirmation():
    low_volume = completed_frame()
    low_volume.loc[0, "volume"] = 74.9
    below_vwap = completed_frame()
    below_vwap.loc[0, "vwap"] = 100.5

    result = filter_dynamic_watchlist(
        {"NSE:LOW_VOLUME": 1, "NSE:BELOW_VWAP": 2},
        {"NSE:LOW_VOLUME": low_volume, "NSE:BELOW_VWAP": below_vwap},
    )

    assert list(result["symbol"]) == ["NSE:LOW_VOLUME", "NSE:BELOW_VWAP"]


def test_first_session_candles_computes_opening_metrics_from_history():
    previous_days = []
    for day_index in range(20):
        day = pd.Timestamp("2026-08-10") + pd.Timedelta(days=day_index)
        previous_days.append(
            pd.DataFrame(
                [
                    {
                        "timestamp": day + pd.Timedelta(hours=9, minutes=15),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 100.0,
                        "close": 100.5,
                        "volume": 100.0 + day_index,
                    },
                    {
                        "timestamp": day + pd.Timedelta(hours=9, minutes=20),
                        "open": 100.0,
                        "high": 101.0,
                        "low": 100.0,
                        "close": 100.5,
                        "volume": 1000.0,
                    },
                ]
            )
        )
    current = completed_frame().drop(columns=["avg_volume_20", "vwap"])
    current.loc[0, "close"] = 100.6
    frame = pd.concat([*previous_days, current], ignore_index=True)

    result = first_session_candles(
        frame,
        date(2026, 9, 7),
        time(9, 15),
    )

    assert result.iloc[0]["avg_volume_20"] == 109.5
    assert isclose(result.iloc[0]["vwap"], 100.53333333333333)


def test_session_candles_excludes_previous_session():
    frame = pd.concat(
        [
            completed_frame().assign(timestamp=lambda values: values["timestamp"] - pd.Timedelta(days=1)),
            completed_frame(),
        ],
        ignore_index=True,
    )

    result = session_candles(frame, date(2026, 9, 7), time(9, 15))

    assert len(result) == 21
    assert result.iloc[0]["timestamp"].date() == date(2026, 9, 7)


def test_dynamic_filter_does_not_require_current_session_warmup():
    previous = completed_frame().assign(timestamp=lambda values: values["timestamp"] - pd.Timedelta(days=1))
    previous.loc[previous.index[-1], ["close", "volume"]] = [100.5, 100.0]
    current = completed_frame().iloc[[-1]].copy()
    current["timestamp"] = pd.Timestamp("2026-09-07 09:15")

    result = first_session_candles(
        pd.concat([previous, current], ignore_index=True),
        date(2026, 9, 7),
        time(9, 15),
    )

    assert len(result) == 1
    assert result.iloc[0]["timestamp"].date() == date(2026, 9, 7)
    assert list(filter_dynamic_watchlist({"NSE:AAA": 7}, {"NSE:AAA": result})["symbol"]) == ["NSE:AAA"]


def test_first_session_candles_requires_exact_market_open_candle():
    frame = completed_frame()
    frame["timestamp"] = frame["timestamp"] + pd.Timedelta(minutes=5)

    result = first_session_candles(frame, date(2026, 9, 7), time(9, 15))

    assert result.empty


def test_off_hours_refresh_keeps_the_first_0915_candle():
    previous = completed_frame().assign(timestamp=lambda values: values["timestamp"] - pd.Timedelta(days=1))
    current = completed_frame()
    current.loc[current.index[-1], ["low", "close"]] = [90.0, 120.0]

    result = first_session_candles(
        pd.concat([previous, current], ignore_index=True),
        date(2026, 9, 7),
        time(9, 15),
    )
    filtered = filter_dynamic_watchlist({"NSE:AAA": 7}, {"NSE:AAA": result})

    assert result.iloc[0]["timestamp"] == pd.Timestamp("2026-09-07 09:15")
    assert filtered.iloc[0]["candle_time"] == pd.Timestamp("2026-09-07 09:15")
    assert filtered.iloc[0]["current_price"] == 100.5


def test_breakout_condition_waits_until_a_later_candle_is_available():
    first = completed_frame().iloc[[0]].copy()
    first.loc[first.index[0], "high"] = 102.0
    later = completed_frame().iloc[[1]].copy()

    assert evaluate_dynamic_candle(first, current_price=101.0, require_breakout=True) is not None
    assert evaluate_dynamic_candle(pd.concat([first, later]), current_price=101.5, require_breakout=True) is not None
    assert evaluate_dynamic_candle(pd.concat([first, later]), current_price=102.1, require_breakout=True) is not None


def test_auto_refresh_slot_covers_only_first_four_five_minute_candles():
    assert auto_refresh_slot(pd.Timestamp("2026-09-07 09:14:59", tz="Asia/Kolkata"), time(9, 15)) is None
    assert auto_refresh_slot(pd.Timestamp("2026-09-07 09:15", tz="Asia/Kolkata"), time(9, 15)) == 0
    assert auto_refresh_slot(pd.Timestamp("2026-09-07 09:24:59", tz="Asia/Kolkata"), time(9, 15)) == 1
    assert auto_refresh_slot(pd.Timestamp("2026-09-07 09:25", tz="Asia/Kolkata"), time(9, 15)) == 2
    assert auto_refresh_slot(pd.Timestamp("2026-09-07 09:30", tz="Asia/Kolkata"), time(9, 15)) is None
