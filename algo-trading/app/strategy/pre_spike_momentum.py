from __future__ import annotations

from dataclasses import dataclass
import pandas as pd

from app.market.candles import validate_ohlcv
from app.market.indicators import atr, ema, vwap
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal
from app.config.constants import SignalAction


@dataclass(frozen=True)
class PreSpikeMetrics:
    rvol: float
    price_change_pct: float
    vwap_value: float
    vwap_rising: bool
    ema9: float
    ema20: float
    ema50: float
    ema200: float
    previous_day_high: float
    previous_20_day_high: float
    volume_buildup_ratio: float
    range_compression: bool
    body_ratio: float
    close_location: float
    relative_strength: float | None
    score: int
    previous_day_breakout: bool
    twenty_day_breakout: bool


@dataclass(frozen=True)
class PreSpikeMomentumConfig:
    minimum_score: int = 80
    minimum_rvol: float = 2.0
    minimum_price_change_pct: float = 0.50
    minimum_volume_buildup_ratio: float = 0.0
    maximum_compression_pct: float = 2.0
    minimum_close_location: float = 0.70
    require_vwap_rising: bool = False
    require_price_above_ema20: bool = False
    require_ema9_above_ema20: bool = False
    require_ema20_above_ema50: bool = False
    require_previous_day_breakout: bool = False
    require_twenty_day_breakout: bool = False
    require_bullish_quality: bool = False
    require_volume_buildup: bool = False
    require_range_compression: bool = False


@dataclass(frozen=True)
class PreSpikeEvaluation:
    signal: Signal
    metrics: PreSpikeMetrics

    @property
    def current_vwap(self) -> float:
        return self.metrics.vwap_value

    @property
    def current_ema20(self) -> float:
        return self.metrics.ema20

    @property
    def current_price(self) -> float:
        return self.signal.current_price

    @property
    def vwap(self) -> float:
        return self.metrics.vwap_value

    @property
    def ema9(self) -> float:
        return self.metrics.ema9

    @property
    def ema20(self) -> float:
        return self.metrics.ema20

    @property
    def ema50(self) -> float:
        return self.metrics.ema50

    @property
    def ema200(self) -> float:
        return self.metrics.ema200

    @property
    def relative_volume(self) -> float:
        return self.metrics.rvol

    @property
    def breakout_status(self) -> str:
        if self.metrics.twenty_day_breakout:
            return "20-day high breakout"
        if self.metrics.previous_day_breakout:
            return "previous-day high breakout"
        return "momentum expansion"


@dataclass(frozen=True)
class PreSpikeObservation:
    timestamp: object
    current_price: float
    current_open: float
    previous_close: float
    current_atr: float
    previous_vwap: float
    previous_ema20: float
    bullish_direction_valid: bool
    signal_eligible: bool
    metrics: PreSpikeMetrics


