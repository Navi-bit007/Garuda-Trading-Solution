from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction


@dataclass(frozen=True)
class Projection:
    target_1: float
    target_2: float
    target_3: float
    target_2_percent: float
    target_3_percent: float
    historical_probability_2_percent: float
    historical_probability_3_percent: float
    expected_value: float
    risk_reward: float


def project_targets(
    candles: pd.DataFrame,
    action: SignalAction,
    entry_price: float,
    stop_loss: float,
    atr_value: float,
    target_2_percent: float = 0.02,
    target_3_percent: float = 0.03,
    horizon: int = 5,
    lookback: int = 100,
) -> Projection:
    direction = 1 if action == SignalAction.BUY else -1
    target_1 = entry_price + direction * atr_value
    target_2 = entry_price * (1 + direction * target_2_percent)
    target_3 = entry_price * (1 + direction * target_3_percent)
    start = max(0, len(candles) - lookback - horizon - 1)
    end = len(candles) - horizon
    observations = reached_2 = reached_3 = 0
    for index in range(start, max(start, end)):
        future = candles.iloc[index + 1 : index + horizon + 1]
        if future.empty:
            continue
        historical_entry = float(candles["close"].iloc[index])
        observations += 1
        if action == SignalAction.BUY:
            reached_2 += int(float(future["high"].max()) >= historical_entry * (1 + target_2_percent))
            reached_3 += int(float(future["high"].max()) >= historical_entry * (1 + target_3_percent))
        else:
            reached_2 += int(float(future["low"].min()) <= historical_entry * (1 - target_2_percent))
            reached_3 += int(float(future["low"].min()) <= historical_entry * (1 - target_3_percent))
    probability_2 = reached_2 / observations if observations else 0.0
    probability_3 = reached_3 / observations if observations else 0.0
    risk = abs(entry_price - stop_loss)
    reward_2 = abs(target_2 - entry_price)
    incremental_reward_3 = abs(target_3 - target_2)
    expected_value = probability_2 * reward_2 + probability_3 * incremental_reward_3 - (1 - probability_2) * risk
    risk_reward = reward_2 / risk if risk else 0.0
    return Projection(
        target_1=target_1,
        target_2=target_2,
        target_3=target_3,
        target_2_percent=target_2_percent,
        target_3_percent=target_3_percent,
        historical_probability_2_percent=probability_2,
        historical_probability_3_percent=probability_3,
        expected_value=expected_value,
        risk_reward=risk_reward,
    )