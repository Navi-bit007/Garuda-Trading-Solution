from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.ema_9_200_progressive import Ema9200ProgressiveStrategy
from app.strategy.swing_trend_breakout import SwingTrendBreakoutEvaluation, SwingTrendBreakoutStrategy
from backtest.simulator import Trade
from backtest.engine import BacktestEngine
from backtest.simulator import Simulator


@dataclass
class _Position:
    side: SignalAction
    entry_price: float
    entry_time: object
    stop: float | None
    target_1: float | None = None


def run_strategy_backtest(
    symbol: str,
    candles: pd.DataFrame,
    strategy,
    quantity: int = 1,
    fee_rate: float = 0.0003,
    slippage_rate: float = 0.0005,
) -> list[Trade]:
    if isinstance(strategy, Ema9200ProgressiveStrategy):
        return _run_progressive(symbol, candles, strategy, quantity, fee_rate, slippage_rate)
    if isinstance(strategy, SwingTrendBreakoutStrategy):
        return _run_swing_trend_breakout(symbol, candles, strategy, quantity, fee_rate, slippage_rate)
    simulator = Simulator(fee_rate=fee_rate, slippage_rate=slippage_rate)
    return BacktestEngine(simulator).run(symbol, candles, strategy, quantity)


def _run_progressive(symbol, candles, strategy, quantity, fee_rate, slippage_rate):
    trades: list[Trade] = []
    cycle = None
    position: _Position | None = None
    for index in range(1, len(candles) - 1):
        history = candles.iloc[: index + 1]
        if len(history) < strategy.warmup_period:
            continue
        evaluation = strategy.evaluate(symbol, history, 0, cycle)
        cycle = evaluation.cycle
        next_bar = candles.iloc[index + 1]
        if position is None and "STRONG" in evaluation.events and cycle is not None:
            position = _Position(
                SignalAction.BUY,
                float(next_bar["open"]) * (1 + slippage_rate),
                cycle.strong_signal_time or cycle.crossover_time,
                float(cycle.ema200),
            )
            continue
        if position is None:
            continue
        stop_hit = position.stop is not None and float(next_bar["low"]) <= position.stop
        invalidated = "INVALIDATED" in evaluation.events
        if stop_hit or invalidated or index == len(candles) - 2:
            exit_price = float(position.stop) if stop_hit else float(next_bar["close"] if index == len(candles) - 2 else next_bar["open"])
            reason = "stop_loss" if stop_hit else "ema200_invalidation" if invalidated else "end_of_data"
            trades.append(_trade(symbol, position, next_bar["timestamp"], exit_price, quantity, fee_rate, slippage_rate, reason, strategy.name))
            position = None
    return trades


def _run_swing_trend_breakout(symbol, candles, strategy, quantity, fee_rate, slippage_rate):
    trades: list[Trade] = []
    pending: SwingTrendBreakoutEvaluation | None = None
    position: _Position | None = None
    for index in range(1, len(candles) - 1):
        history = candles.iloc[: index + 1]
        if len(history) < strategy.warmup_period:
            continue
        next_bar = candles.iloc[index + 1]
        if position is None and pending is not None:
            confirmed = strategy.confirm_entry(pending, history)
            if confirmed is not None:
                position = _Position(
                    confirmed.action,
                    float(next_bar["open"]) * (1 + slippage_rate),
                    confirmed.timestamp,
                    confirmed.stop_loss,
                    confirmed.target_1,
                )
                pending = None
                continue
        if position is not None:
            stop_hit = position.stop is not None and float(next_bar["low"]) <= position.stop
            target_hit = position.target_1 is not None and float(next_bar["high"]) >= position.target_1
            if stop_hit or target_hit or index == len(candles) - 2:
                exit_price = float(position.stop) if stop_hit else float(position.target_1) if target_hit else float(next_bar["close"])
                reason = "stop_loss" if stop_hit else "target_1" if target_hit else "end_of_data"
                trades.append(_trade(symbol, position, next_bar["timestamp"], exit_price, quantity, fee_rate, slippage_rate, reason, strategy.name))
                position = None
                continue
        evaluation = strategy.evaluate(symbol, history)
        pending = evaluation if evaluation.qualified else None
    return trades


def _trade(symbol, position, exit_time, raw_exit_price, quantity, fee_rate, slippage_rate, reason, strategy_name):
    exit_price = raw_exit_price * (1 - slippage_rate if position.side == SignalAction.BUY else 1 + slippage_rate)
    gross = (exit_price - position.entry_price) * quantity if position.side == SignalAction.BUY else (position.entry_price - exit_price) * quantity
    costs = (position.entry_price + exit_price) * quantity * fee_rate
    return Trade(
        symbol=symbol,
        entry_time=position.entry_time,
        exit_time=exit_time,
        side=position.side,
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=quantity,
        pnl=gross - costs,
        exit_reason=reason,
        strategy_name=strategy_name,
    )
