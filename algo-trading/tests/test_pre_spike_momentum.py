from datetime import datetime

import pandas as pd
import pytest

from app.database.database import Database
from app.database.models import SignalRecord
from app.database.repository import Repository
from app.strategy.base import NoSignal
from app.strategy.pre_spike_momentum import PreSpikeMomentumConfig, PreSpikeMomentumStrategy


def pre_spike_frame(
    last_volume: float = 1000.0,
    last_close: float = 102.0,
    last_open: float = 100.0,
    previous_close: float = 100.0,
    current_session_price: float = 100.0,
    prior_day_high: float = 100.5,
    last_high: float | None = None,
) -> pd.DataFrame:
    rows = []
    sessions = pd.bdate_range("2025-12-01", periods=25)
    slots = pd.date_range("09:15", periods=75, freq="5min").time
    for session in sessions:
        session_price = current_session_price if session == sessions[-1] else 100.0
        for slot in slots:
            rows.append(
                {
                    "timestamp": pd.Timestamp.combine(session.date(), slot),
                    "open": session_price,
                    "high": session_price + 0.5,
                    "low": session_price - 0.5,
                    "close": session_price,
                    "volume": 100.0,
                }
            )
    rows[-2]["close"] = previous_close
    rows[-2]["open"] = previous_close
    rows[-2]["high"] = previous_close + 0.5
    rows[-2]["low"] = previous_close - 0.5
    previous_day_start = (len(sessions) - 2) * len(slots)
    for row in rows[previous_day_start : previous_day_start + len(slots)]:
        row["high"] = prior_day_high
    rows[-1].update(
        open=last_open,
        high=last_high if last_high is not None else max(103.0, last_close, last_open),
        low=min(99.5, last_close, last_open),
        close=last_close,
        volume=last_volume,
    )
    return pd.DataFrame(rows)


def test_high_rvol_and_bullish_expansion_emits_early_momentum():
    signal = PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame()).signal

    assert signal.action.value == "BUY"
    assert signal.signal_reasons[0] == "EARLY_MOMENTUM"
    assert signal.score >= 80
    assert signal.target_1 > signal.entry_price > signal.stop_loss
    assert signal.metadata["rvol"] >= 2.0
    assert signal.metadata["body_ratio"] > 0


def test_high_volume_without_price_expansion_is_rejected():
    with pytest.raises(NoSignal):
        PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame(last_close=100.1))


def test_price_expansion_without_volume_is_rejected():
    with pytest.raises(NoSignal):
        PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame(last_volume=100.0))


def test_custom_minimum_rvol_can_reject_default_signal():
    strategy = PreSpikeMomentumStrategy(config=PreSpikeMomentumConfig(minimum_rvol=20.0))

    with pytest.raises(NoSignal):
        strategy.evaluate("NSE:TEST", pre_spike_frame())


def test_bearish_candle_is_rejected_even_with_high_rvol_and_breakout_factors():
    with pytest.raises(NoSignal):
        PreSpikeMomentumStrategy().evaluate(
            "NSE:TEST",
            pre_spike_frame(last_open=103.0, last_close=102.0, last_volume=1000.0, last_high=106.0),
        )


@pytest.mark.parametrize("last_close", [99.0, 100.0])
def test_non_positive_five_minute_return_is_rejected(last_close: float):
    with pytest.raises(NoSignal):
        PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame(last_close=last_close))


def test_price_below_vwap_is_rejected():
    with pytest.raises(NoSignal):
        PreSpikeMomentumStrategy().evaluate(
            "NSE:TEST",
            pre_spike_frame(last_close=101.0, current_session_price=102.0),
        )


def test_intrabar_previous_day_breakout_without_bullish_close_is_not_confirmed():
    evaluation = PreSpikeMomentumStrategy().evaluate(
        "NSE:TEST",
        pre_spike_frame(last_close=102.0, last_high=106.0, prior_day_high=105.0),
    )

    assert evaluation.metrics.previous_day_breakout is False
    assert evaluation.metrics.twenty_day_breakout is False


