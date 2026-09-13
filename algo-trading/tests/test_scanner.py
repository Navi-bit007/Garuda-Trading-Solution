from datetime import datetime

import pandas as pd
import pytest
from kiteconnect.exceptions import InputException

from app.market.scanner import Nifty500Scanner
from app.market.signal_scanner import StrategySignalScanner
from app.strategy.previous_day_high_breakout import PreviousDayHighBreakoutStrategy
from app.strategy.vwap_ema_breakout import MarketRegimeContext, TimeframeConfirmation, VwapEmaBreakoutStrategy


def breakout_frame(direction: str = "bullish", volume: float = 2000.0) -> pd.DataFrame:
    closes = [100.0 + index * 0.25 if direction == "bullish" else 200.0 - index * 0.25 for index in range(201)]
    highs = [value + 2.0 for value in closes]
    lows = [value - 2.0 for value in closes]
    if direction == "bullish":
        closes[-1], highs[-1], lows[-1] = 153.0, 155.0, 151.0
    else:
        closes[-1], highs[-1], lows[-1] = 147.0, 149.0, 145.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=201, freq="5min"),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1000.0] * 200 + [volume],
        }
    )


def candidates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC"],
            "instrument_token": [1, 2, 3],
            "last_price": [103.0, 103.0, 97.0],
        }
    )


def neutral_regime() -> MarketRegimeContext:
    return MarketRegimeContext("NIFTY 50", pd.Timestamp("2026-01-01 14:10"), 100.0, 100.0, 100.0, 100.0)


def confirmations(direction: str) -> TimeframeConfirmation:
    return TimeframeConfirmation(
        pd.Timestamp("2026-01-01 14:10"),
        150.0,
        145.0 if direction == "bullish" else 155.0,
        140.0 if direction == "bullish" else 160.0,
    )


def test_scanner_limit_controls_ranked_results():
    scanner = Nifty500Scanner({1: "AAA", 2: "BBB", 3: "CCC"}, max_candidates=2)

    result = scanner.rank_ticks(
        [
            {"instrument_token": 1, "last_price": 110, "ohlc": {"close": 100}, "timestamp": datetime.now()},
            {"instrument_token": 2, "last_price": 105, "ohlc": {"close": 100}, "timestamp": datetime.now()},
            {"instrument_token": 3, "last_price": 102, "ohlc": {"close": 100}, "timestamp": datetime.now()},
        ]
    )

    assert result["symbol"].tolist() == ["AAA", "BBB"]


def test_scanner_skips_malformed_ticks_without_dropping_valid_ticks():
    scanner = Nifty500Scanner({1: "AAA", 2: "BBB"})

    result = scanner.rank_ticks(
        [
            {"instrument_token": 1, "last_price": "not-a-price"},
            {"instrument_token": 1, "last_price": float("nan")},
            {"instrument_token": 1, "last_price": 101, "ohlc": {"close": 0}},
            {"instrument_token": 1, "last_price": 101, "volume_traded": float("inf")},
            {"instrument_token": 2, "last_price": 105, "ohlc": {"close": 100}},
        ]
    )

    assert result["symbol"].tolist() == ["BBB"]


def test_scanner_rejects_non_positive_limit():
    with pytest.raises(ValueError, match="max_candidates must be positive"):
        Nifty500Scanner({1: "AAA"}, max_candidates=0)


def test_strategy_signal_scanner_returns_independent_buy_and_sell_limits():
    frames = {"AAA": breakout_frame("bullish", 1499), "BBB": breakout_frame("bullish", 3000), "CCC": breakout_frame("bearish", 2000)}
    result = StrategySignalScanner(buy_limit=1, sell_limit=2).scan(
        candidates(),
        VwapEmaBreakoutStrategy(),
        lambda symbol: frames[symbol],
        neutral_regime(),
        lambda symbol: confirmations("bearish" if symbol == "CCC" else "bullish"),
    )

    assert result.buy["symbol"].tolist() == ["BBB"]
    assert result.sell["symbol"].tolist() == ["CCC"]
    assert result.errors == ()
    assert result.scanned == 3


def test_strategy_signal_scanner_reuses_supplied_regime_and_keeps_symbol_errors_local():
    calls = []

    def load_candles(symbol: str) -> pd.DataFrame:
        calls.append(symbol)
        if symbol == "BBB":
            raise RuntimeError("historical unavailable")
        return breakout_frame("bullish")

    result = StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        candidates().iloc[:2],
        VwapEmaBreakoutStrategy(),
        load_candles,
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
    )

    assert calls == ["AAA", "BBB"]
    assert result.buy["symbol"].tolist() == ["AAA"]
    assert result.errors == ("BBB: historical unavailable",)


