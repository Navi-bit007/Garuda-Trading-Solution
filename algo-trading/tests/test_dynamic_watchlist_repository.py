from datetime import datetime

from app.database.database import Database
from app.database.models import DynamicWatchlistRecord
from app.database.repository import Repository


def test_dynamic_watchlist_round_trips_source_results_and_refresh_state(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    refreshed_at = datetime(2026, 9, 7, 9, 20)

    repository.save_dynamic_watchlist(
        DynamicWatchlistRecord(
            user_id="alice",
            source_name="Morning",
            symbols={"NSE:AAA": 7},
            selected=False,
            refreshed_at=refreshed_at,
            session_date="2026-09-07",
            refresh_slot=1,
            require_breakout=True,
        )
    )

    result = repository.load_dynamic_watchlist("alice")

    assert result is not None
    assert result.source_name == "Morning"
    assert result.symbols == {"NSE:AAA": 7}
    assert result.selected is False
    assert result.refreshed_at == refreshed_at
    assert result.session_date == "2026-09-07"
    assert result.refresh_slot == 1
    assert result.require_breakout is True
    database.close()