class PreSpikeMomentumStrategy(Strategy):
    name = "PRE_SPIKE_MOMENTUM"
    timeframe = "5minute"
    warmup_period = 200
    minimum_history_days = 21

    def __init__(self, timeframe: str | None = None, config: PreSpikeMomentumConfig | None = None):
        self.timeframe = timeframe or type(self).timeframe
        self.config = config or PreSpikeMomentumConfig()

    @property
    def minimum_score(self) -> int:
        return self.config.minimum_score

    def generate_signal(self, symbol: str, candles: pd.DataFrame) -> Signal:
        return self.evaluate(symbol, candles).signal

    def inspect(self, candles: pd.DataFrame) -> PreSpikeObservation:
        frame = self._completed_frame(candles)
        if len(frame) < self.warmup_period:
            raise ValueError("not enough completed candles for PRE_SPIKE_MOMENTUM")

        timestamps = frame["timestamp"]
        session_dates = timestamps.dt.date
        current_index = len(frame) - 1
        previous_index = current_index - 1
        current_date = session_dates.iloc[current_index]
        prior_frame = frame.loc[session_dates < current_date]
        prior_dates = sorted(set(session_dates.loc[session_dates < current_date]))
        if len(prior_dates) < self.minimum_history_days:
            raise ValueError("not enough completed trading days for PRE_SPIKE_MOMENTUM")

        current = frame.iloc[current_index]
        previous = frame.iloc[previous_index]
        current_slot = timestamps.iloc[current_index].strftime("%H:%M")
        current_open = float(current["open"])
        current_close = float(current["close"])
        previous_close = float(previous["close"])
        price_change_pct = ((current_close - previous_close) / previous_close * 100) if previous_close else 0.0
        prior_slot = prior_frame.loc[timestamps.loc[prior_frame.index].dt.strftime("%H:%M") == current_slot]
        vwap_values = vwap(frame)
        current_vwap = float(vwap_values.iloc[current_index])
        bullish_direction_valid = (
            current_close > current_open
            and current_close > previous_close
            and price_change_pct > 0
            and current_close > current_vwap
        )

        slot_by_day = prior_slot.groupby(session_dates.loc[prior_slot.index]).tail(1)
        if len(slot_by_day) < 5 and bullish_direction_valid:
            raise ValueError("not enough same-time-slot volume history for PRE_SPIKE_MOMENTUM")
        same_slot_average_volume = float(slot_by_day.tail(20)["volume"].mean()) if len(slot_by_day) else 0.0
        rvol = float(current["volume"]) / same_slot_average_volume if same_slot_average_volume > 0 else 0.0

        ema9_values = ema(frame["close"], 9)
        ema20_values = ema(frame["close"], 20)
        ema50_values = ema(frame["close"], 50)
        ema200_values = ema(frame["close"], 200)
        atr_values = atr(frame, 14)
        previous_vwap = float(vwap_values.iloc[previous_index])
        current_atr = float(atr_values.iloc[current_index])
        ema9_value = float(ema9_values.iloc[current_index])
        ema20_value = float(ema20_values.iloc[current_index])
        ema50_value = float(ema50_values.iloc[current_index])
        ema200_value = float(ema200_values.iloc[current_index])
        previous_ema20 = float(ema20_values.iloc[previous_index])
        current_high = float(current["high"])
        current_low = float(current["low"])
        candle_range = current_high - current_low
        body = abs(current_close - current_open)
        body_ratio = body / candle_range if candle_range > 0 else 0.0
        close_location = (current_close - current_low) / candle_range if candle_range > 0 else 0.0

        previous_day = prior_dates[-1]
        previous_day_high = float(prior_frame.loc[session_dates.loc[prior_frame.index] == previous_day, "high"].max())
        previous_20_dates = prior_dates[-20:]
        previous_20_day_high = float(
            prior_frame.loc[session_dates.loc[prior_frame.index].isin(previous_20_dates), "high"].max()
        )
        previous_day_breakout = current_close > previous_day_high
        twenty_day_breakout = current_close > previous_20_day_high

        recent_volume = float(frame.iloc[current_index - 2 : current_index + 1]["volume"].mean())
        baseline_volume = float(frame.iloc[current_index - 22 : current_index - 2]["volume"].mean())
        volume_buildup_ratio = recent_volume / baseline_volume if baseline_volume > 0 else 0.0
        compression_frame = frame.iloc[current_index - 12 : current_index]
        compression_low = float(compression_frame["low"].min())
        compression_range = ((float(compression_frame["high"].max()) - compression_low) / compression_low * 100) if compression_low > 0 else 0.0
        range_compression = compression_range <= self.config.maximum_compression_pct
        bullish_quality = current_close > current_open and close_location >= self.config.minimum_close_location

        score = 0
        if bullish_direction_valid:
            if rvol >= 4.0:
                score += 25
            elif rvol >= 3.0:
                score += 20
            elif rvol >= 2.0:
                score += 15
            if price_change_pct >= 1.25:
                score += 20
            elif price_change_pct >= 0.75:
                score += 15
            elif price_change_pct >= 0.50:
                score += 10
            score += 10
            if current_vwap > previous_vwap:
                score += 5
            if current_close > ema20_value:
                score += 5
            if ema9_value > ema20_value:
                score += 5
            if ema20_value > ema50_value:
                score += 5
            if previous_day_breakout:
                score += 10
            if twenty_day_breakout:
                score += 15
            if bullish_quality:
                score += 5
            if volume_buildup_ratio >= 2.0:
                score += 10
            elif volume_buildup_ratio >= 1.5:
                score += 5
            if range_compression:
                score += 5
        score = min(score, 100)
        breakout = previous_day_breakout or twenty_day_breakout
        price_condition = price_change_pct >= self.config.minimum_price_change_pct or breakout
        required_conditions = (
            (not self.config.require_vwap_rising or current_vwap > previous_vwap)
            and (not self.config.require_price_above_ema20 or current_close > ema20_value)
            and (not self.config.require_ema9_above_ema20 or ema9_value > ema20_value)
            and (not self.config.require_ema20_above_ema50 or ema20_value > ema50_value)
            and (not self.config.require_previous_day_breakout or previous_day_breakout)
            and (not self.config.require_twenty_day_breakout or twenty_day_breakout)
            and (not self.config.require_bullish_quality or bullish_quality)
            and (not self.config.require_volume_buildup or volume_buildup_ratio >= self.config.minimum_volume_buildup_ratio)
            and (not self.config.require_range_compression or range_compression)
        )
        demand_condition = rvol >= self.config.minimum_rvol
        signal_eligible = (
            bullish_direction_valid
            and score >= self.config.minimum_score
            and price_condition
            and demand_condition
            and (not self.config.require_volume_buildup or volume_buildup_ratio >= self.config.minimum_volume_buildup_ratio)
            and required_conditions
        )
        metrics = PreSpikeMetrics(
            rvol=rvol,
            price_change_pct=price_change_pct,
            vwap_value=current_vwap,
            vwap_rising=current_vwap > previous_vwap,
            ema9=ema9_value,
            ema20=ema20_value,
            ema50=ema50_value,
            ema200=ema200_value,
            previous_day_high=previous_day_high,
            previous_20_day_high=previous_20_day_high,
            volume_buildup_ratio=volume_buildup_ratio,
            range_compression=range_compression,
            body_ratio=body_ratio,
            close_location=close_location,
            relative_strength=None,
            score=score,
            previous_day_breakout=previous_day_breakout,
            twenty_day_breakout=twenty_day_breakout,
        )
        return PreSpikeObservation(
            timestamp=timestamps.iloc[current_index].to_pydatetime(),
            current_price=current_close,
            current_open=current_open,
            previous_close=previous_close,
            current_atr=current_atr,
            previous_vwap=previous_vwap,
            previous_ema20=previous_ema20,
            bullish_direction_valid=bullish_direction_valid,
            signal_eligible=signal_eligible,
            metrics=metrics,
        )

    def evaluate(
        self,
        symbol: str,
        candles: pd.DataFrame,
        instrument_token: int | None = None,
        market_regime=None,
        confirmation=None,
    ) -> PreSpikeEvaluation:
        observation = self.inspect(candles)
        if not observation.signal_eligible:
            raise NoSignal
        return PreSpikeEvaluation(self._signal_from_observation(symbol, observation), observation.metrics)

    def reason_for_observation(self, observation: PreSpikeObservation) -> str:
        metrics = observation.metrics
        if not observation.bullish_direction_valid:
            return "Momentum conditions are not currently bullish."
        reason_parts = [
            f"Early bullish momentum: price {metrics.price_change_pct:+.2f}% in 5m",
            f"RVOL {metrics.rvol:.1f}x",
            "above VWAP",
        ]
        if metrics.ema9 > metrics.ema20:
            reason_parts.append("EMA9 > EMA20")
        if metrics.previous_day_breakout:
            reason_parts.append("previous-day-high breakout")
        if metrics.twenty_day_breakout:
            reason_parts.append("20-day-high breakout")
        return ", ".join(reason_parts) + "."

    def _signal_from_observation(self, symbol: str, observation: PreSpikeObservation) -> Signal:
        metrics = observation.metrics
        reason = self.reason_for_observation(observation)
        return Signal(
            symbol=symbol,
            action=SignalAction.BUY,
            timestamp=observation.timestamp,
            price=observation.current_price,
            stop_loss=observation.current_price - (1.5 * observation.current_atr),
            reason=reason,
            score=metrics.score,
            entry_price=observation.current_price,
            target_1=observation.current_price + (2.0 * observation.current_atr),
            target_2=observation.current_price + (3.0 * observation.current_atr),
            signal_reasons=("EARLY_MOMENTUM", reason),
            metadata={
                "signal_type": "EARLY_MOMENTUM",
                "timeframe": self.timeframe,
                "rvol": metrics.rvol,
                "price_change_pct": metrics.price_change_pct,
                "vwap": metrics.vwap_value,
                "ema9": metrics.ema9,
                "ema20": metrics.ema20,
                "ema50": metrics.ema50,
                "ema200": metrics.ema200,
                "previous_day_high": metrics.previous_day_high,
                "previous_20_day_high": metrics.previous_20_day_high,
                "volume_buildup_ratio": metrics.volume_buildup_ratio,
                "range_compression": metrics.range_compression,
                "body_ratio": metrics.body_ratio,
                "close_location": metrics.close_location,
                "relative_strength": None,
            },
        )

    @staticmethod
    def _completed_frame(candles: pd.DataFrame) -> pd.DataFrame:
        frame = validate_ohlcv(candles)
        if frame.empty:
            return frame
        latest = frame["timestamp"].iloc[-1]
        now = pd.Timestamp.now(tz=latest.tzinfo) if latest.tzinfo else pd.Timestamp.now()
        interval_start = now.floor("5min")
        if latest >= interval_start:
            frame = frame.iloc[:-1]
        return frame.reset_index(drop=True)
