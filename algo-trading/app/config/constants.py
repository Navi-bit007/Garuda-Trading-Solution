from enum import StrEnum


class TradingMode(StrEnum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


NSE_TICK_SIZE = 0.05
DEFAULT_LOT_SIZE = 1
