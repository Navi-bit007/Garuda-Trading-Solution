from __future__ import annotations

from typing import Any

from app.strategy.ema_trend import EmaTrendStrategy
from app.strategy.vwap_ema_breakout import VwapEmaBreakoutStrategy


CURRENT_STRATEGY_TYPE = "vwap_ema_breakout"
EMA_ONLY_STRATEGY_TYPE = "ema_trend"
EMA_ONLY_STRATEGY_LABEL = "EMA entry/exit (simple)"

DEFAULT_PARAMETERS: dict[str, Any] = {
    "atr_period": 14,
    "breakout_period": 20,
    "volume_multiplier": 1.5,
    "rsi_buy_min": 55.0,
    "rsi_sell_max": 45.0,
    "adx_min": 20.0,
    "target_percent": 0.02,
    "target_3_percent": 0.03,
    "atr_horizon": 5,
    "stop_atr": 1.5,
    "minimum_score": 80,
    "require_confirmation": True,
    "require_market_regime": True,
}

EMA_ONLY_DEFAULT_PARAMETERS: dict[str, Any] = {
    "entry_ema": 200,
    "exit_ema": 9,
    "atr_period": 14,
    "stop_atr": 1.5,
    "min_gap_percent": 1.0,
    "confirmation": "candle_close",
    "signal_mode": "crossover",
    "signal_window_candles": 3,
}


def validate_preset_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise ValueError("strategy name is required")
    if len(normalized) > 60:
        raise ValueError("strategy name must be 60 characters or fewer")
    return normalized


def normalize_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    values = {**DEFAULT_PARAMETERS, **parameters}
    integer_fields = ("atr_period", "breakout_period", "atr_horizon", "minimum_score")
    float_fields = ("volume_multiplier", "rsi_buy_min", "rsi_sell_max", "adx_min", "target_percent", "target_3_percent", "stop_atr")
    boolean_fields = ("require_confirmation", "require_market_regime")
    try:
        for field in integer_fields:
            values[field] = int(values[field])
        for field in float_fields:
            values[field] = float(values[field])
        for field in boolean_fields:
            values[field] = bool(values[field])
    except (TypeError, ValueError) as error:
        raise ValueError("strategy parameters must use valid numbers and switches") from error
    unknown = set(values) - set(DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError(f"unknown strategy parameters: {', '.join(sorted(unknown))}")
    return values


def validate_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    values = normalize_parameters(parameters)
    VwapEmaBreakoutStrategy(**values)
    return values


def normalize_ema_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    legacy_parameters = "fast" in parameters or "slow" in parameters
    if legacy_parameters:
        if "fast" not in parameters or "slow" not in parameters:
            raise ValueError("fast and slow EMA periods must be provided together")
        if int(parameters["fast"]) >= int(parameters["slow"]):
            raise ValueError("fast EMA period must be less than slow EMA period")
        parameters = {
            **parameters,
            "entry_ema": parameters["slow"],
            "exit_ema": parameters["fast"],
        }
        parameters.pop("fast", None)
        parameters.pop("slow", None)
        parameters.setdefault("confirmation", "candle_close")
        parameters.setdefault("min_gap_percent", 0.0)
        parameters.setdefault("signal_mode", "trend")
    values = {**EMA_ONLY_DEFAULT_PARAMETERS, **parameters}
    unknown = set(values) - set(EMA_ONLY_DEFAULT_PARAMETERS)
    if unknown:
        raise ValueError(f"unknown EMA strategy parameters: {', '.join(sorted(unknown))}")
    try:
        values["entry_ema"] = int(values["entry_ema"])
        values["exit_ema"] = int(values["exit_ema"])
        values["atr_period"] = int(values["atr_period"])
        values["stop_atr"] = float(values["stop_atr"])
        values["min_gap_percent"] = float(values["min_gap_percent"])
        values["signal_window_candles"] = int(values["signal_window_candles"])
    except (TypeError, ValueError) as error:
        raise ValueError("EMA strategy parameters must use valid numbers") from error
    EmaTrendStrategy(**values)
    return values


def build_preset_strategy(name: str, parameters: dict[str, Any]) -> VwapEmaBreakoutStrategy:
    normalized_name = validate_preset_name(name)
    values = validate_parameters(parameters)
    return VwapEmaBreakoutStrategy(strategy_name=normalized_name, **values)


def build_ema_preset_strategy(name: str, parameters: dict[str, Any]) -> EmaTrendStrategy:
    normalized_name = validate_preset_name(name)
    values = normalize_ema_parameters(parameters)
    return EmaTrendStrategy(strategy_name=normalized_name, **values)


def validate_strategy_parameters(strategy_type: str, parameters: dict[str, Any]) -> dict[str, Any]:
    if strategy_type == CURRENT_STRATEGY_TYPE:
        return validate_parameters(parameters)
    if strategy_type == EMA_ONLY_STRATEGY_TYPE:
        return normalize_ema_parameters(parameters)
    raise ValueError(f"unknown strategy type: {strategy_type}")


def current_strategy() -> VwapEmaBreakoutStrategy:
    return VwapEmaBreakoutStrategy()