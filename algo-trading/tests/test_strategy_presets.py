from datetime import datetime

import pytest

from app.database.database import Database
from app.database.models import StrategyPresetRecord
from app.database.repository import Repository
from app.strategy.preset_builder import (
    DEFAULT_PARAMETERS,
    build_ema_preset_strategy,
    build_preset_strategy,
    normalize_ema_parameters,
    validate_parameters,
)


def test_custom_preset_builds_configurable_vwap_strategy():
    parameters = {**DEFAULT_PARAMETERS, "minimum_score": 90, "volume_multiplier": 2.0, "target_percent": 0.025}

    strategy = build_preset_strategy("Momentum confirmation", parameters)

    assert strategy.name == "Momentum confirmation"
    assert strategy.minimum_score == 90
    assert strategy.volume_multiplier == 2.0
    assert strategy.target_percent == 0.025


def test_preset_validation_rejects_overlapping_rsi_thresholds():
    parameters = {**DEFAULT_PARAMETERS, "rsi_buy_min": 45, "rsi_sell_max": 50}

    with pytest.raises(ValueError, match="RSI sell threshold"):
        validate_parameters(parameters)


def test_ema_only_preset_uses_fast_and_slow_periods():
    strategy = build_ema_preset_strategy("Fast EMA test", {"fast": 8, "slow": 21, "atr_period": 10, "stop_atr": 2.0})

    assert strategy.name == "Fast EMA test"
    assert strategy.fast == 8
    assert strategy.slow == 21
    assert strategy.minimum_score == 80


def test_ema_only_preset_accepts_entry_exit_configuration():
    parameters = normalize_ema_parameters(
        {"entry_ema": 200, "exit_ema": 9, "atr_period": 14, "stop_atr": 1.5, "min_gap_percent": 1.0}
    )

    assert parameters == {
        "entry_ema": 200,
        "exit_ema": 9,
        "atr_period": 14,
        "stop_atr": 1.5,
        "min_gap_percent": 1.0,
        "confirmation": "candle_close",
        "signal_mode": "crossover",
        "signal_window_candles": 3,
    }


def test_ema_only_validation_rejects_fast_period_after_slow_period():
    with pytest.raises(ValueError, match="fast EMA period"):
        normalize_ema_parameters({"fast": 50, "slow": 20})


def test_ema_only_preset_accepts_trend_mode_and_gap_filter():
    parameters = normalize_ema_parameters({"signal_mode": "trend", "min_gap_percent": 0.5})

    assert parameters["signal_mode"] == "trend"
    assert parameters["min_gap_percent"] == 0.5


def test_strategy_preset_repository_round_trips_json_parameters(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    created_at = datetime(2026, 1, 1, 9, 15)
    updated_at = datetime(2026, 1, 1, 9, 20)
    preset = StrategyPresetRecord(
        "Opening push",
        "vwap_ema_breakout",
        {**DEFAULT_PARAMETERS, "require_market_regime": False},
        created_at,
        updated_at,
    )

    repository.save_strategy_preset(preset)

    assert repository.load_strategy_presets() == [preset]
    repository.delete_strategy_preset("Opening push")
    assert repository.load_strategy_presets() == []
    database.close()