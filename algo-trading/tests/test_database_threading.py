from datetime import datetime
from threading import Thread

from app.database.database import Database
from app.database.models import PositionRecord
from app.database.repository import Repository


def test_repository_can_read_shared_database_from_another_thread(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_position(PositionRecord("AAA", "BUY", 10, 100, 95, datetime(2026, 1, 1, 9, 25)))
    result = []

    worker = Thread(target=lambda: result.extend(repository.load_positions()))
    worker.start()
    worker.join()

    assert [position.symbol for position in result] == ["AAA"]
    database.close()


def test_position_trading_mode_round_trips(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_position(PositionRecord("AAA", "BUY", 10, 100, 95, datetime(2026, 1, 1, 9, 25), trading_mode="LIVE"))
    repository.save_position(PositionRecord("BBB", "BUY", 5, 50, 45, datetime(2026, 1, 1, 9, 30), trading_mode="PAPER"))

    positions = {position.symbol: position for position in repository.load_positions()}

    assert positions["AAA"].trading_mode == "LIVE"
    assert positions["BBB"].trading_mode == "PAPER"
    database.close()


def test_legacy_position_row_without_trading_mode_defaults_to_live(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    # Simulate a row written before the trading_mode migration: the column default kicks in.
    database.connection.execute(
        "INSERT INTO positions (symbol, side, quantity, entry_price, stop_loss, entry_time) "
        "VALUES ('CCC', 'BUY', 1, 10, 9, '2026-01-01T09:25:00')"
    )
    database.connection.commit()
    repository = Repository(database)

    [saved] = repository.load_positions()

    assert saved.trading_mode == "LIVE"
    database.close()