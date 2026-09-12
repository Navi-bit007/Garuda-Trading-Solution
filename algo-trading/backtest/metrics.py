from __future__ import annotations

from collections.abc import Iterable


def calculate_metrics(trades: Iterable) -> dict[str, float]:
    pnls = [float(trade.pnl) for trade in trades]
    if not pnls:
        return {"trades": 0, "net_pnl": 0.0, "win_rate": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0}
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [abs(pnl) for pnl in pnls if pnl < 0]
    return {"trades": len(pnls), "net_pnl": sum(pnls), "win_rate": len(wins) / len(pnls), "profit_factor": sum(wins) / sum(losses) if losses else float("inf"), "max_drawdown": drawdown}
