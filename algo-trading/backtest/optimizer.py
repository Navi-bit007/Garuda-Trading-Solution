from __future__ import annotations

from collections.abc import Iterable

from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics


def choose_best(engine: BacktestEngine, symbol: str, candles, strategies: Iterable) -> tuple[object, dict]:
    candidates = [(strategy, calculate_metrics(engine.run(symbol, candles, strategy))) for strategy in strategies]
    return max(candidates, key=lambda item: (item[1]["net_pnl"], -item[1]["max_drawdown"]))
