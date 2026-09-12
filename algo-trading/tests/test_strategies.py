import pandas as pd
import pytest

import app.strategy.ema_trend as ema_trend_module
from app.config.constants import SignalAction
from app.strategy.base import NoSignal
import app.strategy.ema_200_20_confirmation as ema_confirmation_module
from app.strategy.ema_200_20_confirmation import Ema20020ConfirmationStrategy
from app.strategy.ema_trend import EmaTrendStrategy
from app.strategy.high_conviction_long import HighConvictionLongStrategy
from app.strategy.previous_day_high_breakout import PreviousDayHighBreakoutStrategy
from app.strategy.vwap_ema_breakout import MarketRegimeContext, TimeframeConfirmation, VwapEmaBreakoutStrategy


def make_breakout_frame(direction: str = "bullish") -> pd.DataFrame:
    closes = [100.0 + index * 0.25 if direction == "bullish" else 200.0 - index * 0.25 for index in range(201)]
    opens = closes.copy()
    highs = [value + 2.0 for value in closes]
    lows = [value - 2.0 for value in closes]
    volumes = [1000.0] * 201
    if direction == "bullish":
        closes[-1], opens[-1], highs[-1], lows[-1], volumes[-1] = 153.0, 153.0, 155.0, 151.0, 2000.0
    else:
        closes[-1], opens[-1], highs[-1], lows[-1], volumes[-1] = 147.0, 147.0, 149.0, 145.0, 2000.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=201, freq="5min"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def previous_day_high_frame(current_close: float = 106.0) -> pd.DataFrame:
    previous_day = pd.date_range("2026-01-05 09:15", periods=3, freq="5min")
    current_day = pd.date_range("2026-01-06 09:15", periods=2, freq="5min")
    timestamps = previous_day.append(current_day)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [100.0, 101.0, 102.0, 104.0, 105.0],
            "high": [103.0, 104.0, 105.0, 105.5, current_close],
            "low": [99.0, 100.0, 101.0, 103.0, 104.0],
            "close": [101.0, 102.0, 104.0, 105.0, current_close],
            "volume": [1000.0] * 5,
        }
    )


def test_previous_day_high_breakout_emits_buy_on_crossing_candle():
    signal = PreviousDayHighBreakoutStrategy().generate_signal("AAA", previous_day_high_frame())

    assert signal.side == "BUY"
    assert signal.price == 106.0
    assert signal.stop_loss == 105.0
    assert signal.metadata["previous_day_high"] == 105.0


def test_previous_day_high_breakout_requires_a_cross_not_just_price_above_high():
    frame = previous_day_high_frame()
    frame.loc[3, "close"] = 106.0

    with pytest.raises(NoSignal, match="did not cross"):
        PreviousDayHighBreakoutStrategy().generate_signal("AAA", frame)


def neutral_regime() -> MarketRegimeContext:
    return MarketRegimeContext("NIFTY 50", pd.Timestamp("2026-01-01 14:10"), 100.0, 100.0, 100.0, 100.0)


def confirmation(direction: str) -> TimeframeConfirmation:
    return TimeframeConfirmation(
        pd.Timestamp("2026-01-01 14:10"),
        150.0 if direction == "bullish" else 150.0,
        145.0 if direction == "bullish" else 155.0,
        140.0 if direction == "bullish" else 160.0,
    )


def test_ema_strategy_generates_signal_from_completed_bars():
    closes = [100] * 55 + list(range(100, 125))
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"), "open": closes, "high": [value + 1 for value in closes], "low": [value - 1 for value in closes], "close": closes, "volume": [1000] * len(closes)})
    signal = EmaTrendStrategy(fast=3, slow=5, atr_period=3).generate_signal("TEST", frame)
    assert signal.action in (SignalAction.BUY, SignalAction.HOLD)
    assert signal.timestamp == frame["timestamp"].iloc[-1].to_pydatetime()
    assert signal.score in (0, 100)


def test_ema_trend_mode_signals_when_fast_ema_leads():
    closes = [100] * 55 + list(range(100, 125))
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"), "open": closes, "high": [value + 1 for value in closes], "low": [value - 1 for value in closes], "close": closes, "volume": [1000] * len(closes)})

    signal = EmaTrendStrategy(fast=3, slow=5, atr_period=3, signal_mode="trend").generate_signal("TEST", frame)

    assert signal.action == SignalAction.BUY
    assert signal.reason == "bullish EMA trend alignment"


