from __future__ import annotations

import pandas as pd

from app.market.candles import validate_ohlcv


def load_data(path: str) -> pd.DataFrame:
    return validate_ohlcv(pd.read_csv(path))
