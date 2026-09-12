from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import pandas as pd

from app.config.constants import SignalAction
from app.strategy.base import NoSignal, Strategy
from app.strategy.signal import Signal
from app.strategy.vwap_ema_breakout import BreakoutEvaluation, MarketRegimeContext, TimeframeConfirmation


SIGNAL_COLUMNS = [
    "symbol", "instrument_token", "side", "score", "current_price", "entry_price", "stop_loss",
    "target_1", "target_2", "target_2_percent", "target_3_percent", "historical_probability_2_percent",
    "historical_probability_3_percent", "expected_value", "risk_reward", "trend", "vwap", "ema20",
    "ema50", "ema100", "ema200", "rsi", "adx", "atr", "relative_volume", "breakout_status",
    "market_regime", "strategy", "timestamp", "signal_reasons",
    "previous_day_high",
]


@dataclass(frozen=True)
class SignalScanResult:
    buy: pd.DataFrame
    sell: pd.DataFrame
    errors: tuple[str, ...]
    scanned: int


class StrategySignalScanner:
    def __init__(self, buy_limit: int = 5, sell_limit: int = 5, minimum_score: int = 80):
        if buy_limit < 1 or sell_limit < 1:
            raise ValueError("signal limits must be positive")
        if not 0 <= minimum_score <= 100:
            raise ValueError("minimum_score must be between 0 and 100")
        self.buy_limit = buy_limit
        self.sell_limit = sell_limit
        self.minimum_score = minimum_score

    def scan(
        self,
        selected_stocks: Iterable[str] | pd.DataFrame,
        strategy: Strategy,
        candle_loader: Callable[[str], pd.DataFrame],
        market_regime: MarketRegimeContext | None = None,
        confirmation_loader: Callable[[str], TimeframeConfirmation] | None = None,
    ) -> SignalScanResult:
        candidates = self._normalise_selected_stocks(selected_stocks)
        buys: list[dict] = []
        sells: list[dict] = []
        errors: list[str] = []
        for symbol, token in candidates:
            try:
                candles = candle_loader(symbol)
                confirmation = confirmation_loader(symbol) if confirmation_loader else None
                if isinstance(strategy, type) or not hasattr(strategy, "evaluate"):
                    signal = strategy.generate_signal(symbol, candles)
                    row = self._signal_row(signal, strategy, token)
                else:
                    evaluation = strategy.evaluate(
                        symbol,
                        candles,
                        market_regime=market_regime,
                        confirmation=confirmation,
                    )
                    signal = evaluation.signal
                    row = self._evaluation_row(evaluation, strategy, token)
                if signal.score < self.minimum_score:
                    continue
            except NoSignal:
                continue
            except Exception as error:
                errors.append(f"{symbol}: {error}")
                continue
            if signal.action == SignalAction.BUY:
                buys.append(row)
            elif signal.action == SignalAction.SELL:
                sells.append(row)
        return SignalScanResult(
            buy=self._rank(buys, self.buy_limit),
            sell=self._rank(sells, self.sell_limit),
            errors=tuple(errors),
            scanned=len(candidates),
        )

    @staticmethod
    def _normalise_selected_stocks(selected_stocks: Iterable[str] | pd.DataFrame) -> list[tuple[str, int | None]]:
        if isinstance(selected_stocks, pd.DataFrame):
            if "symbol" not in selected_stocks.columns:
                raise ValueError("selected stocks must contain a symbol column")
            return [
                (str(row.symbol), StrategySignalScanner._optional_token(getattr(row, "instrument_token", None)))
                for row in selected_stocks.itertuples(index=False)
            ]
        return [(str(symbol), None) for symbol in selected_stocks]

    @staticmethod
    def _optional_token(value: object) -> int | None:
        if value is None:
            return None
        try:
            if bool(pd.isna(value)):
                return None
            token = int(value)
            if float(value) != token:
                return None
            return token
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _signal_row(signal: Signal, strategy: Strategy, token: int | None) -> dict:
        return {
            "symbol": signal.symbol,
            "instrument_token": token,
            "side": signal.side,
            "score": signal.score,
            "current_price": signal.current_price,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target_1": signal.target_1,
            "target_2": signal.target_2,
            "target_2_percent": signal.target_2_percent,
            "target_3_percent": signal.target_3_percent,
            "historical_probability_2_percent": signal.historical_probability_2_percent,
            "historical_probability_3_percent": signal.historical_probability_3_percent,
            "expected_value": signal.expected_value,
            "risk_reward": signal.risk_reward,
            "trend": None,
            "vwap": None,
            "ema20": None,
            "ema50": None,
            "ema100": None,
            "ema200": None,
            "rsi": None,
            "adx": None,
            "atr": None,
            "relative_volume": None,
            "breakout_status": None,
            "market_regime": None,
            "strategy": strategy.name,
            "timestamp": signal.timestamp,
            "signal_reasons": "; ".join(signal.signal_reasons) or signal.reason,
            "previous_day_high": signal.metadata.get("previous_day_high"),
        }

    @staticmethod
    def _evaluation_row(evaluation: BreakoutEvaluation, strategy: Strategy, token: int | None) -> dict:
        signal = evaluation.signal
        return {
            "symbol": signal.symbol,
            "instrument_token": token,
            "side": signal.side,
            "score": signal.score,
            "current_price": getattr(evaluation, "current_price", signal.current_price),
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target_1": signal.target_1,
            "target_2": signal.target_2,
            "target_2_percent": signal.target_2_percent,
            "target_3_percent": signal.target_3_percent,
            "historical_probability_2_percent": signal.historical_probability_2_percent,
            "historical_probability_3_percent": signal.historical_probability_3_percent,
            "expected_value": signal.expected_value,
            "risk_reward": signal.risk_reward,
            "trend": getattr(evaluation, "trend", None),
            "vwap": getattr(evaluation, "vwap", None),
            "ema20": getattr(evaluation, "ema20", None),
            "ema50": getattr(evaluation, "ema50", None),
            "ema100": getattr(evaluation, "ema100", None),
            "ema200": getattr(evaluation, "ema200", None),
            "rsi": getattr(evaluation, "rsi", None),
            "adx": getattr(evaluation, "adx", None),
            "atr": getattr(evaluation, "atr", None),
            "relative_volume": getattr(evaluation, "relative_volume", None),
            "breakout_status": getattr(evaluation, "breakout_status", None),
            "market_regime": getattr(evaluation, "market_regime", None),
            "strategy": strategy.name,
            "timestamp": signal.timestamp,
            "signal_reasons": "; ".join(signal.signal_reasons),
            "previous_day_high": signal.metadata.get("previous_day_high"),
        }

    @staticmethod
    def _rank(rows: list[dict], limit: int) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame(columns=SIGNAL_COLUMNS)
        return (
            pd.DataFrame(rows, columns=SIGNAL_COLUMNS)
            .sort_values(["score", "expected_value", "symbol"], ascending=[False, False, True], na_position="last")
            .head(limit)
            .reset_index(drop=True)
        )
