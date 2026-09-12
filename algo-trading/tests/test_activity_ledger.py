from datetime import datetime, time

from app.config.constants import SignalAction, TradingMode
from app.config.settings import Settings
from app.database.database import Database
from app.database.repository import Repository
from app.execution.trading_pipeline import TradingPipeline


class ManualStrategy:
    atr_period = 2
    stop_atr = 1.0


class RejectingBroker:
    def instruments(self, exchange=None):
        return [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        raise RuntimeError("Market orders without market protection")


def test_activity_ledger_keeps_submitted_rejected_and_closed_events(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    settings = Settings(trading_mode=TradingMode.PAPER, force_exit=time(15, 15))
    pipeline = TradingPipeline(settings, {1: "AAA"}, ManualStrategy(), activity_repository=Repository(database))

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)
    rejected = pipeline.submit_manual_entry("AAA", 100, 101, datetime(2026, 1, 1, 9, 26), action=SignalAction.BUY)
    exit_event = pipeline.submit_manual_exit("AAA", 102, datetime(2026, 1, 1, 9, 27))

    rows = database.connection.execute(
        "SELECT event_kind, symbol, mode, side, quantity, pnl, reason FROM activity ORDER BY id"
    ).fetchall()

    assert [row[0] for row in rows] == ["entry_submitted", "entry_rejected", "exit_submitted"]
    assert entry.order_id is not None
    assert rows[0][1:] == ("AAA", "PAPER", "BUY", 10, None, "manual entry")
    assert rows[1][5] is None
    assert "stop loss" in rows[1][6]
    assert exit_event.kind == rows[2][0]
    assert rows[2][5] == 20.0
    database.close()


def test_broker_rejection_is_recorded_without_opening_a_position(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    settings = Settings(trading_mode=TradingMode.LIVE, force_exit=time(15, 15))
    pipeline = TradingPipeline(
        settings,
        {1: "AAA"},
        ManualStrategy(),
        RejectingBroker(),
        Repository(database),
    )

    event = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)
    row = database.connection.execute(
        "SELECT event_kind, reason FROM activity ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert event.kind == "entry_rejected"
    assert not pipeline.managed_positions
    assert row[0] == "entry_rejected"
    assert "Market orders without market protection" in row[1]
    database.close()


def test_position_record_is_removed_after_exit_and_daily_stats_are_recoverable(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    settings = Settings(trading_mode=TradingMode.PAPER, force_exit=time(15, 15))
    pipeline = TradingPipeline(settings, {1: "AAA"}, ManualStrategy(), activity_repository=repository)

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime.now(), quantity=10)
    assert entry.kind == "entry_submitted"
    assert repository.load_positions()
    assert pipeline.submit_manual_exit("AAA", 102, datetime.now()).kind == "exit_submitted"

    assert repository.load_positions() == []
    trades, pnl = repository.load_daily_trade_stats(datetime.now().date().isoformat())
    assert trades == 1
    assert pnl == 20.0
    database.close()