def test_strategy_signal_scanner_keeps_scanning_when_token_metadata_is_missing():
    selected = pd.DataFrame({"symbol": ["AAA", "BBB"], "instrument_token": [1, float("nan")]})

    result = StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        selected,
        VwapEmaBreakoutStrategy(),
        lambda symbol: breakout_frame("bullish"),
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
    )

    assert result.buy["symbol"].tolist() == ["AAA", "BBB"]
    assert pd.isna(result.buy.loc[result.buy["symbol"] == "BBB", "instrument_token"]).all()
    assert result.errors == ()


def test_strategy_signal_scanner_keeps_invalid_kite_token_local():
    def load_candles(symbol: str) -> pd.DataFrame:
        if symbol == "BBB":
            raise InputException("invalid token")
        return breakout_frame("bullish")

    result = StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        candidates().iloc[:2],
        VwapEmaBreakoutStrategy(),
        load_candles,
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
    )

    assert result.buy["symbol"].tolist() == ["AAA"]
    assert result.errors == ("BBB: invalid token",)


def test_strategy_signal_scanner_reports_progress_per_symbol_in_order():
    progress_calls: list[tuple[int, int, str]] = []

    StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        candidates().iloc[:2],
        VwapEmaBreakoutStrategy(),
        lambda symbol: breakout_frame("bullish"),
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
        on_progress=lambda index, total, symbol: progress_calls.append((index, total, symbol)),
    )

    assert progress_calls == [(1, 2, "AAA"), (2, 2, "BBB")]


def test_strategy_signal_scanner_records_insufficient_history_separately_from_errors():
    def load_candles(symbol: str) -> pd.DataFrame:
        return breakout_frame("bullish").iloc[:10]

    result = StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        candidates().iloc[:1],
        VwapEmaBreakoutStrategy(),
        load_candles,
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
    )

    assert result.errors == ()
    assert result.insufficient_history == ("AAA",)
    assert result.no_signal == ()


class LowScoreStrategy:
    name = "LOW_SCORE"

    def generate_signal(self, symbol, candles):
        from app.config.constants import SignalAction
        from app.strategy.signal import Signal

        latest = candles.iloc[-1]
        return Signal(symbol, SignalAction.BUY, latest["timestamp"].to_pydatetime(), float(latest["close"]), float(latest["close"] - 5), "test", score=10)


def test_strategy_signal_scanner_records_no_signal_reason_for_low_score():
    result = StrategySignalScanner(buy_limit=5, sell_limit=5, minimum_score=80).scan(
        candidates().iloc[:1],
        LowScoreStrategy(),
        lambda symbol: breakout_frame("bullish"),
    )

    assert result.buy.empty
    assert len(result.no_signal) == 1
    symbol, reason = result.no_signal[0]
    assert symbol == "AAA"
    assert "below threshold" in reason


def test_strategy_signal_scanner_result_defaults_are_empty_tuples():
    result = StrategySignalScanner(buy_limit=5, sell_limit=5).scan(
        candidates().iloc[:1],
        VwapEmaBreakoutStrategy(),
        lambda symbol: breakout_frame("bullish"),
        neutral_regime(),
        lambda symbol: confirmations("bullish"),
    )

    assert isinstance(result.insufficient_history, tuple)
    assert isinstance(result.no_signal, tuple)


def test_strategy_signal_scanner_preserves_previous_day_high_metadata():
    previous_day = pd.date_range("2026-01-05 09:15", periods=3, freq="5min")
    current_day = pd.date_range("2026-01-06 09:15", periods=2, freq="5min")
    frame = pd.DataFrame(
        {
            "timestamp": previous_day.append(current_day),
            "open": [100.0, 101.0, 102.0, 104.0, 105.0],
            "high": [103.0, 104.0, 105.0, 105.5, 106.0],
            "low": [99.0, 100.0, 101.0, 103.0, 104.0],
            "close": [101.0, 102.0, 104.0, 105.0, 106.0],
            "volume": [1000.0] * 5,
        }
    )

    result = StrategySignalScanner(minimum_score=80).scan(
        pd.DataFrame({"symbol": ["AAA"], "instrument_token": [1]}),
        PreviousDayHighBreakoutStrategy(),
        lambda symbol: frame,
    )

    assert result.buy["symbol"].tolist() == ["AAA"]
    assert result.buy.iloc[0]["previous_day_high"] == 105.0
