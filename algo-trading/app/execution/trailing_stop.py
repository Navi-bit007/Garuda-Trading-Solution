from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TrailingStop:
    entry_price: float
    initial_stop: float
    atr_multiplier: float
    side: str

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        self.stop = self.initial_stop

    def update(self, price: float, atr_value: float) -> float:
        if atr_value < 0:
            raise ValueError("ATR cannot be negative")
        candidate = price - self.atr_multiplier * atr_value if self.side == "BUY" else price + self.atr_multiplier * atr_value
        self.stop = max(self.stop, candidate) if self.side == "BUY" else min(self.stop, candidate)
        return self.stop
