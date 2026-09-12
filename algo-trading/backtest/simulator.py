from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import Strategy


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_time: object
    exit_time: object
    side: SignalAction
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float


class Simulator:
    def __init__(self, initial_capital: float = 100_000, fee_rate: float = 0.0003, slippage_rate: float = 0.0005):
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    def run(self, symbol: str, candles: pd.DataFrame, strategy: Strategy, quantity: int = 1) -> list[Trade]:
        trades: list[Trade] = []
        position: tuple[SignalAction, int, float, object, float | None] | None = None
        for index in range(1, len(candles) - 1):
            history = candles.iloc[: index + 1]
            if len(history) < strategy.warmup_period:
                continue
            signal = strategy.generate_signal(symbol, history)
            next_bar = candles.iloc[index + 1]
            if position is None and signal.action in (SignalAction.BUY, SignalAction.SELL) and signal.stop_loss is not None:
                fill = float(next_bar["open"]) * (1 + self.slippage_rate if signal.action == SignalAction.BUY else 1 - self.slippage_rate)
                position = (signal.action, quantity, fill, signal.timestamp, signal.stop_loss)
                continue
            if position is None:
                continue
            side, held_quantity, entry_price, entry_time, stop = position
            stop_hit = (next_bar["low"] <= stop if side == SignalAction.BUY else next_bar["high"] >= stop)
            reversal = signal.action == (SignalAction.SELL if side == SignalAction.BUY else SignalAction.BUY)
            if stop_hit or reversal or index == len(candles) - 2:
                exit_price = float(stop if stop_hit else next_bar["open"])
                exit_price *= 1 - self.slippage_rate if side == SignalAction.BUY else 1 + self.slippage_rate
                gross = (exit_price - entry_price) * held_quantity if side == SignalAction.BUY else (entry_price - exit_price) * held_quantity
                costs = (entry_price + exit_price) * held_quantity * self.fee_rate
                trades.append(Trade(symbol, entry_time, next_bar["timestamp"], side, entry_price, exit_price, held_quantity, gross - costs))
                position = None
        return trades
