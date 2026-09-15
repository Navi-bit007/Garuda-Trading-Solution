from datetime import time
from functools import lru_cache

from pydantic import Field, SecretStr

try:
    from pydantic import field_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict

    PYDANTIC_V2 = True
except ImportError:
    from pydantic import BaseSettings, validator

    PYDANTIC_V2 = False

from app.config.constants import TradingMode


class Settings(BaseSettings):
    if PYDANTIC_V2:
        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    else:
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"

    kite_api_key: str = ""
    kite_api_secret: SecretStr = SecretStr("")
    kite_access_token: SecretStr = SecretStr("")
    trading_mode: TradingMode = TradingMode.PAPER
    initial_capital: float = Field(default=100_000, gt=0)
    max_open_positions: int = Field(default=3, ge=1)
    max_trades_per_day: int = Field(default=5, ge=1)
    max_capital_deployment: float = Field(default=0.80, gt=0, le=1)
    market_open: time = time(9, 15)
    entry_start: time = time(9, 20)
    entry_end: time = time(14, 45)
    force_exit: time = time(15, 15)
    trailing_atr_multiplier: float = Field(default=1.5, gt=0)
    enable_telegram: bool = False
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    user_id: str = "default"
    signal_timeframe: str = "5minute"
    signal_poll_seconds: int = Field(default=300, ge=5)
    pre_spike_cooldown_minutes: int = Field(default=30, ge=0)
    enable_ema_progressive_strategy: bool = True
    swing_capital_limit: float = Field(default=2_000.0, gt=0)
    swing_quantity_limit: int = Field(default=1, ge=1)
    swing_trailing_atr_multiplier: float = Field(default=2.0, gt=0)
    swing_max_open_positions: int = Field(default=10, ge=1)
    intraday_capital_limit: float = Field(default=5_000.0, gt=0)
    intraday_leverage_multiplier: float = Field(default=1.0, ge=1.0)

    if PYDANTIC_V2:
        @field_validator("entry_end")
        @classmethod
        def entry_end_after_start(cls, value: time, info):
            entry_start = info.data.get("entry_start", time(9, 20))
            if value <= entry_start:
                raise ValueError("entry_end must be after entry_start")
            return value

        @field_validator("force_exit")
        @classmethod
        def force_exit_after_open(cls, value: time, info):
            market_open = info.data.get("market_open", time(9, 15))
            if value <= market_open:
                raise ValueError("force_exit must be after market_open")
            return value
    else:
        @validator("entry_end", allow_reuse=True)
        def entry_end_after_start(cls, value: time, values):
            entry_start = values.get("entry_start", time(9, 20))
            if value <= entry_start:
                raise ValueError("entry_end must be after entry_start")
            return value

        @validator("force_exit", allow_reuse=True)
        def force_exit_after_open(cls, value: time, values):
            market_open = values.get("market_open", time(9, 15))
            if value <= market_open:
                raise ValueError("force_exit must be after market_open")
            return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
