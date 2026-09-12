from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import ema
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class Ema200CloseEvaluation:
    signal: Signal
    current_ema200: float


class Ema200CloseStrategy(Strategy):
    name = "ema200_close"
    minimum_score = 80
    warmup_period = 200

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        return self.evaluate(symbol, candles, 0).signal

    def evaluate(self, symbol: str, candles: pd.DataFrame, instrument_token: int | None = None) -> Ema200CloseEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for EMA 200 close strategy")

        current = frame.iloc[-1]
        current_close = float(current["close"])
        current_ema200 = float(ema(frame["close"], 200).iloc[-1])
        if current_close <= current_ema200:
            raise NoSignal("completed candle did not close above EMA 200")

        timestamp = pd.Timestamp(current["timestamp"]).to_pydatetime()
        signal = Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            timestamp=timestamp,
            price=current_close,
            stop_loss=current_ema200,
            reason="completed candle closed above EMA 200",
            score=100,
            entry_price=current_close,
            signal_reasons=("completed-candle close above EMA 200",),
            metadata={
                "ema200": current_ema200,
                "candle_open": float(current["open"]),
                "candle_high": float(current["high"]),
                "candle_low": float(current["low"]),
                "candle_close": current_close,
            },
        )
        return Ema200CloseEvaluation(signal, current_ema200)
