from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime

from app.config.constants import SignalAction


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: SignalAction
    timestamp: datetime
    price: float
    stop_loss: float | None = None
    reason: str = ""
    score: int = 0
    entry_price: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    target_2_percent: float | None = None
    target_3_percent: float | None = None
    historical_probability_2_percent: float | None = None
    historical_probability_3_percent: float | None = None
    expected_value: float | None = None
    risk_reward: float | None = None
    signal_reasons: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.BUY, SignalAction.SELL)

    @property
    def side(self) -> str:
        return self.action.value

    @property
    def current_price(self) -> float:
        return self.price
