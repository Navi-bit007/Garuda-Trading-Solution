import pandas as pd
import pytest

from app.strategy.base import NoSignal
from app.strategy.ema_200_close import Ema200CloseStrategy


def ema_frame(last_close: float = 101.0, rows: int = 200) -> pd.DataFrame:
    closes = [100.0] * (rows - 1) + [last_close]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=rows, freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * rows,
        }
    )


def test_ema_200_close_emits_buy_for_completed_close_above_ema():
    signal = Ema200CloseStrategy().generate_signal("AAA", ema_frame())

    assert signal.side == "BUY"
    assert signal.reason == "completed candle closed above EMA 200"
    assert signal.metadata["ema200"] < signal.price
    assert signal.metadata["candle_close"] == signal.price


def test_ema_200_close_requires_close_above_ema_not_just_warmup():
    with pytest.raises(NoSignal, match="did not close above EMA 200"):
        Ema200CloseStrategy().generate_signal("AAA", ema_frame(last_close=99.0))


def test_ema_200_close_requires_200_completed_candles():
    with pytest.raises(ValueError, match="not enough completed candles"):
        Ema200CloseStrategy().generate_signal("AAA", ema_frame(rows=199))
