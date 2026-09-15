from __future__ import annotations

import math


def calculate_quantity(equity: float, entry_price: float, deployment_fraction: float = 0.80, leverage: float = 1.0) -> int:
    if min(equity, entry_price, deployment_fraction, leverage) <= 0:
        raise ValueError("capital, price, deployment fraction, and leverage must be positive")
    deployment_quantity = math.floor(equity * deployment_fraction * leverage / entry_price)
    return max(0, deployment_quantity)
