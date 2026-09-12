from datetime import datetime, time

import pandas as pd

from app.database.database import Database
from app.database.models import WatchlistRecord
from app.database.repository import Repository
from app.monitoring.signal_engine import AlwaysOnSignalEngine
from app.strategy.base import NoSignal
from app.strategy.ema_9_200_progressive import Ema9200ProgressiveStrategy


def frame_from_closes(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=len(closes), freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def non_promoting_cross_frame() -> pd.DataFrame:
    closes = [100.0] * 176 + [120.0] * 5
    closes.extend(120.0 + (80.0 - 120.0) * index / 19 for index in range(20))
    closes.append(154.0)
    return frame_from_closes(closes)


def immediate_promotion_frame() -> pd.DataFrame:
    return frame_from_closes([100.0] * 200 + [101.0])


def test_fresh_cross_creates_bucket_a_without_strong_alignment():
    result = Ema9200ProgressiveStrategy().evaluate("AAA", non_promoting_cross_frame(), 1)

    assert result.events == ("LIGHT",)
    assert result.cycle is not None
    assert result.cycle.bucket == "A"
    assert result.cycle.signal_type == "LIGHT"
    assert result.cycle.status == "ACTIVE"


def test_already_above_ema200_without_fresh_cross_creates_nothing():
    result = Ema9200ProgressiveStrategy().evaluate("AAA", frame_from_closes([100.0] * 200 + [101.0, 101.0]), 1)

    assert result.cycle is None


def test_bucket_a_promotes_immediately_on_alignment():
    result = Ema9200ProgressiveStrategy().evaluate("AAA", immediate_promotion_frame(), 1)

    assert result.events == ("LIGHT", "STRONG")
    assert result.cycle is not None
    assert result.cycle.bucket == "B"
    assert result.cycle.signal_type == "STRONG"
    assert result.cycle.strong_signal_time == result.cycle.crossover_time


def test_bucket_a_persists_until_alignment_or_invalidation():
    strategy = Ema9200ProgressiveStrategy()
    first = strategy.evaluate("AAA", non_promoting_cross_frame(), 1).cycle

    assert first is not None
    unchanged = strategy.evaluate("AAA", non_promoting_cross_frame(), 1, first)

    assert unchanged.events == ()
    assert unchanged.cycle is not None
    assert unchanged.cycle.bucket == "A"
    assert unchanged.cycle.status == "ACTIVE"


def test_bucket_a_is_invalidated_when_ema9_returns_below_ema200():
    strategy = Ema9200ProgressiveStrategy()
    first = strategy.evaluate("AAA", non_promoting_cross_frame(), 1).cycle

    assert first is not None
    invalidated = strategy.evaluate("AAA", frame_from_closes([100.0] * 201 + [80.0]), 1, first)

    assert invalidated.events == ("INVALIDATED",)
    assert invalidated.cycle is not None
    assert invalidated.cycle.status == "INVALIDATED"


def test_bucket_b_is_weakened_when_alignment_is_lost():
    strategy = Ema9200ProgressiveStrategy()
    first = strategy.evaluate("AAA", immediate_promotion_frame(), 1).cycle

    assert first is not None
    weakened = strategy.evaluate("AAA", frame_from_closes([100.0] * 200 + [101.0, 99.0]), 1, first)

    assert weakened.events == ("WEAKENED",)
    assert weakened.cycle is not None
    assert weakened.cycle.bucket == "B"
    assert weakened.cycle.status == "WEAKENED"


def test_new_fresh_cross_starts_a_new_cycle_after_old_cycle_ended():
    strategy = Ema9200ProgressiveStrategy()
    first = strategy.evaluate("AAA", non_promoting_cross_frame(), 1).cycle

    assert first is not None
    ended = strategy.evaluate("AAA", frame_from_closes([100.0] * 201 + [80.0]), 1, first).cycle
    assert ended is not None and ended.status == "INVALIDATED"

    new_cycle = strategy.evaluate("AAA", immediate_promotion_frame(), 1, ended)

    assert new_cycle.events == ("LIGHT", "STRONG")
    assert new_cycle.cycle is not None
    assert new_cycle.cycle.cycle_id != first.cycle_id


def test_repository_keeps_historical_cycle_and_returns_latest_payload(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    strategy = Ema9200ProgressiveStrategy()
    first = strategy.evaluate("AAA", non_promoting_cross_frame(), 1).cycle
    assert first is not None
    repository.save_progressive_cycle(first.__class__(**{**first.__dict__, "user_id": "alice"}))
    ended = strategy.evaluate("AAA", frame_from_closes([100.0] * 201 + [80.0]), 1, first).cycle
    assert ended is not None
    repository.save_progressive_cycle(ended.__class__(**{**ended.__dict__, "user_id": "alice"}))

    history = repository.load_progressive_cycles("alice")
    response = repository.load_progressive_signal_response("alice")

    assert len(history) == 1
    assert response["bucket_a"][0]["status"] == "INVALIDATED"
    database.close()


def test_repository_excludes_legacy_strong_cycle_without_ema100(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    cycle = Ema9200ProgressiveStrategy().evaluate("AAA", immediate_promotion_frame(), 1).cycle

    assert cycle is not None
    legacy_cycle = cycle.__class__(**{**cycle.__dict__, "user_id": "alice", "ema100": None})
    repository.save_progressive_cycle(legacy_cycle)

    response = repository.load_progressive_signal_response("alice")

    assert response["bucket_b"] == []
    database.close()


class EngineSettings:
    market_open = time(9, 15)
    force_exit = time(15, 15)
    signal_timeframe = "5minute"
    signal_poll_seconds = 30
    enable_telegram = False
    telegram_bot_token = ""
    telegram_chat_id = ""
    enable_ema_progressive_strategy = True


class QuietStrategy:
    name = "quiet"

    def evaluate(self, symbol, candles, instrument_token):
        raise NoSignal


def test_engine_scans_only_the_passed_watchlist_and_persists_progressive_response(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Passed", {"AAA": 1}))
    calls: list[int] = []

    def load_candles(token, interval, days):
        calls.append(token)
        return immediate_promotion_frame()

    engine = AlwaysOnSignalEngine(EngineSettings(), repository, load_candles, user_id="alice", strategy=QuietStrategy())

    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 0
    assert calls == [1]
    response = engine.response()
    assert response["strategy"] == "EMA_9_200_PROGRESSIVE"
    assert response["bucket_b"][0]["symbol"] == "AAA"
    assert response["bucket_b"][0]["signal"] == "STRONG"
    database.close()