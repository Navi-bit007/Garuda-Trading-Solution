from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class Ema9200SwingEvaluation:
    signal: Signal
    ema9: float
    ema200: float
    atr: float


class Ema9200SwingStrategy(Strategy):
    name = "ema9_200_swing"
    minimum_score = 100
    warmup_period = 201
    atr_period = 14
    trailing_atr_multiplier = 2.0

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        return self.evaluate(symbol, candles, 0).signal

    def evaluate(self, symbol: str, candles: pd.DataFrame, instrument_token: int | None = None) -> Ema9200SwingEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed daily candles for EMA 9/200 swing strategy")

        ema9_values = ema(frame["close"], 9)
        ema200_values = ema(frame["close"], 200)
        previous_index = len(frame) - 2
        current_index = len(frame) - 1
        previous_ema9 = float(ema9_values.iloc[previous_index])
        previous_ema200 = float(ema200_values.iloc[previous_index])
        current_ema9 = float(ema9_values.iloc[current_index])
        current_ema200 = float(ema200_values.iloc[current_index])
        if not (previous_ema9 <= previous_ema200 and current_ema9 > current_ema200):
            raise NoSignal("EMA 9 did not freshly cross above EMA 200")

        current_atr = float(atr(frame, self.atr_period).iloc[current_index])
        current_close = float(frame.iloc[current_index]["close"])
        initial_stop = min(current_ema200, current_close - self.trailing_atr_multiplier * current_atr)
        if not pd.notna(initial_stop) or initial_stop <= 0 or initial_stop >= current_close:
            raise NoSignal("EMA 9/200 cross does not have a valid protective stop")

        current = frame.iloc[current_index]
        timestamp = pd.Timestamp(current["timestamp"]).to_pydatetime()
        signal = Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            timestamp=timestamp,
            price=current_close,
            stop_loss=initial_stop,
            reason="fresh EMA 9 cross above EMA 200 on completed daily candle",
            score=100,
            entry_price=current_close,
            signal_reasons=("completed daily candle", "fresh EMA 9 above EMA 200 cross"),
            metadata={
                "timeframe": "day",
                "ema9": current_ema9,
                "ema200": current_ema200,
                "atr": current_atr,
                "initial_stop": initial_stop,
                "candle_open": float(current["open"]),
                "candle_high": float(current["high"]),
                "candle_low": float(current["low"]),
                "candle_close": current_close,
            },
        )
        return Ema9200SwingEvaluation(signal, current_ema9, current_ema200, current_atr)
