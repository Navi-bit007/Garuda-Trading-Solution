from dataclasses import dataclass


@dataclass
class DailyLimits:
    starting_equity: float
    max_loss_fraction: float
    max_trades: int
    trades: int = 0
    realized_pnl: float = 0.0

    @property
    def loss_limit(self) -> float:
        return self.starting_equity * self.max_loss_fraction

    def can_trade(self) -> bool:
        return self.trades < self.max_trades and self.realized_pnl > -self.loss_limit

    def record_trade(self, pnl: float) -> None:
        self.trades += 1
        self.realized_pnl += pnl