def test_ema_minimum_gap_can_filter_a_crossover():
    closes = [100] * 55 + list(range(100, 125))
    frame = pd.DataFrame({"timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"), "open": closes, "high": [value + 1 for value in closes], "low": [value - 1 for value in closes], "close": closes, "volume": [1000] * len(closes)})

    signal = EmaTrendStrategy(fast=3, slow=5, atr_period=3, min_gap_percent=10).generate_signal("TEST", frame)

    assert signal.action == SignalAction.HOLD


def make_ema_entry_exit_frame(direction: str) -> pd.DataFrame:
    tail = list(range(100, 141)) if direction == "bullish" else list(range(100, 59, -1))
    closes = [100] * 200 + tail
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


def make_ema_reversal_frame() -> pd.DataFrame:
    closes = [100.0] * 214 + [99.0, 101.0, 102.0, 103.0, 104.0]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


def test_ema_entry_exit_defaults_generate_buy_and_sell_from_candle_close():
    strategy = EmaTrendStrategy(signal_mode="trend")

    bullish = strategy.generate_signal("BULL", make_ema_entry_exit_frame("bullish"))
    bearish = strategy.generate_signal("BEAR", make_ema_entry_exit_frame("bearish"))

    assert (strategy.entry_ema, strategy.exit_ema) == (200, 9)
    assert strategy.atr_period == 14
    assert strategy.stop_atr == 1.5
    assert strategy.min_gap_percent == 1.0
    assert strategy.confirmation == "candle_close"
    assert strategy.signal_mode == "trend"
    assert bullish.action == SignalAction.BUY
    assert bearish.action == SignalAction.SELL
    assert "candle-close" in bullish.reason
    assert "candle-close" in bearish.reason


def test_ema_entry_exit_default_keeps_crossover_signal_fresh_for_three_candles():
    frame = make_ema_reversal_frame()
    strategy = EmaTrendStrategy(min_gap_percent=0)

    signals = [strategy.generate_signal("REVERSAL", frame.iloc[:length]) for length in (216, 217, 218, 219)]

    assert strategy.signal_mode == "crossover"
    assert [signal.action for signal in signals] == [SignalAction.BUY, SignalAction.BUY, SignalAction.BUY, SignalAction.HOLD]
    assert signals[0].timestamp == signals[1].timestamp == signals[2].timestamp
    assert "2 candle(s) ago" in signals[2].reason


def high_conviction_frame(last_close: float = 102.0, last_volume: float = 1600.0) -> pd.DataFrame:
    closes = [100.0] * 220
    opens = [99.8] * 220
    highs = [100.5] * 220
    lows = [99.5] * 220
    volumes = [1000.0] * 220
    closes[-1] = last_close
    opens[-1] = 100.0
    highs[-1] = 102.5
    lows[-1] = 99.5
    volumes[-1] = last_volume
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def test_high_conviction_long_requires_all_hard_gates_and_sets_exit_plan():
    evaluation = HighConvictionLongStrategy().evaluate("HIGH", high_conviction_frame())

    assert evaluation.signal.action == SignalAction.BUY
    assert evaluation.signal.score == 95
    assert evaluation.signal.target_1 == pytest.approx(103.02)
    assert evaluation.signal.target_2 == pytest.approx(104.04)
    assert evaluation.signal.stop_loss < evaluation.signal.entry_price
    assert evaluation.conditions["breakout_20_high"] is True
    assert evaluation.conditions["relative_volume_1_5"] is True
    assert evaluation.conditions["close_above_vwap"] is True
    assert evaluation.signal.metadata["move_stop_to_breakeven_after_target_1"] is True


def test_high_conviction_long_rejects_close_that_does_not_break_previous_high():
    evaluation = HighConvictionLongStrategy().evaluate("NO_BREAKOUT", high_conviction_frame(last_close=100.4))

    assert evaluation.signal.action == SignalAction.HOLD
    assert evaluation.conditions["breakout_20_high"] is False


