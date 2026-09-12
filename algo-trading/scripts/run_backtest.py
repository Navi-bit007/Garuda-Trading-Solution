from __future__ import annotations

import argparse

from app.strategy.ema_trend import EmaTrendStrategy
from backtest.data_loader import load_data
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics
from backtest.reports import write_report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a deterministic EMA backtest")
    parser.add_argument("--input", required=True)
    parser.add_argument("--symbol", default="TEST")
    parser.add_argument("--output", default="data/processed/backtest.json")
    args = parser.parse_args()
    trades = BacktestEngine().run(args.symbol, load_data(args.input), EmaTrendStrategy())
    metrics = calculate_metrics(trades)
    write_report(metrics, args.output)
    print(metrics)


if __name__ == "__main__":
    main()
