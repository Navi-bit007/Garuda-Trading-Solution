from datetime import datetime, time

import pandas as pd

from app.database.database import Database
from app.database.models import WatchlistRecord
from app.database.repository import Repository
from app.monitoring.signal_engine import AlwaysOnSignalEngine, refresh_selected_watchlist_tokens
from app.strategy.crossover import CrossoverStrategy, NoSignal
from app.strategy.pre_spike_momentum import PreSpikeMomentumStrategy
from app.strategy.previous_day_high_breakout import PreviousDayHighBreakoutStrategy
from tests.test_pre_spike_momentum import pre_spike_frame


def crossover_frame() -> pd.DataFrame:
    closes = [100.0] * 19 + [99.0, 101.0]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=21, freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def sell_crossover_frame() -> pd.DataFrame:
    closes = [100.0] * 19 + [101.0, 99.0]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01 09:15", periods=21, freq="5min"),
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


class EngineSettings:
    market_open = time(9, 15)
    force_exit = time(15, 15)
    signal_timeframe = "5minute"
    signal_poll_seconds = 30
    enable_telegram = False
    telegram_bot_token = ""
    telegram_chat_id = ""
    pre_spike_cooldown_minutes = 30


class InputException(Exception):
    pass


class BrokerInstrumentCatalog:
    def instruments(self):
        return [
            {"tradingsymbol": "AAA", "instrument_token": 101, "exchange": "NSE", "instrument_type": "EQ"},
            {"tradingsymbol": "BBB", "instrument_token": 202, "exchange": "BSE", "instrument_type": "EQ"},
            {"tradingsymbol": "AAA", "instrument_token": 303, "exchange": "NSE", "instrument_type": "FUT"},
        ]


def test_crossover_strategy_uses_only_the_requested_rules():
    signal = CrossoverStrategy().evaluate("AAA", crossover_frame(), 1).signal

    assert signal.side == "BUY"
    assert signal.timestamp == datetime(2026, 1, 1, 10, 55)
    assert signal.score == 100


def test_crossover_strategy_emits_sell_for_the_requested_ema20_cross():
    signal = CrossoverStrategy().evaluate("AAA", sell_crossover_frame(), 1).signal

    assert signal.side == "SELL"


def test_always_on_engine_persists_once_and_isolates_users(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    repository.save_watchlist(WatchlistRecord("bob", "Morning", {"AAA": 1}))
    settings = EngineSettings()
    engine = AlwaysOnSignalEngine(settings, repository, lambda token, interval, days: crossover_frame())

    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 2
    assert engine.run_once(datetime(2026, 1, 1, 10, 1)) == 0
    assert len(repository.load_signals("alice")) == 1
    assert len(repository.load_signals("bob")) == 1
    assert len([item for item in repository.load_notifications() if item.user_id == "alice"]) == 1
    assert len([item for item in repository.load_notifications() if item.user_id == "bob"]) == 1
    database.close()


def test_engine_reloads_selected_watchlists_and_skips_closed_market(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    calls = []

    def load_candles(token, interval, days):
        calls.append(token)
        return crossover_frame()

    engine = AlwaysOnSignalEngine(EngineSettings(), repository, load_candles, user_id="alice")
    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 1
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"BBB": 2}))
    assert engine.run_once(datetime(2026, 1, 1, 10, 5)) == 1
    assert calls == [1, 2]
    assert engine.run_once(datetime(2026, 1, 1, 16, 0)) == 0
    assert calls == [1, 2]
    database.close()


def test_refresh_selected_watchlist_tokens_replaces_stale_tokens_and_symbols(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"NSE:AAA": 999, "BSE:BBB": 2, "NSE:OLD": 3}))

    stale_symbols = refresh_selected_watchlist_tokens(repository, "alice", BrokerInstrumentCatalog())

    assert stale_symbols == ("NSE:OLD",)
    assert repository.load_watchlists("alice", selected_only=True)[0].symbols == {"BSE:BBB": 202, "NSE:AAA": 101}
    database.close()


def test_engine_does_not_mark_invalid_token_as_cycle_error(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"NSE:OLD": 999}))

    def load_candles(token, interval, days):
        raise InputException("invalid token")

    engine = AlwaysOnSignalEngine(EngineSettings(), repository, load_candles, user_id="alice")

    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 0
    assert engine.last_error == ""
    assert repository.load_signal_engine_status("alice").last_error == ""
    database.close()


def test_engine_stops_between_symbols_when_stop_is_requested(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1, "BBB": 2}))
    calls = []
    engine = None

    def load_candles(token, interval, days):
        calls.append(token)
        if token == 1:
            engine.stop()
        return crossover_frame()

    engine = AlwaysOnSignalEngine(EngineSettings(), repository, load_candles, user_id="alice")

    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 1
    assert calls == [1]
    database.close()


