from dataclasses import dataclass


@dataclass
class DailyLimits:
    starting_equity: float
    max_trades: int
    trades: int = 0
    realized_pnl: float = 0.0

    def can_trade(self) -> bool:
        """True while fewer than max_trades entries have been taken today.

        Checked before a new entry is submitted -- a position closing later never gives back
        headroom, so opening N positions in one day always counts against this cap even once
        some of them have since closed.
        """
        return self.trades < self.max_trades

    def record_entry(self) -> None:
        """Call once a new position is actually opened -- this, not record_trade, is what the
        daily trade cap (max_trades_per_day) counts against."""
        self.trades += 1

    def record_trade(self, pnl: float) -> None:
        """Call once a position closes, to fold its realized P&L into today's total. Does not
        touch the entry count: that's tracked by record_entry() at entry time, so closing a
        position never appears to free up daily-limit headroom for a new one."""
        self.realized_pnl += pnl
