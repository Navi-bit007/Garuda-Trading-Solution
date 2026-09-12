from dataclasses import dataclass
from dataclasses import field
from datetime import datetime


@dataclass(frozen=True)
class OrderRecord:
    broker_order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    created_at: datetime


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float


@dataclass(frozen=True)
class ActivityRecord:
    event_kind: str
    symbol: str
    timestamp: datetime
    mode: str
    price: float | None = None
    order_id: str | None = None
    side: str | None = None
    quantity: int | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    pnl: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class NotificationRecord:
    strategy: str
    universe: str
    sector: str
    symbol: str
    side: str
    score: int
    signal_timestamp: datetime
    created_at: datetime
    message: str
    read: bool = False
    user_id: str = "default"
    instrument_token: int | None = None


@dataclass(frozen=True)
class SignalRecord:
    user_id: str
    instrument_token: int
    symbol: str
    side: str
    signal_timestamp: datetime
    price: float
    vwap: float | None = None
    ema20: float | None = None
    stop_loss: float | None = None
    reason: str = ""
    strategy: str = ""
    score: int = 0
    entry_price: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    event_id: str = ""
    signal_key: str = ""


@dataclass(frozen=True)
class PreSpikeEventRecord:
    user_id: str
    instrument_token: int
    symbol: str
    strategy: str
    timeframe: str
    trading_date: str
    event_id: str
    status: str
    trigger_time: datetime
    trigger_price: float
    latest_time: datetime
    latest_price: float
    latest_score: int
    highest_score: int
    last_valid_time: datetime | None
    entry_price: float
    cooldown_until: datetime | None = None
    ended_at: datetime | None = None
    invalidation_streak: int = 0
    latest_metadata: dict[str, object] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class ProgressiveEmaCycleRecord:
    user_id: str
    instrument_token: int
    symbol: str
    timeframe: str
    cycle_id: str
    bucket: str
    signal_type: str
    crossover_time: datetime
    crossover_price: float
    strong_signal_time: datetime | None
    strong_signal_price: float | None
    ema9: float
    ema20: float
    ema50: float
    ema100: float | None
    ema200: float
    current_price: float
    status: str
    updated_at: datetime


@dataclass(frozen=True)
class WatchlistRecord:
    user_id: str
    name: str
    symbols: dict[str, int]
    selected: bool = True
    updated_at: datetime | None = None


@dataclass(frozen=True)
class DynamicWatchlistRecord:
    user_id: str
    source_name: str
    symbols: dict[str, int]
    selected: bool = True
    updated_at: datetime | None = None
    refreshed_at: datetime | None = None
    session_date: str = ""
    refresh_slot: int | None = None
    require_breakout: bool = False


@dataclass(frozen=True)
class SignalEngineStatus:
    user_id: str
    last_run_at: datetime
    last_error: str = ""
    heartbeat_at: datetime | None = None


@dataclass(frozen=True)
class PositionRecord:
    symbol: str
    side: str
    quantity: int
    entry_price: float
    stop_loss: float
    entry_time: datetime
    target_1: float | None = None
    target_2: float | None = None
    protective_order_id: str | None = None
    target_1_hit: bool = False
    instrument_token: int | None = None
    position_type: str = "INTRADAY"
    atr_multiplier: float | None = None
    strategy_name: str = ""


@dataclass(frozen=True)
class StrategyPresetRecord:
    name: str
    strategy_type: str
    parameters: dict
    created_at: datetime
    updated_at: datetime
