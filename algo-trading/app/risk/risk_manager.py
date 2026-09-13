from __future__ import annotations

from dataclasses import dataclass

from app.risk.daily_limits import DailyLimits
from app.risk.exposure import Exposure
from app.risk.position_sizing import calculate_quantity


@dataclass
class RiskManager:
    equity: float
    max_positions: int
    limits: DailyLimits
    exposure: Exposure

    def quantity(self, entry_price: float) -> int:
        return calculate_quantity(self.equity, entry_price, self.exposure.max_fraction)

    def approve_entry(self, open_positions: int, current_exposure: float, entry_price: float, quantity: int | None = None) -> bool:
        quantity = self.quantity(entry_price) if quantity is None else quantity
        return self.limits.can_trade() and open_positions < self.max_positions and quantity > 0 and self.exposure.can_add(current_exposure, quantity * entry_price)
