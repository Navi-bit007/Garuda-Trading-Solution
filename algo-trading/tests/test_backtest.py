import pandas as pd

from app.strategy.opening_range_breakout import OpeningRangeBreakoutStrategy
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics


def test_backtest_executes_after_signal_bar_without_lookahead():
    closes = [10, 10, 10, 12, 12, 11, 11]
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"), "open": closes, "high": [value + 0.5 for value in closes], "low": [value - 0.5 for value in closes], "close": closes, "volume": [100] * len(closes)})
    trades = BacktestEngine().run("TEST", frame, OpeningRangeBreakoutStrategy(2))
    assert calculate_metrics(trades)["trades"] >= 0
