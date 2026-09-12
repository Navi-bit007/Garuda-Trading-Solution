from datetime import datetime, timezone

from app.database.database import Database
from app.database.models import NotificationRecord, ProgressiveEmaCycleRecord, SignalEngineStatus, SignalRecord
from app.database.repository import Repository


def test_signal_backup_contains_current_results_and_clear_removes_only_signal_state(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    timestamp = datetime(2026, 9, 6, 10, 0)
    repository.save_signal(SignalRecord("alice", 1, "NSE:AAA", "BUY", timestamp, 100.0, reason="legacy"))
    repository.save_notification(
        NotificationRecord(
            strategy="EMA_9_200_PROGRESSIVE",
            universe="selected watchlists",
            sector="All sectors",
            symbol="NSE:AAA",
            side="BUY",
            score=100,
            signal_timestamp=timestamp,
            created_at=timestamp,
            message="strong signal",
            user_id="alice",
            instrument_token=1,
        )
    )
    repository.save_progressive_cycle(
        ProgressiveEmaCycleRecord(
            user_id="alice",
            instrument_token=1,
            symbol="NSE:AAA",
            timeframe="5minute",
            cycle_id="NSE:AAA:5minute:2026-09-06T10:00:00",
            bucket="B",
            signal_type="STRONG",
            crossover_time=timestamp,
            crossover_price=100.0,
            strong_signal_time=timestamp,
            strong_signal_price=101.0,
            ema9=101.0,
            ema20=100.5,
            ema50=100.0,
            ema100=99.5,
            ema200=99.0,
            current_price=101.0,
            status="ACTIVE",
            updated_at=timestamp,
        )
    )
    repository.save_signal_engine_status(SignalEngineStatus("alice", timestamp))

    backup, counts = repository.backup_and_clear_signal_state("alice")

    assert len(backup["signals"]) == 1
    assert len(backup["progressive_cycles"]) == 1
    assert len(backup["notifications"]) == 1
    assert backup["exported_at"].endswith("+05:30")
    assert backup["signal_engine_response"]["bucket_b"][0]["symbol"] == "NSE:AAA"
    assert backup["signal_engine_response"]["bucket_b"][0]["ema100"] == 99.5
    assert counts == {"signals": 1, "pre_spike_events": 0, "ema_progressive_cycles": 1, "notifications": 1}
    assert repository.load_signals("alice") == []
    assert repository.load_notifications() == []
    assert repository.load_progressive_cycles("alice") == []
    assert repository.load_signal_engine_status("alice") is not None
    database.close()


def test_signal_backup_converts_utc_timestamps_to_india_time(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    timestamp = datetime(2026, 9, 8, 4, 20, tzinfo=timezone.utc)
    repository.save_signal(SignalRecord("alice", 1, "NSE:AAA", "BUY", timestamp, 100.0, reason="utc"))

    backup = repository.export_signal_state("alice")

    assert backup["signals"][0]["signal_timestamp"] == "2026-09-08T09:50:00.000+05:30"
    database.close()