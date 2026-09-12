from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema, vwap
from app.strategy.base import Strategy
from app.strategy.signal import Signal
from app.strategy.vwap_ema_breakout import MarketRegimeContext


@dataclass(frozen=True)
class HighConvictionEvaluation:
    signal: Signal
    current_price: float
    trend: str
    vwap: float
    ema9: float
    ema20: float
    ema50: float
    ema200: float
    atr: float
    relative_volume: float
    price_change_percent: float
    candle_strength: float
    previous_high: float
    breakout_status: str
    market_regime: str
    conditions: dict[str, bool]


class HighConvictionLongStrategy(Strategy):
    name = "high_conviction_long"

    def __init__(
        self,
        atr_period: int = 14,
        breakout_period: int = 20,
        volume_multiplier: float = 1.5,
        target_1_percent: float = 0.01,
        target_2_percent: float = 0.02,
        minimum_score: int = 85,
        strategy_name: str | None = None,
    ):
        if min(atr_period, breakout_period) < 1:
            raise ValueError("indicator periods must be positive")
        if volume_multiplier <= 0 or min(target_1_percent, target_2_percent) <= 0:
            raise ValueError("volume and target thresholds must be positive")
        if target_2_percent <= target_1_percent:
            raise ValueError("target 2 must be above target 1")
        if not 1 <= minimum_score <= 100:
            raise ValueError("minimum score must be between 1 and 100")
        self.atr_period = atr_period
        self.breakout_period = breakout_period
        self.volume_multiplier = volume_multiplier
        self.target_1_percent = target_1_percent
        self.target_2_percent = target_2_percent
        self.minimum_score = minimum_score
        self.name = strategy_name or type(self).name
        self.warmup_period = max(200, breakout_period) + 1

    def evaluate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        instrument_token: int | None = None,
        market_regime: MarketRegimeContext | None = None,
        confirmation=None,
    ) -> HighConvictionEvaluation:
        del instrument_token, confirmation
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for high-conviction long strategy")

        current_index = len(frame) - 1
        current = frame.iloc[current_index]
        price = float(current["close"])
        current_vwap = float(vwap(frame).iloc[current_index])
        ema9_value = float(ema(frame["close"], 9).iloc[current_index])
        ema20_value = float(ema(frame["close"], 20).iloc[current_index])
        ema50_value = float(ema(frame["close"], 50).iloc[current_index])
        ema200_value = float(ema(frame["close"], 200).iloc[current_index])
        atr_value = float(atr(frame, self.atr_period).iloc[current_index])
        previous_window = frame.iloc[current_index - self.breakout_period : current_index]
        previous_high = float(previous_window["high"].max())
        average_volume = float(previous_window["volume"].mean())
        relative_volume = float(current["volume"]) / average_volume if average_volume > 0 else 0.0
        previous_close = float(frame["close"].iloc[current_index - 1])
        price_change_percent = ((price / previous_close) - 1) * 100 if previous_close > 0 else 0.0
        candle_range = float(current["high"] - current["low"])
        candle_strength = (price - float(current["open"])) / candle_range if candle_range > 0 else 0.0
        market_confirmed = market_regime is not None and market_regime.close > market_regime.vwap

        conditions = {
            "close_above_ema200": price > ema200_value,
            "ema9_above_ema20": ema9_value > ema20_value,
            "ema20_above_ema50": ema20_value > ema50_value,
            "breakout_20_high": price > previous_high,
            "relative_volume_1_5": relative_volume >= self.volume_multiplier,
            "close_above_vwap": price > current_vwap,
            "close_above_open": price > float(current["open"]),
            "positive_5_min_change": price > previous_close,
            "candle_strength_060": candle_strength >= 0.60,
            "nifty_above_vwap": market_confirmed,
        }
        score = (
            20 * int(conditions["close_above_ema200"])
            + 10 * int(conditions["ema9_above_ema20"] and conditions["ema20_above_ema50"])
            + 25 * int(conditions["breakout_20_high"])
            + 20 * int(conditions["relative_volume_1_5"])
            + 10 * int(conditions["close_above_vwap"])
            + 10 * int(conditions["candle_strength_060"])
            + 5 * int(conditions["nifty_above_vwap"])
        )
        hard_gates = (
            "breakout_20_high",
            "relative_volume_1_5",
            "close_above_vwap",
            "close_above_open",
            "positive_5_min_change",
        )
        qualifies = all(conditions[name] for name in hard_gates) and score >= self.minimum_score
        action = SignalAction.BUY if qualifies else SignalAction.HOLD
        stop_loss = price - atr_value if action == SignalAction.BUY else None
        target_1 = price * (1 + self.target_1_percent) if action == SignalAction.BUY else None
        target_2 = price * (1 + self.target_2_percent) if action == SignalAction.BUY else None
        reasons = tuple(name.replace("_", " ") for name, passed in conditions.items() if passed)
        reason = "; ".join(reasons) if reasons else "high-conviction long conditions not met"
        signal = Signal(
            symbol=symbol,
            action=action,
            timestamp=current["timestamp"].to_pydatetime(),
            price=price,
            stop_loss=stop_loss,
            reason=reason,
            score=score,
            entry_price=price,
            target_1=target_1,
            target_2=target_2,
            target_2_percent=self.target_2_percent,
            signal_reasons=reasons,
            metadata={
                "strategy_policy": "high_conviction_long",
                "move_stop_to_breakeven_after_target_1": True,
                "ema9": ema9_value,
                "ema20": ema20_value,
                "ema50": ema50_value,
                "ema200": ema200_value,
                "vwap": current_vwap,
                "atr": atr_value,
                "relative_volume": relative_volume,
                "price_change_percent": price_change_percent,
                "candle_strength": candle_strength,
                "previous_20_candle_high": previous_high,
                "market_nifty_above_vwap": market_confirmed,
            },
        )
        return HighConvictionEvaluation(
            signal=signal,
            current_price=price,
            trend="bullish" if price > ema200_value else "mixed",
            vwap=current_vwap,
            ema9=ema9_value,
            ema20=ema20_value,
            ema50=ema50_value,
            ema200=ema200_value,
            atr=atr_value,
            relative_volume=relative_volume,
            price_change_percent=price_change_percent,
            candle_strength=candle_strength,
            previous_high=previous_high,
            breakout_status="breakout" if conditions["breakout_20_high"] else "none",
            market_regime=market_regime.label if market_regime else "unavailable",
            conditions=conditions,
        )

    def generate_signal(
        self,
        symbol: str,
        candles: pd.DataFrame,
        market_regime: MarketRegimeContext | None = None,
        confirmation=None,
    ) -> Signal:
        return self.evaluate(symbol, candles, market_regime=market_regime, confirmation=confirmation).signal