def test_high_conviction_long_rejects_low_relative_volume():
    evaluation = HighConvictionLongStrategy().evaluate("LOW_VOLUME", high_conviction_frame(last_volume=1499.0))

    assert evaluation.signal.action == SignalAction.HOLD
    assert evaluation.conditions["relative_volume_1_5"] is False


def test_ema_entry_exit_buy_uses_close_crossing_entry_ema(monkeypatch):
    closes = [100.0] * 214 + [99.0, 101.0]
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )

    def fake_ema(values: pd.Series, period: int) -> pd.Series:
        level = 100.0 if period == 200 else 101.0
        return pd.Series([level] * len(values), index=values.index)

    monkeypatch.setattr(ema_trend_module, "ema", fake_ema)
    monkeypatch.setattr(ema_trend_module, "atr", lambda candles, period: pd.Series([1.0] * len(candles)))

    signal = EmaTrendStrategy(min_gap_percent=0).generate_signal("BUY", frame)

    assert signal.action == SignalAction.BUY


def test_ema_entry_exit_sell_uses_close_crossing_exit_ema(monkeypatch):
    closes = [100.0] * 214 + [101.0, 98.0]
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )

    def fake_ema(values: pd.Series, period: int) -> pd.Series:
        level = 100.0 if period == 200 else 99.0
        return pd.Series([level] * len(values), index=values.index)

    monkeypatch.setattr(ema_trend_module, "ema", fake_ema)
    monkeypatch.setattr(ema_trend_module, "atr", lambda candles, period: pd.Series([1.0] * len(candles)))

    signal = EmaTrendStrategy(min_gap_percent=0).generate_signal("SELL", frame)

    assert signal.action == SignalAction.SELL


def make_ema_confirmation_frame(direction: str, confirmed: bool = True) -> pd.DataFrame:
    closes = [100.0] * 202
    opens = closes.copy()
    if direction == "buy":
        closes[200] = 101.0
        opens[201] = 101.0
        closes[201] = 102.0 if confirmed else 101.0
    else:
        closes[200] = 99.0
        opens[201] = 99.0
        closes[201] = 98.0 if confirmed else 99.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": opens,
            "high": [max(open_price, close) + 1 for open_price, close in zip(opens, closes)],
            "low": [min(open_price, close) - 1 for open_price, close in zip(opens, closes)],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def test_ema_200_20_confirmation_waits_for_bullish_next_candle(monkeypatch):
    monkeypatch.setattr(ema_confirmation_module, "ema", lambda values, period: pd.Series([100.0] * len(values)))

    signal = Ema20020ConfirmationStrategy().generate_signal("BUY", make_ema_confirmation_frame("buy"))

    assert signal.action == SignalAction.BUY
    assert signal.price == 102.0
    assert signal.timestamp == pd.Timestamp("2026-01-02 02:00").to_pydatetime()


def test_ema_200_20_confirmation_waits_for_bearish_next_candle(monkeypatch):
    monkeypatch.setattr(ema_confirmation_module, "ema", lambda values, period: pd.Series([100.0] * len(values)))

    signal = Ema20020ConfirmationStrategy().generate_signal("SELL", make_ema_confirmation_frame("sell"))

    assert signal.action == SignalAction.SELL
    assert signal.price == 98.0


def test_ema_200_20_confirmation_expires_without_required_confirmation(monkeypatch):
    monkeypatch.setattr(ema_confirmation_module, "ema", lambda values, period: pd.Series([100.0] * len(values)))

    with pytest.raises(NoSignal):
        Ema20020ConfirmationStrategy().generate_signal("NO_SIGNAL", make_ema_confirmation_frame("buy", confirmed=False))


def test_ema_entry_exit_emits_only_one_signal_for_a_bullish_transition():
    frame = make_ema_reversal_frame()
    strategy = EmaTrendStrategy(min_gap_percent=0)

    crossover = strategy.generate_signal("REVERSAL", frame.iloc[:216])
    following_bullish_candle = strategy.generate_signal("REVERSAL", frame)

    assert crossover.action == SignalAction.BUY
    assert "crossover" in crossover.reason
    assert following_bullish_candle.action == SignalAction.HOLD


def test_ema_entry_exit_gap_filter_blocks_small_separation():
    signal = EmaTrendStrategy(entry_ema=200, exit_ema=9, min_gap_percent=50).generate_signal(
        "TEST", make_ema_entry_exit_frame("bullish")
    )

    assert signal.action == SignalAction.HOLD


