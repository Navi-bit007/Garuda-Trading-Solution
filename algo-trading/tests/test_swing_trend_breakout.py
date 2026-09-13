import pandas as pd
import pytest

import app.strategy.swing_trend_breakout as breakout_module
from app.strategy.base import NoSignal
from app.config.constants import TradingMode
from app.execution.swing_auto_trader import SwingAutoTrader
from app.strategy.swing_trend_breakout import SwingTrendBreakoutStrategy


def breakout_frame(rows: int = 221, close: float = 120.0, volume: float = 2_000.0) -> pd.DataFrame:
    closes = [100.0] * (rows - 1) + [close]
    highs = [105.0] * (rows - 1) + [close + 1]
    lows = [95.0] * (rows - 1) + [close - 1]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=rows, freq="D"),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1_000.0] * (rows - 1) + [volume],
        }
    )


def patch_indicators(monkeypatch, frame: pd.DataFrame) -> None:
    def fake_ema(values: pd.Series, period: int) -> pd.Series:
        level = {20: 110.0, 50: 105.0, 200: 100.0}[period]
        return pd.Series([level] * len(values), index=values.index)

    monkeypatch.setattr(breakout_module, "ema", fake_ema)
    monkeypatch.setattr(breakout_module, "atr", lambda candles, period: pd.Series([4.0] * len(candles), index=candles.index))
    monkeypatch.setattr(breakout_module, "rsi", lambda values, period: pd.Series([60.0] * len(values), index=values.index))
    monkeypatch.setattr(breakout_module, "adx", lambda candles, period: pd.Series([25.0] * len(candles), index=candles.index))


def test_breakout_evaluation_reports_qualified_candidate(monkeypatch):
    frame = breakout_frame(close=117.0)
    patch_indicators(monkeypatch, frame)

    evaluation = SwingTrendBreakoutStrategy().evaluate("NSE:AAA", frame)

    assert evaluation.qualified
    assert evaluation.state == "SWING_CANDIDATE"
    assert evaluation.classification == "A"
    assert evaluation.score == 100
    assert evaluation.breakout_level == 105.0
    assert evaluation.volume_ratio == 2.0
    assert evaluation.rejection_reasons == ()


def test_confirmation_requires_a_later_close_above_breakout_high(monkeypatch):
    frame = breakout_frame(close=117.0)
    patch_indicators(monkeypatch, frame)
    strategy = SwingTrendBreakoutStrategy()
    evaluation = strategy.evaluate("NSE:AAA", frame)

    assert strategy.confirm_entry(evaluation, frame) is None
    confirmed_frame = pd.concat(
        [
            frame,
            pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2025-08-11")],
                    "open": [106.0],
                    "high": [113.0],
                    "low": [105.0],
                    "close": [112.0],
                    "volume": [1_500.0],
                }
            ),
        ],
        ignore_index=True,
    )

    signal = strategy.confirm_entry(evaluation, confirmed_frame)

    assert signal is not None
    assert signal.side == "BUY"
    assert signal.price == 112.0
    assert signal.stop_loss == 106.0
    assert signal.target_1 == 124.0
    assert signal.metadata["strategy_state"] == "CONFIRMED_BUY"


def test_rejection_reasons_include_volume_and_extension(monkeypatch):
    frame = breakout_frame(close=120.0, volume=1_000.0)
    patch_indicators(monkeypatch, frame)
    evaluation = SwingTrendBreakoutStrategy().evaluate("NSE:AAA", frame)

    assert not evaluation.qualified
    assert evaluation.classification == "REJECTED"
    assert any("volume ratio" in reason for reason in evaluation.rejection_reasons)
    assert any("more than" in reason for reason in evaluation.rejection_reasons)
    with pytest.raises(NoSignal, match="volume ratio"):
        SwingTrendBreakoutStrategy().generate_signal("NSE:AAA", frame)


class StubKiteClient:
    def __init__(self):
        self.requests = []
        self.modifications = []
        self.cancellations = []
        self.instrument_rows = [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        self.requests.append(request)
        return f"ORDER-{len(self.requests)}"

    def modify_order(self, **request):
        self.modifications.append(request)
        return request["order_id"]

    def cancel_order(self, **request):
        self.cancellations.append(request)
        return request["order_id"]

    def instruments(self, exchange=None):
        return self.instrument_rows

    def order_history(self, order_id):
        return [{"status": "COMPLETE", "filled_quantity": 10}]

    def positions(self):
        return {"net": [{"tradingsymbol": "AAA", "quantity": 10}]}


def test_trader_waits_for_next_session_then_returns_confirmed_candidate(monkeypatch):
    first_frame = breakout_frame(close=117.0)
    next_frame = pd.concat(
        [
            first_frame,
            pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2025-08-11")],
                    "open": [106.0],
                    "high": [113.0],
                    "low": [105.0],
                    "close": [112.0],
                    "volume": [1_500.0],
                }
            ),
        ],
        ignore_index=True,
    )
    patch_indicators(monkeypatch, first_frame)
    trader = SwingAutoTrader(StubKiteClient(), TradingMode.LIVE, strategy_name="SWING_TREND_BREAKOUT")

    first_scan = trader.scan({"NSE:AAA": 1}, lambda token: first_frame)
    second_scan = trader.scan({"NSE:AAA": 1}, lambda token: next_frame)

    assert first_scan.candidates == ()
    assert len(first_scan.pending_candidates) == 1
    assert len(second_scan.candidates) == 1
    assert second_scan.candidates[0].signal is not None
    assert second_scan.candidates[0].signal.side == "BUY"
    assert second_scan.candidates[0].evaluation.state == "SWING_CANDIDATE"


def test_trader_scan_records_no_signal_reason_for_unqualified_breakout(monkeypatch):
    frame = breakout_frame(close=120.0, volume=1_000.0)
    patch_indicators(monkeypatch, frame)
    trader = SwingAutoTrader(StubKiteClient(), TradingMode.LIVE, strategy_name="SWING_TREND_BREAKOUT")

    result = trader.scan({"NSE:AAA": 1}, lambda token: frame)

    assert result.candidates == ()
    assert result.pending_candidates == ()
    assert len(result.no_signal) == 1
    symbol, reason = result.no_signal[0]
    assert symbol == "NSE:AAA"
    assert "volume ratio" in reason


def test_trader_scan_records_no_signal_when_breakout_does_not_confirm(monkeypatch):
    first_frame = breakout_frame(close=117.0)
    unconfirmed_next_frame = pd.concat(
        [
            first_frame,
            pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2025-08-11")],
                    "open": [100.0],
                    "high": [104.0],
                    "low": [98.0],
                    "close": [102.0],
                    "volume": [1_200.0],
                }
            ),
        ],
        ignore_index=True,
    )
    patch_indicators(monkeypatch, first_frame)
    trader = SwingAutoTrader(StubKiteClient(), TradingMode.LIVE, strategy_name="SWING_TREND_BREAKOUT")

    trader.scan({"NSE:AAA": 1}, lambda token: first_frame)
    second_scan = trader.scan({"NSE:AAA": 1}, lambda token: unconfirmed_next_frame)

    assert second_scan.candidates == ()
    no_signal_symbols = [symbol for symbol, _ in second_scan.no_signal]
    assert "NSE:AAA" in no_signal_symbols


