from __future__ import annotations

import math


def calculate_quantity(equity: float, entry_price: float, stop_price: float, risk_fraction: float, deployment_fraction: float = 0.80) -> int:
    if min(equity, entry_price, risk_fraction, deployment_fraction) <= 0 or stop_price <= 0:
        raise ValueError("capital, prices, and risk values must be positive")
    risk_per_share = abs(entry_price - stop_price)
    if risk_per_share == 0:
        raise ValueError("entry and stop prices must differ")
    risk_quantity = math.floor(equity * risk_fraction / risk_per_share)
    deployment_quantity = math.floor(equity * deployment_fraction / entry_price)
    return max(0, min(risk_quantity, deployment_quantity))
