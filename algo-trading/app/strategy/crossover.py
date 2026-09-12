from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema, vwap
from app.strategy.base import NoSignal
from app.strategy.signal import Signal


@dataclass(frozen=True)
class CrossoverEvaluation:
    signal: Signal
    current_vwap: float
    current_ema20: float


class CrossoverStrategy:
    name = "vwap_ema_crossover"

    def evaluate(self, symbol: str, candles: pd.DataFrame, instrument_token: int) -> CrossoverEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < 21:
            raise ValueError("not enough completed candles for VWAP/EMA20 crossover")
        vwap_values = vwap(frame)
        ema_values = ema(frame["close"], 20)
        previous_index = len(frame) - 2
        current_index = len(frame) - 1
        previous_close = float(frame.iloc[previous_index]["close"])
        current_close = float(frame.iloc[current_index]["close"])
        previous_vwap = float(vwap_values.iloc[previous_index])
        current_vwap = float(vwap_values.iloc[current_index])
        previous_ema20 = float(ema_values.iloc[previous_index])
        current_ema20 = float(ema_values.iloc[current_index])
        action = SignalAction.HOLD
        if previous_close <= previous_vwap and current_close > current_vwap:
            action = SignalAction.BUY
        elif previous_close >= previous_ema20 and current_close < current_ema20:
            action = SignalAction.SELL
        if action == SignalAction.HOLD:
            raise NoSignal
        current_atr = float(atr(frame, 14).iloc[current_index])
        stop_loss = current_close - current_atr if action == SignalAction.BUY else current_close + current_atr
        signal = Signal(
            symbol=symbol,
            action=action,
            timestamp=frame.iloc[current_index]["timestamp"].to_pydatetime(),
            price=current_close,
            stop_loss=stop_loss,
            reason="previous close crossed the configured VWAP/EMA20 boundary",
            score=100,
            entry_price=current_close,
            signal_reasons=("completed-candle crossover",),
        )
        return CrossoverEvaluation(signal, current_vwap, current_ema20)