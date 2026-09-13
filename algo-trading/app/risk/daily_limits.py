from dataclasses import dataclass


@dataclass
class DailyLimits:
    starting_equity: float
    max_trades: int
    trades: int = 0
    realized_pnl: float = 0.0

    def can_trade(self) -> bool:
        return self.trades < self.max_trades

    def record_trade(self, pnl: float) -> None:
        self.trades += 1
        self.realized_pnl += pnl