def test_engine_uses_the_selected_strategy(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))

    class SelectedStrategy:
        name = "selected strategy"

        def evaluate(self, symbol, candles, instrument_token):
            return CrossoverStrategy().evaluate(symbol, candles, instrument_token)

    strategy = SelectedStrategy()
    engine = AlwaysOnSignalEngine(EngineSettings(), repository, lambda token, interval, days: crossover_frame(), strategy=strategy, user_id="alice")

    assert engine.strategy is strategy
    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 1
    assert repository.load_signals("alice")[0].symbol == "AAA"
    database.close()


def test_engine_disables_unselected_progressive_strategy(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))

    class SelectedStrategy:
        name = "selected strategy"

        def evaluate(self, symbol, candles, instrument_token):
            return CrossoverStrategy().evaluate(symbol, candles, instrument_token)

    engine = AlwaysOnSignalEngine(
        EngineSettings(),
        repository,
        lambda token, interval, days: crossover_frame(),
        user_id="alice",
        strategy=SelectedStrategy(),
        progressive_strategy=object(),
        enable_progressive_strategy=False,
    )

    assert engine.progressive_strategy is None
    assert engine.run_once(datetime(2026, 1, 1, 10, 0)) == 1
    assert [signal.strategy for signal in repository.load_signals("alice")] == ["selected strategy"]
    database.close()


def test_engine_supports_previous_day_high_generate_signal_strategy(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-05 09:15", periods=5, freq="5min").tolist()
            + pd.date_range("2026-01-06 09:15", periods=2, freq="5min").tolist(),
            "open": [100.0, 101.0, 102.0, 104.0, 105.0, 106.0, 107.0],
            "high": [103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0],
            "low": [99.0, 100.0, 101.0, 103.0, 104.0, 105.0, 106.0],
            "close": [101.0, 102.0, 104.0, 105.0, 106.0, 106.5, 108.0],
            "volume": [1000.0] * 7,
        }
    )
    engine = AlwaysOnSignalEngine(
        EngineSettings(),
        repository,
        lambda token, interval, days: candles,
        strategy=PreviousDayHighBreakoutStrategy(),
        enable_progressive_strategy=False,
        user_id="alice",
    )

    assert engine.run_once(datetime(2026, 1, 6, 9, 30)) == 1
    signals = repository.load_signals("alice")
    assert len(signals) == 1
    assert signals[0].metadata["previous_day_high"] == 107.0
    database.close()


def test_generated_signal_is_recorded_to_the_unified_decision_log(tmp_path):
    """Every generated signal -- not just ones later traded -- must land in decision_log so a
    future LLM pass has the scanner's full rationale (score, reasons, targets) to train or
    forecast from, independent of whether the signal ever became a position."""
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-05 09:15", periods=5, freq="5min").tolist()
            + pd.date_range("2026-01-06 09:15", periods=2, freq="5min").tolist(),
            "open": [100.0, 101.0, 102.0, 104.0, 105.0, 106.0, 107.0],
            "high": [103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0],
            "low": [99.0, 100.0, 101.0, 103.0, 104.0, 105.0, 106.0],
            "close": [101.0, 102.0, 104.0, 105.0, 106.0, 106.5, 108.0],
            "volume": [1000.0] * 7,
        }
    )
    engine = AlwaysOnSignalEngine(
        EngineSettings(),
        repository,
        lambda token, interval, days: candles,
        strategy=PreviousDayHighBreakoutStrategy(),
        enable_progressive_strategy=False,
        user_id="alice",
    )

    assert engine.run_once(datetime(2026, 1, 6, 9, 30)) == 1

    [decision] = repository.load_decisions(event_type="signal_generated")
    assert decision.symbol == "AAA"
    assert decision.strategy_name == "previous_day_high_breakout"
    assert decision.decision == "BUY"
    assert decision.rationale
    assert decision.outputs["stop_loss"] == 107.0
    assert decision.correlation_id.startswith("AAA:signal:")
    database.close()


def test_crossover_strategy_returns_no_signal_when_rules_do_not_match():
    frame = crossover_frame()
    frame.loc[19, "close"] = 99.0
    frame.loc[20, "close"] = 99.0
    frame.loc[19, "open"] = 99.0
    frame.loc[20, "open"] = 99.0
    frame.loc[19, "high"] = 100.0
    frame.loc[20, "high"] = 100.0
    frame.loc[19, "low"] = 98.0
    frame.loc[20, "low"] = 98.0

    try:
        CrossoverStrategy().evaluate("AAA", frame, 1)
    except NoSignal:
        return
    raise AssertionError("expected a non-crossover candle to produce no signal")


