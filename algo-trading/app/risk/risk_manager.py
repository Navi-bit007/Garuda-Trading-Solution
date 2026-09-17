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
    default_leverage: float = 1.0

    def quantity(self, entry_price: float, leverage: float | None = None) -> int:
        return calculate_quantity(self.equity, entry_price, self.exposure.max_fraction, self._effective_leverage(leverage))

    def approve_entry(self, open_positions: int, current_exposure: float, entry_price: float, quantity: int | None = None, leverage: float | None = None) -> tuple[bool, str]:
        """Returns (approved, reason) -- reason is empty when approved, otherwise a message
        naming the exact numbers behind the rejection so it's clear which limit was hit and by
        how much, instead of a generic "entry limits rejected entry"."""
        effective_leverage = self._effective_leverage(leverage)
        quantity = self.quantity(entry_price, effective_leverage) if quantity is None else quantity
        if not self.limits.can_trade():
            return False, f"daily trade limit reached ({self.limits.trades}/{self.limits.max_trades} trades today)"
        if open_positions >= self.max_positions:
            return False, f"maximum open positions reached ({open_positions}/{self.max_positions})"
        if quantity <= 0:
            return False, f"calculated quantity is {quantity} at entry price {entry_price:.2f}"
        proposed_value = quantity * entry_price
        cap = self.exposure.capital * self.exposure.max_fraction * effective_leverage
        if not self.exposure.can_add(current_exposure, proposed_value, effective_leverage):
            leverage_note = f" × {effective_leverage:.1f}x leverage" if effective_leverage != 1.0 else ""
            available_balance = max(0.0, cap - current_exposure)
            affordable_shares = int(available_balance // entry_price) if entry_price > 0 else 0
            return False, (
                f"exposure cap exceeded at price ₹{entry_price:,.2f}: current ₹{current_exposure:,.0f} + proposed ₹{proposed_value:,.0f} "
                f"= ₹{current_exposure + proposed_value:,.0f} exceeds the ₹{cap:,.0f} cap "
                f"(₹{self.exposure.capital:,.0f} capital × {self.exposure.max_fraction:.0%} deployment{leverage_note}); "
                f"₹{available_balance:,.0f} of capacity remains (up to {affordable_shares} share(s) at this price)"
            )
        return True, ""

    def _effective_leverage(self, leverage: float | None) -> float:
        return leverage if leverage and leverage > 0 else self.default_leverage
