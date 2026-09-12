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