def pre_spike_engine_frame(timestamp_offset: int = 0, bullish: bool = True) -> pd.DataFrame:
    frame = pre_spike_frame(
        last_close=102.0 if bullish else 98.0,
        last_open=100.0 if bullish else 103.0,
        last_volume=1000.0,
    ).copy()
    if timestamp_offset:
        sessions = sorted(frame["timestamp"].dt.date.unique())
        base_slot = frame["timestamp"].iloc[-1].time()
        extra_rows = []
        for minutes in range(5, timestamp_offset + 1, 5):
            slot = (pd.Timestamp.combine(pd.Timestamp("2026-01-02").date(), base_slot) + pd.Timedelta(minutes=minutes)).time()
            for session in sessions:
                extra_rows.append(
                    {
                        "timestamp": pd.Timestamp.combine(session, slot),
                        "open": 100.0,
                        "high": 100.5,
                        "low": 99.5,
                        "close": 100.0,
                        "volume": 100.0,
                    }
                )
        frame = pd.concat([frame, pd.DataFrame(extra_rows)], ignore_index=True).sort_values("timestamp").reset_index(drop=True)
        frame.loc[frame.index[-1], "open"] = 100.0 if bullish else 103.0
        frame.loc[frame.index[-1], "high"] = 103.0 if bullish else 103.0
        frame.loc[frame.index[-1], "low"] = 99.5 if bullish else 97.5
        frame.loc[frame.index[-1], "close"] = 102.0 if bullish else 98.0
        frame.loc[frame.index[-1], "volume"] = 1000.0
    return frame


def test_pre_spike_engine_persists_one_event_and_is_restart_safe(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    settings = EngineSettings()
    strategy = PreSpikeMomentumStrategy()
    loader = lambda token, interval, days: pre_spike_engine_frame()
    engine = AlwaysOnSignalEngine(
        settings,
        repository,
        loader,
        user_id="alice",
        strategy=strategy,
        enable_progressive_strategy=False,
    )

    assert engine.run_once(datetime(2026, 1, 2, 10, 0)) == 1
    assert engine.run_once(datetime(2026, 1, 2, 10, 1)) == 0
    assert len(repository.load_signals("alice", strategy=strategy.name)) == 1
    assert len(repository.load_pre_spike_events("alice", strategy.name, active_only=False)) == 1

    restarted = AlwaysOnSignalEngine(
        settings,
        repository,
        loader,
        user_id="alice",
        strategy=PreSpikeMomentumStrategy(),
        enable_progressive_strategy=False,
    )
    assert restarted.run_once(datetime(2026, 1, 2, 10, 2)) == 0
    assert len(repository.load_signals("alice", strategy=strategy.name)) == 1
    database.close()


def test_pre_spike_engine_updates_active_event_without_new_signal(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    settings = EngineSettings()
    frames = iter([pre_spike_engine_frame(), pre_spike_engine_frame(5)])
    engine = AlwaysOnSignalEngine(
        settings,
        repository,
        lambda token, interval, days: next(frames),
        user_id="alice",
        strategy=PreSpikeMomentumStrategy(),
        enable_progressive_strategy=False,
    )

    assert engine.run_once(datetime(2026, 1, 2, 10, 0)) == 1
    assert engine.run_once(datetime(2026, 1, 2, 10, 1)) == 0
    event = repository.load_pre_spike_events("alice", PreSpikeMomentumStrategy.name, active_only=False)[0]
    assert event.status == "ACTIVE"
    assert event.latest_time == datetime(2026, 1, 2, 15, 30)
    assert len(repository.load_signals("alice", strategy=PreSpikeMomentumStrategy.name)) == 1
    database.close()


def test_pre_spike_engine_ends_event_after_two_consecutive_invalidations(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_watchlist(WatchlistRecord("alice", "Morning", {"AAA": 1}))
    settings = EngineSettings()
    frames = iter([
        pre_spike_engine_frame(),
        pre_spike_engine_frame(5, bullish=False),
        pre_spike_engine_frame(10, bullish=False),
    ])
    engine = AlwaysOnSignalEngine(
        settings,
        repository,
        lambda token, interval, days: next(frames),
        user_id="alice",
        strategy=PreSpikeMomentumStrategy(),
        enable_progressive_strategy=False,
    )

    assert engine.run_once(datetime(2026, 1, 2, 10, 0)) == 1
    assert engine.run_once(datetime(2026, 1, 2, 10, 1)) == 0
    assert engine.run_once(datetime(2026, 1, 2, 10, 2)) == 0
    event = repository.load_pre_spike_events("alice", PreSpikeMomentumStrategy.name, active_only=False)[0]
    assert event.status == "COOLDOWN"
    assert event.invalidation_streak == 2
    assert event.cooldown_until == datetime(2026, 1, 2, 16, 5)
    assert len(repository.load_signals("alice", strategy=PreSpikeMomentumStrategy.name)) == 1
    database.close()
