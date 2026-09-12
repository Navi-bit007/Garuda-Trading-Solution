from __future__ import annotations

import pandas as pd


def split_walk_forward(candles: pd.DataFrame, train_fraction: float = 0.7) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    split = int(len(candles) * train_fraction)
    return candles.iloc[:split].copy(), candles.iloc[split:].copy()
