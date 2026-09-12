from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.market.candles import validate_ohlcv
from app.market.indicators import adx, atr, ema, rsi
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal


@dataclass(frozen=True)
class SwingTrendBreakoutEvaluation:
    symbol: str
    timestamp: object
    current_price: float
    breakout_level: float
    swing_low: float
    atr14: float
    ema20: float
    ema50: float
    ema200: float
    rsi14: float
    adx14: float
    volume_ratio: float
    score: int
    classification: str
    qualified: bool
    candidate_key: str
    conditions: dict[str, bool]
    rejection_reasons: tuple[str, ...] = ()
    state: str = "REJECTED"

    @property
    def signal(self) -> Signal | None:
        return None


class SwingTrendBreakoutStrategy(Strategy):
    name = "SWING_TREND_BREAKOUT"
    warmup_period = 220

    def __init__(
        self,
        breakout_period: int = 20,
        volume_multiplier: float = 1.5,
        adx_threshold: float = 20.0,
        min_rsi: float = 50.0,
        max_rsi: float = 70.0,
        max_extension_percent: float = 8.0,
        atr_period: int = 14,
    ):
        if breakout_period < 2 or atr_period < 1:
            raise ValueError("breakout and ATR periods must be positive")
        if volume_multiplier <= 0 or adx_threshold < 0 or min_rsi >= max_rsi or max_extension_percent <= 0:
            raise ValueError("invalid swing trend breakout thresholds")
        self.breakout_period = breakout_period
        self.volume_multiplier = volume_multiplier
        self.adx_threshold = adx_threshold
        self.min_rsi = min_rsi
        self.max_rsi = max_rsi
        self.max_extension_percent = max_extension_percent
        self.atr_period = atr_period
        self.warmup_period = max(200, breakout_period + 1, atr_period + 1) + 1

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        evaluation = self.evaluate(symbol, candles)
        if not evaluation.qualified:
            raise NoSignal("; ".join(evaluation.rejection_reasons) or "swing trend breakout conditions not met")
        raise NoSignal("breakout is awaiting next-session confirmation")

    def evaluate(self, symbol: str, candles: pd.DataFrame) -> SwingTrendBreakoutEvaluation:
        frame = validate_ohlcv(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed daily candles for swing trend breakout strategy")

        index = len(frame) - 1
        current = frame.iloc[index]
        previous = frame.iloc[index - self.breakout_period:index]
        close = float(current["close"])
        breakout_level = float(previous["high"].max())
        average_volume = float(previous["volume"].mean())
        current_volume = float(current["volume"])
        volume_ratio = current_volume / average_volume if average_volume > 0 else 0.0

        ema20_value = float(ema(frame["close"], 20).iloc[index])
        ema50_value = float(ema(frame["close"], 50).iloc[index])
        ema200_value = float(ema(frame["close"], 200).iloc[index])
        atr_value = float(atr(frame, self.atr_period).iloc[index])
        rsi_value = float(rsi(frame["close"], 14).iloc[index])
        adx_value = float(adx(frame, 14).iloc[index])
        swing_low = float(frame.iloc[max(0, index - 5):index + 1]["low"].min())
        timestamp = pd.Timestamp(current["timestamp"]).to_pydatetime()

        conditions = {
            "breakout": close > breakout_level,
            "volume": volume_ratio >= self.volume_multiplier,
            "ema_alignment": ema20_value > ema50_value > ema200_value,
            "rsi_range": self.min_rsi <= rsi_value <= self.max_rsi,
            "adx": adx_value >= self.adx_threshold,
            "not_extended": close <= ema20_value * (1 + self.max_extension_percent / 100),
        }
        rejection_reasons = tuple(
            reason
            for condition, reason in (
                ("breakout", f"close {close:.2f} did not break previous {self.breakout_period}-day high {breakout_level:.2f}"),
                ("volume", f"volume ratio {volume_ratio:.2f} is below {self.volume_multiplier:.2f}"),
                ("ema_alignment", "EMA20/EMA50/EMA200 are not bullishly aligned"),
                ("rsi_range", f"RSI14 {rsi_value:.2f} is outside {self.min_rsi:.0f}-{self.max_rsi:.0f}"),
                ("adx", f"ADX14 {adx_value:.2f} is below {self.adx_threshold:.2f}"),
                ("not_extended", f"close is more than {self.max_extension_percent:.1f}% above EMA20"),
            )
            if not conditions[condition]
        )
        score = round(100 * sum(1 for passed in conditions.values() if passed) / len(conditions))
        qualified = not rejection_reasons
        classification = "A" if qualified and score == 100 else "B" if qualified else "REJECTED"
        state = "SWING_CANDIDATE" if qualified else "REJECTED"
        candidate_key = f"{symbol}:{timestamp.isoformat()}:{breakout_level:.8f}"

        return SwingTrendBreakoutEvaluation(
            symbol=symbol,
            timestamp=timestamp,
            current_price=close,
            breakout_level=breakout_level,
            swing_low=swing_low,
            atr14=atr_value,
            ema20=ema20_value,
            ema50=ema50_value,
            ema200=ema200_value,
            rsi14=rsi_value,
            adx14=adx_value,
            volume_ratio=volume_ratio,
            score=score,
            classification=classification,
            qualified=qualified,
            candidate_key=candidate_key,
            conditions=conditions,
            rejection_reasons=rejection_reasons,
            state=state,
        )

    def confirm_entry(self, candidate: SwingTrendBreakoutEvaluation, candles: pd.DataFrame) -> Signal | None:
        if not candidate.qualified:
            return None
        frame = validate_ohlcv(candles)
        if frame.empty or pd.Timestamp(frame.iloc[-1]["timestamp"]).to_pydatetime() <= candidate.timestamp:
            return None
        current = frame.iloc[-1]
        close = float(current["close"])
        if close <= candidate.breakout_level:
            return None
        stop_loss = close - 1.5 * candidate.atr14
        risk = close - stop_loss
        target_1 = close + 2 * risk
        timestamp = pd.Timestamp(current["timestamp"]).to_pydatetime()
        return Signal(
            symbol=candidate.symbol,
            action=SignalAction.BUY,
            timestamp=timestamp,
            price=close,
            stop_loss=stop_loss,
            reason="next-session confirmation above breakout candle high",
            score=candidate.score,
            entry_price=close,
            target_1=target_1,
            signal_reasons=("SWING_CANDIDATE confirmed", "close crossed breakout candle high"),
            metadata={
                "strategy_state": "CONFIRMED_BUY",
                "breakout_level": candidate.breakout_level,
                "swing_low": candidate.swing_low,
                "atr14": candidate.atr14,
                "ema20": candidate.ema20,
                "target_1": target_1,
                "risk_per_share": risk,
                "candidate_key": candidate.candidate_key,
                "classification": candidate.classification,
            },
        )