def test_confirmed_previous_day_breakout_with_high_rvol_is_eligible():
    evaluation = PreSpikeMomentumStrategy().evaluate(
        "NSE:TEST",
        pre_spike_frame(last_close=106.0, last_high=106.5, prior_day_high=105.0),
    )

    assert evaluation.metrics.previous_day_breakout is True
    assert evaluation.signal.metadata["signal_type"] == "EARLY_MOMENTUM"


def test_confirmed_twenty_day_breakout_with_high_rvol_is_eligible():
    evaluation = PreSpikeMomentumStrategy().evaluate(
        "NSE:TEST",
        pre_spike_frame(last_close=102.0, last_high=102.5, prior_day_high=100.5),
    )

    assert evaluation.metrics.twenty_day_breakout is True


def test_previous_day_breakout_confirmation_is_independent():
    strategy = PreSpikeMomentumStrategy(
        config=PreSpikeMomentumConfig(require_previous_day_breakout=True)
    )

    evaluation = strategy.evaluate("NSE:TEST", pre_spike_frame())

    assert evaluation.metrics.previous_day_breakout is True


def test_twenty_day_breakout_confirmation_can_be_required_separately():
    frame = pre_spike_frame()
    frame.loc[22 * 75 : 23 * 75 - 1, "high"] = 104.0
    strategy = PreSpikeMomentumStrategy(
        config=PreSpikeMomentumConfig(require_twenty_day_breakout=True)
    )

    with pytest.raises(NoSignal):
        strategy.evaluate("NSE:TEST", frame)


def test_reason_describes_validated_bullish_conditions():
    signal = PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame()).signal

    assert signal.reason.startswith("Early bullish momentum:")
    assert "above VWAP" in signal.reason
    assert "High-volume bullish momentum" not in signal.reason


def test_metadata_contains_breakout_and_candle_quality_metrics():
    signal = PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame()).signal

    assert signal.metadata["previous_day_high"] == 100.5
    assert signal.metadata["previous_20_day_high"] == 100.5
    assert signal.metadata["volume_buildup_ratio"] > 1.0
    assert signal.metadata["range_compression"] is True
    assert signal.metadata["close_location"] > 0.7


def test_forming_candle_is_removed():
    frame = pre_spike_frame()
    current_timestamp = pd.Timestamp.now().floor("5min")
    frame.loc[len(frame)] = [current_timestamp, 100.0, 101.0, 99.0, 100.5, 500.0]

    completed = PreSpikeMomentumStrategy._completed_frame(frame)

    assert completed["timestamp"].iloc[-1] != current_timestamp


def test_insufficient_history_is_explicit():
    with pytest.raises(ValueError, match="not enough completed candles"):
        PreSpikeMomentumStrategy().evaluate("NSE:TEST", pre_spike_frame().iloc[:100])


def test_signal_metadata_and_strategy_filter_round_trip(tmp_path):
    database = Database(str(tmp_path / "signals.sqlite3"))
    database.initialize()
    repository = Repository(database)
    timestamp = datetime(2026, 1, 2, 10, 0)
    repository.save_signal(
        SignalRecord(
            user_id="alice",
            instrument_token=1,
            symbol="NSE:TEST",
            side="BUY",
            signal_timestamp=timestamp,
            price=102.0,
            vwap=100.5,
            ema20=101.0,
            stop_loss=99.0,
            reason="EARLY_MOMENTUM",
            strategy=PreSpikeMomentumStrategy.name,
            score=90,
            entry_price=102.0,
            target_1=104.0,
            target_2=105.0,
            metadata={"rvol": 4.0, "signal_type": "EARLY_MOMENTUM"},
        )
    )

    signals = repository.load_signals("alice", strategy=PreSpikeMomentumStrategy.name)

    assert len(signals) == 1
    assert signals[0].score == 90
    assert signals[0].metadata["rvol"] == 4.0
    assert repository.load_signals("alice", strategy="EMA_9_200_PROGRESSIVE") == []
    database.close()