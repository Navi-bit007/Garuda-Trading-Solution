from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
import math

import pandas as pd


def rank_by_relative_volume(candidates: pd.DataFrame, volume_window: int = 20) -> pd.DataFrame:
    result = candidates.copy()
    result["average_volume"] = result["volume"].rolling(volume_window, min_periods=volume_window).mean()
    result["relative_volume"] = result["volume"] / result["average_volume"]
    return result.sort_values("relative_volume", ascending=False, na_position="last")


@dataclass(frozen=True)
class Nifty500Scanner:
    token_to_symbol: dict[int, str]
    max_candidates: int = 20

    def __post_init__(self) -> None:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be positive")

    def rank_ticks(self, ticks: Iterable[dict]) -> pd.DataFrame:
        records = []
        for tick in ticks:
            if not isinstance(tick, Mapping):
                continue
            try:
                token = int(tick.get("instrument_token", 0))
                symbol = self.token_to_symbol.get(token)
                if symbol is None or "last_price" not in tick:
                    continue
                last_price = float(tick["last_price"])
                ohlc = tick.get("ohlc")
                previous_close_value = ohlc.get("close", last_price) if isinstance(ohlc, Mapping) else last_price
                previous_close = float(previous_close_value)
                volume_value = tick.get("volume_traded", tick.get("volume", 0))
                if volume_value is None:
                    volume_value = tick.get("volume", 0)
                volume = float(volume_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                not math.isfinite(last_price)
                or not math.isfinite(previous_close)
                or not math.isfinite(volume)
                or last_price <= 0
                or previous_close <= 0
                or volume < 0
            ):
                continue
            change_percent = (last_price - previous_close) / previous_close if previous_close else 0.0
            records.append(
                {
                    "symbol": symbol,
                    "instrument_token": token,
                    "last_price": last_price,
                    "change_percent": change_percent,
                    "volume": volume,
                }
            )
        if not records:
            return pd.DataFrame(columns=["symbol", "instrument_token", "last_price", "change_percent", "volume"])
        return pd.DataFrame(records).sort_values(
            ["change_percent", "volume", "symbol"],
            key=lambda column: column.abs() if column.name == "change_percent" else column,
            ascending=[False, False, True],
        ).head(self.max_candidates).reset_index(drop=True)

    def scan(self, ticks: Iterable[dict]) -> list[str]:
        return self.rank_ticks(ticks)["symbol"].tolist()
