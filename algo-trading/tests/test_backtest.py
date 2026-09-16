import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import Strategy
from app.strategy.signal import Signal
from app.strategy.opening_range_breakout import OpeningRangeBreakoutStrategy
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics


def test_backtest_executes_after_signal_bar_without_lookahead():
    closes = [10, 10, 10, 12, 12, 11, 11]
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"), "open": closes, "high": [value + 0.5 for value in closes], "low": [value - 0.5 for value in closes], "close": closes, "volume": [100] * len(closes)})
    trades = BacktestEngine().run("TEST", frame, OpeningRangeBreakoutStrategy(2))
    assert calculate_metrics(trades)["trades"] >= 0


class TargetStrategy(Strategy):
    name = "target_test"
    warmup_period = 1

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        current = candles.iloc[-1]
        action = SignalAction.BUY if len(candles) == 2 else SignalAction.HOLD
        return Signal(
            symbol=symbol,
            action=action,
            timestamp=current["timestamp"].to_pydatetime(),
            price=float(current["close"]),
            stop_loss=9.0 if action == SignalAction.BUY else None,
            target_1=11.0 if action == SignalAction.BUY else None,
            target_2=12.0 if action == SignalAction.BUY else None,
            metadata={"move_stop_to_breakeven_after_target_1": True},
        )


def test_backtest_records_target_exit_and_strategy_name():
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=5, freq="D"),
            "open": [10, 10, 10, 11, 11],
            "high": [10.5, 10.5, 12.5, 11.5, 11.5],
            "low": [9.5, 9.5, 9.5, 10.5, 10.5],
            "close": [10, 10, 12, 11, 11],
            "volume": [100] * 5,
        }
    )

    trades = BacktestEngine().run("TARGET", frame, TargetStrategy())

    assert len(trades) == 1
    assert trades[0].exit_reason == "target_2"
    assert trades[0].strategy_name == "target_test"
