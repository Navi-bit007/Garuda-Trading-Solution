from __future__ import annotations

from datetime import time as datetime_time

from app.config.constants import TradingMode

EDITABLE_SETTINGS = (
    "trading_mode",
    "initial_capital",
    "max_open_positions",
    "max_trades_per_day",
    "max_capital_deployment",
    "market_open",
    "entry_start",
    "entry_end",
    "force_exit",
    "trailing_atr_multiplier",
    "min_stop_improvement_pct",
    "swing_capital_limit",
    "swing_quantity_limit",
    "swing_trailing_atr_multiplier",
    "swing_max_open_positions",
    "intraday_capital_limit",
    "intraday_leverage_multiplier",
)


def settings_values(settings) -> dict:
    return {name: getattr(settings, name) for name in EDITABLE_SETTINGS}


def serialize_frontend_settings(values: dict) -> dict:
    serialized = {}
    for name, value in values.items():
        if isinstance(value, TradingMode):
            serialized[name] = value.value
        elif isinstance(value, datetime_time):
            serialized[name] = value.isoformat()
        else:
            serialized[name] = value
    return serialized


def deserialize_frontend_settings(values: dict) -> dict:
    overrides = {}
    for name, value in values.items():
        if name not in EDITABLE_SETTINGS:
            continue
        if name == "trading_mode":
            value = TradingMode(value)
        elif name in {"market_open", "entry_start", "entry_end", "force_exit"}:
            value = datetime_time.fromisoformat(value)
        overrides[name] = value
    return overrides


def apply_frontend_settings(base_settings, overrides: dict):
    if not overrides:
        return base_settings
    values = base_settings.model_dump() if hasattr(base_settings, "model_dump") else base_settings.dict()
    values.update(overrides)
    return type(base_settings)(**values)
