from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

import pandas as pd

from app.database.models import ProgressiveEmaCycleRecord
from app.market.candles import validate_ohlcv
from app.market.indicators import ema


@dataclass(frozen=True)
class ProgressiveEmaEvaluation:
    cycle: ProgressiveEmaCycleRecord | None
    events: tuple[str, ...] = ()


class Ema9200ProgressiveStrategy:
    name = "EMA_9_200_PROGRESSIVE"

    def __init__(self, timeframe: str = "5minute"):
        if not timeframe:
            raise ValueError("timeframe is required")
        self.timeframe = timeframe
        self.warmup_period = 201

    def evaluate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        instrument_token: int,
        current_cycle: ProgressiveEmaCycleRecord | None = None,
    ) -> ProgressiveEmaEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for EMA 9/200 progressive strategy")

        ema9_values = ema(frame["close"], 9)
        ema20_values = ema(frame["close"], 20)
        ema50_values = ema(frame["close"], 50)
        ema100_values = ema(frame["close"], 100)
        ema200_values = ema(frame["close"], 200)
        previous_index = len(frame) - 2
        current_index = len(frame) - 1
        timestamp = frame.iloc[current_index]["timestamp"].to_pydatetime()
        current_price = float(frame.iloc[current_index]["close"])
        values = {
            "ema9": float(ema9_values.iloc[current_index]),
            "ema20": float(ema20_values.iloc[current_index]),
            "ema50": float(ema50_values.iloc[current_index]),
            "ema100": float(ema100_values.iloc[current_index]),
            "ema200": float(ema200_values.iloc[current_index]),
            "current_price": current_price,
        }
        fresh_cross = (
            float(ema9_values.iloc[previous_index]) <= float(ema200_values.iloc[previous_index])
            and values["ema9"] > values["ema200"]
        )
        strong_alignment = (
            values["ema9"]
            > values["ema20"]
            > values["ema50"]
            > float(ema100_values.iloc[current_index])
        )

        if current_cycle is None or current_cycle.status != "ACTIVE":
            if not fresh_cross:
                return ProgressiveEmaEvaluation(None)
            cycle_id = f"{symbol}:{self.timeframe}:{timestamp.isoformat()}"
            cycle = ProgressiveEmaCycleRecord(
                user_id="",
                instrument_token=instrument_token,
                symbol=symbol,
                timeframe=self.timeframe,
                cycle_id=cycle_id,
                bucket="A",
                signal_type="LIGHT",
                crossover_time=timestamp,
                crossover_price=current_price,
                strong_signal_time=None,
                strong_signal_price=None,
                status="ACTIVE",
                updated_at=timestamp,
                **values,
            )
            if strong_alignment:
                cycle = replace(
                    cycle,
                    bucket="B",
                    signal_type="STRONG",
                    strong_signal_time=timestamp,
                    strong_signal_price=current_price,
                )
                return ProgressiveEmaEvaluation(cycle, ("LIGHT", "STRONG"))
            return ProgressiveEmaEvaluation(cycle, ("LIGHT",))

        cycle = replace(current_cycle, updated_at=timestamp, **values)
        if cycle.bucket == "A":
            if values["ema9"] <= values["ema200"]:
                return ProgressiveEmaEvaluation(replace(cycle, status="INVALIDATED"), ("INVALIDATED",))
            if strong_alignment:
                return ProgressiveEmaEvaluation(
                    replace(
                        cycle,
                        bucket="B",
                        signal_type="STRONG",
                        strong_signal_time=timestamp,
                        strong_signal_price=current_price,
                    ),
                    ("STRONG",),
                )
            return ProgressiveEmaEvaluation(cycle)

        if cycle.bucket == "B" and not strong_alignment:
            return ProgressiveEmaEvaluation(replace(cycle, status="WEAKENED"), ("WEAKENED",))
        return ProgressiveEmaEvaluation(cycle)