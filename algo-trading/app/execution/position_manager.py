from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config.constants import Side


@dataclass
class Position:
    symbol: str
    side: Side
    quantity: int
    entry_price: float
    stop_loss: float
    entry_time: datetime | None = None
    target_1: float | None = None
    target_2: float | None = None

    def unrealized_pnl(self, price: float) -> float:
        multiplier = 1 if self.side == Side.BUY else -1
        return multiplier * (price - self.entry_price) * self.quantity


class PositionManager:
    def __init__(self):
        self.positions: dict[str, Position] = {}

    def add(self, position: Position) -> None:
        if position.symbol in self.positions:
            raise ValueError(f"position already exists for {position.symbol}")
        self.positions[position.symbol] = position

    def remove(self, symbol: str) -> Position:
        return self.positions.pop(symbol)
