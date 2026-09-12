from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class PreviousDayHighEvaluation:
    signal: Signal


class PreviousDayHighBreakoutStrategy(Strategy):
    name = "previous_day_high_breakout"
    minimum_score = 80

    def __init__(self, strategy_name: str | None = None):
        self.name = strategy_name or type(self).name
        self.warmup_period = 2

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        if len(candles) < self.warmup_period:
            raise NoSignal("not enough completed candles for previous-day high breakout")

        frame = candles.copy()
        timestamps = pd.to_datetime(frame["timestamp"], errors="raise")
        session_dates = timestamps.dt.date
        current_date = session_dates.iloc[-1]
        previous_dates = sorted({value for value in session_dates if value < current_date})
        if not previous_dates:
            raise NoSignal("previous trading session is unavailable")

        previous_date: date = previous_dates[-1]
        previous_day_high = float(frame.loc[session_dates == previous_date, "high"].max())
        previous_close = float(frame["close"].iloc[-2])
        current = frame.iloc[-1]
        current_close = float(current["close"])
        if not (previous_close <= previous_day_high < current_close):
            raise NoSignal("current candle did not cross the previous-day high")

        timestamp = pd.Timestamp(current["timestamp"]).to_pydatetime()
        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            timestamp=timestamp,
            price=current_close,
            stop_loss=previous_day_high,
            reason="previous-day high breakout",
            score=100,
            entry_price=current_close,
            metadata={
                "previous_day_high": previous_day_high,
                "breakout_price": current_close,
                "previous_trading_date": previous_date.isoformat(),
            },
        )

    def evaluate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        instrument_token: int | None = None,
        market_regime=None,
        confirmation=None,
    ) -> PreviousDayHighEvaluation:
        return PreviousDayHighEvaluation(self.generate_signal(symbol, candles))
