from __future__ import annotations

from datetime import datetime
from typing import Any


class MarketData:
    def __init__(self, client: Any):
        self.client = client

    def historical(self, instrument_token: int, start: datetime, end: datetime, interval: str = "minute") -> list[dict]:
        return self.client.historical_data(instrument_token, start, end, interval)

    def ltp(self, instruments: list[str]) -> dict:
        return self.client.ltp(instruments)
