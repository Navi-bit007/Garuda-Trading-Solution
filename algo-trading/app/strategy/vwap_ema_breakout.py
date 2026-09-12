from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import adx, atr, ema, rsi, vwap
from app.strategy.base import Strategy
from app.strategy.projection import project_targets
from app.strategy.signal import Signal


@dataclass(frozen=True)
class MarketRegimeContext:
    symbol: str
    timestamp: pd.Timestamp
    close: float
    vwap: float
    ema20: float
    ema50: float

    @classmethod
    def from_candles(cls, candles: pd.DataFrame, symbol: str = "NIFTY 50") -> "MarketRegimeContext":
        frame = validate_ohlcv(candles)
        if len(frame) < 50:
            raise ValueError("not enough completed candles for market regime")
        current = frame.iloc[-1]
        return cls(
            symbol=symbol,
            timestamp=current["timestamp"],
            close=float(current["close"]),
            vwap=float(vwap(frame).iloc[-1]),
            ema20=float(ema(frame["close"], 20).iloc[-1]),
            ema50=float(ema(frame["close"], 50).iloc[-1]),
        )

    @property
    def strongly_bearish(self) -> bool:
        return self.close < self.vwap and self.ema20 < self.ema50

    @property
    def strongly_bullish(self) -> bool:
        return self.close > self.vwap and self.ema20 > self.ema50

    @property
    def label(self) -> str:
        if self.strongly_bearish:
            return "strongly bearish"
        if self.strongly_bullish:
            return "strongly bullish"
        return "neutral"


@dataclass(frozen=True)
class TimeframeConfirmation:
    timestamp: pd.Timestamp
    close: float
    ema20: float
    ema50: float

    @property
    def bullish(self) -> bool:
        return self.close > self.ema20 and self.ema20 > self.ema50

    @property
    def bearish(self) -> bool:
        return self.close < self.ema20 and self.ema20 < self.ema50

    @classmethod
    def from_candles(cls, candles: pd.DataFrame) -> "TimeframeConfirmation":
        frame = validate_ohlcv(candles)
        if len(frame) < 50:
            raise ValueError("not enough completed candles for 15-minute confirmation")
        current = frame.iloc[-1]
        return cls(
            timestamp=current["timestamp"],
            close=float(current["close"]),
            ema20=float(ema(frame["close"], 20).iloc[-1]),
            ema50=float(ema(frame["close"], 50).iloc[-1]),
        )


@dataclass(frozen=True)
class BreakoutEvaluation:
    signal: Signal
    current_price: float
    trend: str
    vwap: float
    ema20: float
    ema50: float
    ema100: float
    ema200: float
    rsi: float
    adx: float
    atr: float
    relative_volume: float
    breakout_status: str
    market_regime: str
    conditions: dict[str, bool]


