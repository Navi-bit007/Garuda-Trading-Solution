from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import NoSignal, Strategy


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
    exit_reason: str = "unknown"
    target_1_hit: bool = False
    strategy_name: str = ""


@dataclass
class _OpenPosition:
    side: SignalAction
    quantity: int
    entry_price: float
    entry_time: object
    stop: float | None
    target_1: float | None
    target_2: float | None
    move_stop_to_breakeven: bool
    target_1_hit: bool = False


class Simulator:
    def __init__(self, initial_capital: float = 100_000, fee_rate: float = 0.0003, slippage_rate: float = 0.0005):
        self.initial_capital = initial_capital
        self.fee_rate = fee_rate
        self.slippage_rate = slippage_rate

    def run(self, symbol: str, candles: pd.DataFrame, strategy: Strategy, quantity: int = 1) -> list[Trade]:
        if quantity < 1:
            raise ValueError("quantity must be at least one")
        trades: list[Trade] = []
        position: _OpenPosition | None = None
        for index in range(1, len(candles) - 1):
            history = candles.iloc[: index + 1]
            if len(history) < strategy.warmup_period:
                continue
            try:
                signal = strategy.generate_signal(symbol, history)
            except NoSignal:
                signal = None
            next_bar = candles.iloc[index + 1]
            if position is None and signal is not None and signal.action in (SignalAction.BUY, SignalAction.SELL) and signal.stop_loss is not None:
                fill = float(next_bar["open"]) * (1 + self.slippage_rate if signal.action == SignalAction.BUY else 1 - self.slippage_rate)
                position = _OpenPosition(
                    side=signal.action,
                    quantity=quantity,
                    entry_price=fill,
                    entry_time=signal.timestamp,
                    stop=signal.stop_loss,
                    target_1=signal.target_1,
                    target_2=signal.target_2,
                    move_stop_to_breakeven=bool(signal.metadata.get("move_stop_to_breakeven_after_target_1")),
                )
            if position is None:
                continue
            side = position.side
            stop_hit = position.stop is not None and (
                next_bar["low"] <= position.stop if side == SignalAction.BUY else next_bar["high"] >= position.stop
            )
            target_1_hit = not position.target_1_hit and position.target_1 is not None and (
                next_bar["high"] >= position.target_1 if side == SignalAction.BUY else next_bar["low"] <= position.target_1
            )
            target_2_hit = position.target_2 is not None and (
                next_bar["high"] >= position.target_2 if side == SignalAction.BUY else next_bar["low"] <= position.target_2
            )
            reversal = signal is not None and signal.action == (SignalAction.SELL if side == SignalAction.BUY else SignalAction.BUY)
            if stop_hit:
                exit_price = float(position.stop)
                exit_reason = "stop_loss"
            elif target_2_hit:
                exit_price = float(position.target_2)
                exit_reason = "target_2"
            elif target_1_hit and position.move_stop_to_breakeven:
                position.target_1_hit = True
                position.stop = position.entry_price
                if reversal:
                    exit_price = float(next_bar["open"])
                    exit_reason = "reversal_after_target_1"
                else:
                    continue
            elif target_1_hit:
                position.target_1_hit = True
                exit_price = float(position.target_1)
                exit_reason = "target_1"
            elif reversal:
                exit_price = float(next_bar["open"])
                exit_reason = "reversal"
            elif index == len(candles) - 2:
                exit_price = float(next_bar["close"])
                exit_reason = "end_of_data"
            else:
                continue
            exit_price *= 1 - self.slippage_rate if side == SignalAction.BUY else 1 + self.slippage_rate
            gross = (exit_price - position.entry_price) * position.quantity if side == SignalAction.BUY else (position.entry_price - exit_price) * position.quantity
            costs = (position.entry_price + exit_price) * position.quantity * self.fee_rate
            trades.append(
                Trade(
                    symbol,
                    position.entry_time,
                    next_bar["timestamp"],
                    side,
                    position.entry_price,
                    exit_price,
                    position.quantity,
                    gross - costs,
                    exit_reason,
                    position.target_1_hit,
                    getattr(strategy, "name", type(strategy).__name__),
                )
            )
            position = None
        return trades