def test_vwap_ema_breakout_generates_buy_signal_when_all_conditions_pass():
    strategy = VwapEmaBreakoutStrategy()
    evaluation = strategy.evaluate("TEST", make_breakout_frame(), neutral_regime(), confirmation("bullish"))

    assert evaluation.signal.action == SignalAction.BUY
    assert evaluation.relative_volume == 2.0
    assert evaluation.signal.score == 100
    assert evaluation.signal.target_2 == evaluation.signal.price * 1.02
    assert all(evaluation.conditions[name] for name in (
        "price_above_vwap",
        "ema20_above_ema50",
        "breakout_20_high",
        "relative_volume_1_5",
        "rsi_at_least_55",
        "adx_at_least_20",
        "confirmation_15m_bullish",
        "nifty_not_strongly_bearish",
    ))


def test_vwap_ema_breakout_generates_sell_signal_when_all_opposite_conditions_pass():
    evaluation = VwapEmaBreakoutStrategy().evaluate("TEST", make_breakout_frame("bearish"), neutral_regime(), confirmation("bearish"))

    assert evaluation.signal.action == SignalAction.SELL
    assert evaluation.signal.stop_loss > evaluation.signal.price
    assert evaluation.signal.score == 100
    assert evaluation.conditions["breakdown_20_low"]
    assert evaluation.conditions["nifty_not_strongly_bullish"]


def test_vwap_ema_breakout_requires_strict_previous_20_candle_break():
    frame = make_breakout_frame()
    frame.loc[180:199, "high"] = 155.0
    frame.loc[200, "high"] = 155.0

    evaluation = VwapEmaBreakoutStrategy().evaluate("TEST", frame, neutral_regime(), confirmation("bullish"))

    assert evaluation.signal.action == SignalAction.BUY
    assert evaluation.signal.score == 90
    assert not evaluation.conditions["breakout_20_high"]


def test_vwap_ema_breakout_requires_volume_above_one_point_five_times_average():
    frame = make_breakout_frame()
    frame.loc[200, "volume"] = 1499.0

    evaluation = VwapEmaBreakoutStrategy().evaluate("TEST", frame, neutral_regime(), confirmation("bullish"))

    assert evaluation.signal.action == SignalAction.BUY
    assert evaluation.signal.score == 90
    assert not evaluation.conditions["relative_volume_1_5"]


def test_vwap_ema_breakout_scores_long_lower_in_strongly_bearish_regime():
    bearish_regime = MarketRegimeContext("NIFTY 50", pd.Timestamp("2026-01-01 14:10"), 98.0, 100.0, 99.0, 101.0)

    evaluation = VwapEmaBreakoutStrategy().evaluate("TEST", make_breakout_frame(), bearish_regime, confirmation("bullish"))

    assert evaluation.signal.action == SignalAction.BUY
    assert evaluation.signal.score == 90
    assert not evaluation.conditions["nifty_not_strongly_bearish"]


def test_vwap_ema_breakout_scores_short_lower_in_strongly_bullish_regime():
    bullish_regime = MarketRegimeContext("NIFTY 50", pd.Timestamp("2026-01-01 14:10"), 102.0, 100.0, 101.0, 99.0)

    evaluation = VwapEmaBreakoutStrategy().evaluate("TEST", make_breakout_frame("bearish"), bullish_regime, confirmation("bearish"))

    assert evaluation.signal.action == SignalAction.SELL
    assert evaluation.signal.score == 90
    assert not evaluation.conditions["nifty_not_strongly_bullish"]


def test_vwap_ema_breakout_scores_missing_nifty_regime_context_lower():
    signal = VwapEmaBreakoutStrategy().generate_signal("TEST", make_breakout_frame(), confirmation=confirmation("bullish"))

    assert signal.action == SignalAction.BUY
    assert signal.score == 90
    assert "nifty not strongly bearish" not in signal.signal_reasons


def test_vwap_ema_breakout_requires_warmup_data():
    with pytest.raises(ValueError, match="not enough completed candles"):
        VwapEmaBreakoutStrategy().generate_signal("TEST", make_breakout_frame().iloc[:200], neutral_regime(), confirmation("bullish"))