class VwapEmaBreakoutStrategy(Strategy):
    name = "vwap_ema_breakout"

    def __init__(
        self,
        atr_period: int = 14,
        breakout_period: int = 20,
        volume_multiplier: float = 1.5,
        target_percent: float = 0.02,
        target_3_percent: float = 0.03,
        atr_horizon: int = 5,
        stop_atr: float = 1.5,
        rsi_buy_min: float = 55.0,
        rsi_sell_max: float = 45.0,
        adx_min: float = 20.0,
        minimum_score: int = 80,
        require_confirmation: bool = True,
        require_market_regime: bool = True,
        strategy_name: str | None = None,
    ):
        if min(atr_period, breakout_period, atr_horizon) < 1:
            raise ValueError("indicator periods and ATR horizon must be positive")
        if min(volume_multiplier, target_percent, target_3_percent, stop_atr) <= 0:
            raise ValueError("thresholds and stop multiplier must be positive")
        if not 0 <= rsi_sell_max < rsi_buy_min <= 100:
            raise ValueError("RSI sell threshold must be below the RSI buy threshold between 0 and 100")
        if adx_min < 0 or minimum_score < 1 or minimum_score > 100:
            raise ValueError("ADX threshold must be non-negative and minimum score must be between 1 and 100")
        self.atr_period = atr_period
        self.breakout_period = breakout_period
        self.volume_multiplier = volume_multiplier
        self.target_percent = target_percent
        self.target_3_percent = target_3_percent
        self.atr_horizon = atr_horizon
        self.stop_atr = stop_atr
        self.rsi_buy_min = rsi_buy_min
        self.rsi_sell_max = rsi_sell_max
        self.adx_min = adx_min
        self.minimum_score = minimum_score
        self.require_confirmation = require_confirmation
        self.require_market_regime = require_market_regime
        self.name = strategy_name or type(self).name
        self.warmup_period = 201

    def evaluate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_regime: MarketRegimeContext | None = None,
        confirmation: TimeframeConfirmation | None = None,
    ) -> BreakoutEvaluation:
        if len(candles) < self.warmup_period:
            raise ValueError("not enough completed candles for VWAP EMA breakout")
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for VWAP EMA breakout")
        current_index = len(frame) - 1
        current = frame.iloc[current_index]
        price = float(current["close"])
        current_vwap = float(vwap(frame).iloc[current_index])
        ema20_value = float(ema(frame["close"], 20).iloc[current_index])
        ema50_value = float(ema(frame["close"], 50).iloc[current_index])
        ema100_value = float(ema(frame["close"], 100).iloc[current_index])
        ema200_value = float(ema(frame["close"], 200).iloc[current_index])
        rsi_value = float(rsi(frame["close"], 14).iloc[current_index])
        adx_value = float(adx(frame, 14).iloc[current_index])
        atr_value = float(atr(frame, self.atr_period).iloc[current_index])
        previous_window = frame.iloc[current_index - self.breakout_period : current_index]
        previous_high = float(previous_window["high"].max())
        previous_low = float(previous_window["low"].min())
        average_volume = float(previous_window["volume"].mean())
        relative_volume = float(current["volume"]) / average_volume if average_volume else 0.0
        market_label = market_regime.label if market_regime else "unavailable"
        confirmation_bullish = confirmation is not None and confirmation.bullish
        confirmation_bearish = confirmation is not None and confirmation.bearish
        conditions = {
            "price_above_vwap": price > current_vwap,
            "ema20_above_ema50": ema20_value > ema50_value,
            "ema50_above_ema100": ema50_value > ema100_value,
            "ema100_above_ema200": ema100_value > ema200_value,
            "breakout_20_high": float(current["high"]) > previous_high,
            "relative_volume_1_5": relative_volume >= self.volume_multiplier,
            "rsi_at_least_55": rsi_value >= self.rsi_buy_min,
            "adx_at_least_20": adx_value >= self.adx_min,
            "confirmation_15m_bullish": not self.require_confirmation or confirmation_bullish,
            "nifty_not_strongly_bearish": not self.require_market_regime or (market_regime is not None and not market_regime.strongly_bearish),
            "price_below_vwap": price < current_vwap,
            "ema20_below_ema50": ema20_value < ema50_value,
            "ema50_below_ema100": ema50_value < ema100_value,
            "ema100_below_ema200": ema100_value < ema200_value,
            "breakdown_20_low": float(current["low"]) < previous_low,
            "rsi_at_most_45": rsi_value <= self.rsi_sell_max,
            "confirmation_15m_bearish": not self.require_confirmation or confirmation_bearish,
            "nifty_not_strongly_bullish": not self.require_market_regime or (market_regime is not None and not market_regime.strongly_bullish),
        }
        buy_names = (
            "price_above_vwap", "ema20_above_ema50", "ema50_above_ema100", "ema100_above_ema200",
            "breakout_20_high", "relative_volume_1_5", "rsi_at_least_55", "adx_at_least_20",
            "confirmation_15m_bullish", "nifty_not_strongly_bearish",
        )
        sell_names = (
            "price_below_vwap", "ema20_below_ema50", "ema50_below_ema100", "ema100_below_ema200",
            "breakdown_20_low", "relative_volume_1_5", "rsi_at_most_45", "adx_at_least_20",
            "confirmation_15m_bearish", "nifty_not_strongly_bullish",
        )
        buy_score = sum(10 for name in buy_names if conditions[name])
        sell_score = sum(10 for name in sell_names if conditions[name])
        action = SignalAction.BUY if buy_score >= self.minimum_score and buy_score > sell_score else SignalAction.SELL if sell_score >= self.minimum_score else SignalAction.HOLD
        score = buy_score if action == SignalAction.BUY else sell_score if action == SignalAction.SELL else max(buy_score, sell_score)
        trend = "bullish" if ema20_value > ema50_value > ema100_value > ema200_value else "bearish" if ema20_value < ema50_value < ema100_value < ema200_value else "mixed"
        breakout_status = "breakout" if conditions["breakout_20_high"] else "breakdown" if conditions["breakdown_20_low"] else "none"
        stop_distance = atr_value * self.stop_atr
        stop_loss = price - stop_distance if action == SignalAction.BUY else price + stop_distance if action == SignalAction.SELL else None
        projection = project_targets(frame, action, price, stop_loss or price, atr_value, self.target_percent, self.target_3_percent, self.atr_horizon) if action != SignalAction.HOLD else None
        selected_names = buy_names if action == SignalAction.BUY else sell_names if action == SignalAction.SELL else ()
        reasons = tuple(name.replace("_", " ") for name in selected_names if conditions[name])
        reason = "; ".join(reasons) if reasons else "no strong alignment"
        signal = Signal(
            symbol=symbol,
            action=action,
            timestamp=current["timestamp"].to_pydatetime(),
            price=price,
            stop_loss=stop_loss,
            reason=reason,
            score=score,
            entry_price=price,
            target_1=projection.target_1 if projection else None,
            target_2=projection.target_2 if projection else None,
            target_2_percent=projection.target_2_percent if projection else None,
            target_3_percent=projection.target_3_percent if projection else None,
            historical_probability_2_percent=projection.historical_probability_2_percent if projection else None,
            historical_probability_3_percent=projection.historical_probability_3_percent if projection else None,
            expected_value=projection.expected_value if projection else None,
            risk_reward=projection.risk_reward if projection else None,
            signal_reasons=reasons,
        )
        return BreakoutEvaluation(
            signal=signal,
            current_price=price,
            trend=trend,
            vwap=current_vwap,
            ema20=ema20_value,
            ema50=ema50_value,
            ema100=ema100_value,
            ema200=ema200_value,
            rsi=rsi_value,
            adx=adx_value,
            atr=atr_value,
            relative_volume=relative_volume,
            breakout_status=breakout_status,
            market_regime=market_label,
            conditions=conditions,
        )

    def generate_signal(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_regime: MarketRegimeContext | None = None,
        confirmation: TimeframeConfirmation | None = None,
    ) -> Signal:
        return self.evaluate(symbol, candles, market_regime, confirmation).signal
