import pandas as pd

from app.market.indicators import adx, atr, ema, vwap


def test_ema_has_no_value_before_warmup():
    values = pd.Series([1, 2, 3, 4, 5])
    result = ema(values, 3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[-1] > result.iloc[-2]


def test_atr_is_non_negative():
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=4, freq="min"), "open": [10, 11, 10, 12], "high": [11, 12, 11, 13], "low": [9, 10, 9, 11], "close": [10, 11, 10, 12], "volume": [100] * 4})
    assert (atr(frame, 2).dropna() >= 0).all()


def test_zero_volume_flat_data_keeps_undefined_indicators_numeric():
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01 09:15", periods=60, freq="5min"),
        "open": [100.0] * 60,
        "high": [100.0] * 60,
        "low": [100.0] * 60,
        "close": [100.0] * 60,
        "volume": [0.0] * 60,
    })

    assert pd.api.types.is_float_dtype(vwap(frame))
    assert pd.api.types.is_float_dtype(adx(frame))
