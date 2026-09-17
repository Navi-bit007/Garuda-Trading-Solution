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


def test_swing_settings_round_trip_in_sqlite(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    values = {
        "swing_capital_limit": 5_000.0,
        "swing_quantity_limit": 3,
        "swing_trailing_atr_multiplier": 2.5,
        "swing_max_open_positions": 15,
    }

    repository.save_dashboard_settings("alice", serialize_frontend_settings(values))
    stored = repository.load_dashboard_settings("alice")
    restored = deserialize_frontend_settings(stored)

    assert restored == values
    database.close()


def test_intraday_settings_round_trip_in_sqlite(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    values = {
        "intraday_capital_limit": 10_000.0,
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


def test_kite_access_token_round_trip_in_sqlite(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)

    assert repository.load_kite_access_token("alice") == ""
    repository.save_kite_access_token("alice", "token-one")
    assert repository.load_kite_access_token("alice") == "token-one"
    repository.save_kite_access_token("alice", "token-two")
    assert repository.load_kite_access_token("alice") == "token-two"
    repository.clear_kite_access_token("alice")
    assert repository.load_kite_access_token("alice") == ""
    database.close()


def test_kite_access_token_is_scoped_by_user(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)

    repository.save_kite_access_token("alice", "alice-token")
    repository.save_kite_access_token("bob", "bob-token")

    assert repository.load_kite_access_token("alice") == "alice-token"
    assert repository.load_kite_access_token("bob") == "bob-token"
    database.close()


def test_runtime_access_token_falls_back_to_a_token_persisted_by_another_tab(tmp_path):
    """Streamlit gives every browser tab its own st.session_state -- even a duplicated tab
    starts empty. A tab that has never logged in should still pick up the day's access token
    once another tab has generated one, instead of forcing the Kite login flow to repeat."""
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "token-from-tab-one")
    settings = SimpleNamespace(user_id="alice", kite_access_token=SimpleNamespace(get_secret_value=lambda: ""))
    session_state = _SessionState(dashboard_repository=repository)
    streamlit = SimpleNamespace(session_state=session_state)

    token = dashboard_app.runtime_access_token(streamlit, settings)

    assert token == "token-from-tab-one"
    assert session_state["kite_access_token"] == "token-from-tab-one"
    database.close()


def test_runtime_access_token_ignores_persisted_token_after_explicit_logout(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "token-from-tab-one")
    settings = SimpleNamespace(user_id="alice", kite_access_token=SimpleNamespace(get_secret_value=lambda: ""))
    session_state = _SessionState(dashboard_repository=repository, kite_logged_out=True)
    streamlit = SimpleNamespace(session_state=session_state)

    token = dashboard_app.runtime_access_token(streamlit, settings)

    assert token == ""
    database.close()


def test_log_out_of_kite_clears_the_persisted_token(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "token-from-tab-one")
    session_state = _SessionState(kite_access_token="token-from-tab-one")
    streamlit = SimpleNamespace(session_state=session_state, query_params=SimpleNamespace(clear=lambda: None))

    dashboard_app.log_out_of_kite(streamlit, repository, "alice")

    assert repository.load_kite_access_token("alice") == ""
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


def _broker_settings(user_id: str = "alice"):
    return SimpleNamespace(
        user_id=user_id,
        kite_api_key="test-key",
        kite_api_secret=SimpleNamespace(get_secret_value=lambda: "test-secret"),
        kite_access_token=SimpleNamespace(get_secret_value=lambda: ""),
    )


def test_verified_token_clears_a_dead_session_on_a_real_token_exception(monkeypatch, tmp_path):
    """Kite invalidates every access token once a trading day with no refresh grant -- a token
    persisted from yesterday reads as present but is dead. Confirmed via Kite's own
    TokenException (raised for a 403/expired-session response, not a generic error), the stale
    token must be dropped from both session state and the database, with a notice queued for
    the login page -- otherwise the app keeps claiming "authenticated" long after Zerodha has
    actually revoked the session."""
    from kiteconnect.exceptions import TokenException

    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "stale-token")
    session_state = _SessionState(dashboard_repository=repository)
    streamlit = SimpleNamespace(session_state=session_state)
    settings = _broker_settings()

    class FailingProfile:
        def profile(self):
            raise TokenException("session expired")

    monkeypatch.setattr(dashboard_app, "connect_kite", lambda settings, token: SimpleNamespace(client=FailingProfile()))

    token = dashboard_app.verified_kite_access_token(streamlit, settings)

    assert token == ""
    assert not session_state.get("kite_access_token")
    assert repository.load_kite_access_token("alice") == ""
    assert session_state.get("kite_session_expired_notice") is True
    database.close()


def test_verified_token_is_trusted_and_cached_once_confirmed_valid(monkeypatch, tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "fresh-token")
    session_state = _SessionState(dashboard_repository=repository)
    streamlit = SimpleNamespace(session_state=session_state)
    settings = _broker_settings()

    call_count = {"n": 0}

    class WorkingProfile:
        def profile(self):
            call_count["n"] += 1
            return {"user_id": "AB1234"}

    monkeypatch.setattr(dashboard_app, "connect_kite", lambda settings, token: SimpleNamespace(client=WorkingProfile()))

    first = dashboard_app.verified_kite_access_token(streamlit, settings)
    second = dashboard_app.verified_kite_access_token(streamlit, settings)

    assert first == "fresh-token"
    assert second == "fresh-token"
    assert call_count["n"] == 1  # cached: the second call didn't re-verify against the broker
    database.close()


def test_verified_token_survives_a_network_error_without_forcing_a_false_logout(monkeypatch, tmp_path):
    """Only Kite's own TokenException means the session is actually dead -- a network blip (or
    Kite being briefly unreachable) while checking must never be treated the same way, or a
    transient failure here would force everyone into a false "please sign in again"."""
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_kite_access_token("alice", "fresh-token")
    session_state = _SessionState(dashboard_repository=repository)
    streamlit = SimpleNamespace(session_state=session_state)
    settings = _broker_settings()

    class UnreachableProfile:
        def profile(self):
            raise ConnectionError("network blip")

    monkeypatch.setattr(dashboard_app, "connect_kite", lambda settings, token: SimpleNamespace(client=UnreachableProfile()))

    token = dashboard_app.verified_kite_access_token(streamlit, settings)

    assert token == "fresh-token"
    assert repository.load_kite_access_token("alice") == "fresh-token"
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
