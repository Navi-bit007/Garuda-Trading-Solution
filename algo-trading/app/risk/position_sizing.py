from __future__ import annotations

import math


def calculate_quantity(equity: float, entry_price: float, deployment_fraction: float = 0.80) -> int:
    if min(equity, entry_price, deployment_fraction) <= 0:
        raise ValueError("capital, price, and deployment fraction must be positive")
    deployment_quantity = math.floor(equity * deployment_fraction / entry_price)
    return max(0, deployment_quantity)
