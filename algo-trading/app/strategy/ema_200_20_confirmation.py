from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import ema
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class EmaConfirmationEvaluation:
    signal: Signal
    current_vwap: float | None
    current_ema20: float


class Ema20020ConfirmationStrategy(Strategy):
    name = "EMA 200/20 Confirmation Strategy"
    warmup_period = 201

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        return self.evaluate(symbol, candles, 0).signal

    def evaluate(self, symbol: str, candles: pd.DataFrame, instrument_token: int) -> EmaConfirmationEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for EMA 200/20 confirmation")

        ema20_values = ema(frame["close"], 20)
        ema200_values = ema(frame["close"], 200)
        confirmation_index = len(frame) - 1
        crossover_index = confirmation_index - 1
        previous_index = crossover_index - 1
        previous_close = float(frame.iloc[previous_index]["close"])
        crossover_close = float(frame.iloc[crossover_index]["close"])
        confirmation_open = float(frame.iloc[confirmation_index]["open"])
        confirmation_close = float(frame.iloc[confirmation_index]["close"])

        buy_crossed = previous_close <= float(ema200_values.iloc[previous_index]) and crossover_close > float(ema200_values.iloc[crossover_index])
        sell_crossed = previous_close >= float(ema20_values.iloc[previous_index]) and crossover_close < float(ema20_values.iloc[crossover_index])
        if buy_crossed and confirmation_close > confirmation_open:
            action = SignalAction.BUY
            reason = "EMA 200 crossover confirmed by bullish next candle"
        elif sell_crossed and confirmation_close < confirmation_open:
            action = SignalAction.SELL
            reason = "EMA 20 crossover confirmed by bearish next candle"
        else:
            raise NoSignal

        signal = Signal(
            symbol=symbol,
            action=action,
            timestamp=frame.iloc[confirmation_index]["timestamp"].to_pydatetime(),
            price=confirmation_close,
            reason=reason,
            score=100,
            entry_price=confirmation_close,
            signal_reasons=("completed-candle crossover confirmation",),
        )
        return EmaConfirmationEvaluation(signal, None, float(ema20_values.iloc[confirmation_index]))