from datetime import time
from types import SimpleNamespace

from app.config.constants import TradingMode
from app.database.database import Database
from app.database.repository import Repository
from dashboard import app as dashboard_app
from dashboard.app import deserialize_frontend_settings, serialize_frontend_settings


class _SessionState(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


def test_dashboard_settings_round_trip_in_sqlite(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    values = {
        "trading_mode": TradingMode.LIVE,
        "initial_capital": 250_000.0,
        "market_open": time(9, 10),
        "entry_start": time(9, 25),
        "entry_end": time(14, 50),
        "force_exit": time(15, 20),
    }

    repository.save_dashboard_settings("alice", serialize_frontend_settings(values))
    stored = repository.load_dashboard_settings("alice")
    restored = deserialize_frontend_settings(stored)

    assert restored == values
    database.close()


def test_dashboard_settings_are_scoped_by_user(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)

    repository.save_dashboard_settings("alice", {"initial_capital": 250_000.0})
    repository.save_dashboard_settings("bob", {"initial_capital": 100_000.0})

    assert repository.load_dashboard_settings("alice")["initial_capital"] == 250_000.0
    assert repository.load_dashboard_settings("bob")["initial_capital"] == 100_000.0
    database.close()


def test_dashboard_repository_rebuilds_stale_session_instance(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    stale_repository = SimpleNamespace(database=SimpleNamespace(thread_safe=True))
    session_state = _SessionState(dashboard_repository=stale_repository)
    streamlit = SimpleNamespace(session_state=session_state)

    monkeypatch.setattr(dashboard_app, "Database", lambda: database)

    repository = dashboard_app.get_dashboard_repository(streamlit)

    assert isinstance(repository, Repository)
    assert hasattr(repository, "save_dashboard_settings")
    assert session_state["dashboard_repository"] is repository
    database.close()


def test_dashboard_settings_save_supports_hot_reloaded_repository(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    stale_repository = SimpleNamespace(database=database)

    dashboard_app.save_dashboard_settings(stale_repository, "alice", {"initial_capital": 250_000.0})

    row = database.connection.execute(
        "SELECT settings FROM dashboard_settings WHERE user_id = ?",
        ("alice",),
    ).fetchone()
    assert row["settings"] == '{"initial_capital": 250000.0}'
    database.close()
