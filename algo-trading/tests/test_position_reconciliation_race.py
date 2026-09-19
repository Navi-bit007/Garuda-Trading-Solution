from datetime import datetime
from threading import Barrier, Thread

from app.database.database import Database
from app.database.models import PositionRecord
from app.database.repository import Repository


def test_claim_position_close_returns_the_record_and_deletes_it(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_position(PositionRecord("AAA", "BUY", 10, 100.0, 95.0, datetime(2026, 1, 1, 9, 25)))

    claimed = repository.claim_position_close("AAA")

    assert claimed is not None
    assert claimed.symbol == "AAA"
    assert repository.load_positions() == []
    database.close()


def test_claim_position_close_returns_none_when_already_claimed(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_position(PositionRecord("AAA", "BUY", 10, 100.0, 95.0, datetime(2026, 1, 1, 9, 25)))
    repository.claim_position_close("AAA")

    second_claim = repository.claim_position_close("AAA")

    assert second_claim is None


def test_claim_position_close_returns_none_for_a_symbol_that_was_never_open(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)

    assert repository.claim_position_close("NEVER:OPEN") is None


def test_concurrent_claims_for_the_same_symbol_only_ever_let_one_caller_win(tmp_path):
    """Simulates the real race: the standalone agent and a dashboard pipeline, each running in
    their own process against the same SQLite file, both notice the same broker-side close and
    call claim_position_close for the same symbol at essentially the same instant. Only one of
    them must ever get the record back."""
    db_path = str(tmp_path / "trading.sqlite3")
    setup_database = Database(db_path)
    setup_database.initialize()
    Repository(setup_database).save_position(PositionRecord("AAA", "BUY", 10, 100.0, 95.0, datetime(2026, 1, 1, 9, 25)))
    setup_database.close()

    results: list = [None, None]
    barrier = Barrier(2)

    def claim(index: int) -> None:
        database = Database(db_path)
        repository = Repository(database)
        barrier.wait()
        results[index] = repository.claim_position_close("AAA")
        database.close()

    threads = [Thread(target=claim, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].symbol == "AAA"
