from datetime import time

from app.config.constants import TradingMode
from app.config.frontend_settings import (
    EDITABLE_SETTINGS,
    apply_frontend_settings,
    deserialize_frontend_settings,
    serialize_frontend_settings,
    settings_values,
)
from app.config.settings import Settings


def test_settings_values_reads_only_the_editable_fields():
    settings = Settings()

    values = settings_values(settings)

    assert set(values) == set(EDITABLE_SETTINGS)
    assert values["trading_mode"] == settings.trading_mode


def test_serialize_and_deserialize_round_trip_editable_settings():
    values = {
        "trading_mode": TradingMode.LIVE,
        "initial_capital": 250_000.0,
        "market_open": time(9, 10),
    }

    serialized = serialize_frontend_settings(values)
    restored = deserialize_frontend_settings(serialized)

    assert restored == values


def test_deserialize_ignores_fields_outside_the_editable_allowlist():
    restored = deserialize_frontend_settings({"kite_api_key": "should-be-ignored", "initial_capital": 1000.0})

    assert restored == {"initial_capital": 1000.0}


def test_apply_frontend_settings_overrides_only_the_given_fields():
    base = Settings()

    updated = apply_frontend_settings(base, {"initial_capital": 999.0})

    assert updated.initial_capital == 999.0
    assert updated.trading_mode == base.trading_mode


def test_apply_frontend_settings_returns_the_same_object_with_no_overrides():
    base = Settings()

    assert apply_frontend_settings(base, {}) is base
