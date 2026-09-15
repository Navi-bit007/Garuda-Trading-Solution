from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.broker.market_data import MarketData
from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side, TradingMode
from app.database.database import Database
from app.database.models import PositionRecord
from app.database.repository import Repository
from app.execution.trailing_stop_agent import TrailingStopAgent
from app.monitoring.notifications import Notifier


class StubKiteClient:
    def __init__(self):
        self.instrument_rows = [{"tradingsymbol": "AAA", "instrument_token": 111, "tick_size": 0.05}]
        self.ltp_response: dict = {}
        self.historical_rows: dict = {}
        self.broker_holdings = [{"tradingsymbol": "AAA", "quantity": 10}]
        self.order_history_by_id: dict = {}
        self.access_token = "initial-token"
        self.positions_error: Exception | None = None

    def instruments(self, exchange=None):
        return self.instrument_rows

    def ltp(self, instruments):
        return self.ltp_response

    def historical_data(self, instrument_token, start, end, interval):
        return self.historical_rows.get((instrument_token, interval), [])

    def positions(self):
        if self.positions_error is not None:
            raise self.positions_error
        return {"net": self.broker_holdings}

    def order_history(self, order_id):
        return self.order_history_by_id.get(order_id, [])

    def set_access_token(self, access_token):
        self.access_token = access_token


def intraday_candles(rows: int = 30, base: float = 100.0, spread: float = 4.0) -> list[dict]:
    start = datetime(2026, 1, 1, 9, 15)
    return [
        {
            "date": start + timedelta(minutes=15 * index),
            "open": base,
            "high": base + spread,
            "low": base - spread,
            "close": base,
            "volume": 1000,
        }
        for index in range(rows)
    ]


def daily_candles(rows: int = 30, start_close: float = 100.0, step: float = 1.0) -> list[dict]:
    start = datetime(2025, 1, 1)
    candles = []
    for index in range(rows):
        close = start_close + step * index
        candles.append(
            {
                "date": start + timedelta(days=index),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.5,
                "close": close,
                "volume": 10_000,
            }
        )
    return candles


def build_repository(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    return database, Repository(database)


def build_agent(repository, client, settings=None, **overrides):
    orders = OrderAPI(TradingMode.PAPER, client)
    market_data = MarketData(client)
    kwargs = dict(atr_refresh_seconds=0, swing_recompute_seconds=0)
    kwargs.update(overrides)
    return TrailingStopAgent(
        settings or object(),
        repository,
        orders,
        market_data,
        broker_client=client,
        notifier=Notifier(),
        **kwargs,
    )


def test_intraday_trailing_stop_ratchets_and_persists(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    assert saved.stop_loss > 90.0
    assert saved.stop_loss < 110.0
    assert agent.orders.paper_protective_orders["PAPER-STOP-000001"].stop_loss == saved.stop_loss
    database.close()


def test_intraday_trailing_stop_move_is_logged_to_activity(tmp_path):
    """The Live monitor page needs a queryable history of how many times, and at what price,
    the trailing-stop agent has moved a position's SL-M -- otherwise there's no way to see that
    activity short of digging through Telegram notifications."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    [activity_row] = database.connection.execute(
        "SELECT event_kind, symbol, price, stop_loss FROM activity WHERE event_kind = 'stop_trailed'"
    ).fetchall()
    assert tuple(activity_row)[:2] == ("stop_trailed", "NSE:AAA")
    assert activity_row["price"] == saved.stop_loss
    assert activity_row["stop_loss"] == saved.stop_loss
    database.close()


def test_missing_instrument_token_is_resolved_and_persisted(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=None,
            position_type="INTRADAY",
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {}

    agent.load_positions()

    assert agent.positions["NSE:AAA"].instrument_token == 111
    [saved] = repository.load_positions()
    assert saved.instrument_token == 111
    database.close()


def test_paper_mode_position_is_never_loaded_or_touched(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="PAPER",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    assert "NSE:AAA" not in agent.positions
    [saved] = repository.load_positions()
    assert saved.stop_loss == 90.0
    assert agent.orders.paper_protective_orders["PAPER-STOP-000001"].stop_loss == 90.0
    database.close()


def test_restart_never_regresses_the_persisted_stop(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()
    agent.run_once(now=entry_time + timedelta(minutes=5))
    [after_first_run] = repository.load_positions()

    restarted = build_agent(repository, client)
    restarted.load_positions()
    client.ltp_response = {"NSE:AAA": {"last_price": 95.0}}
    restarted.run_once(now=entry_time + timedelta(minutes=10))

    [after_restart] = repository.load_positions()
    assert after_restart.stop_loss >= after_first_run.stop_loss
    database.close()


def test_target_1_hit_floors_the_stop_at_breakeven(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            target_1_hit=True,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    # Price barely above entry: a plain ATR ratchet would land well below breakeven.
    client.ltp_response = {"NSE:AAA": {"last_price": 101.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    assert saved.stop_loss == 100.0
    database.close()


def test_permanent_modify_failure_leaves_db_untouched_and_notifies(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="MISSING-ORDER-ID",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    sent_messages: list[str] = []

    class RecordingNotifier(Notifier):
        def send(self, message: str) -> None:
            sent_messages.append(message)

    agent = TrailingStopAgent(
        object(),
        repository,
        OrderAPI(TradingMode.PAPER, client),
        MarketData(client),
        broker_client=client,
        notifier=RecordingNotifier(),
        atr_refresh_seconds=0,
        swing_recompute_seconds=0,
        modify_retry_attempts=2,
        modify_retry_backoff_seconds=0.01,
    )
    # Deliberately do not register "MISSING-ORDER-ID" in paper_protective_orders, so
    # modify_protective_stop raises every attempt (simulating a permanent broker rejection).
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    assert saved.stop_loss == 90.0
    assert any("critical_unprotected" in message for message in sent_messages)
    database.close()


def test_intraday_position_closed_at_broker_is_dropped_and_logged(tmp_path):
    """The always-on agent must catch an intraday position that closed at the broker (a
    protective SL-M fill, a manual exit at Zerodha, or an entry that was accepted then
    rejected) even when no Streamlit dashboard session is open to run
    TradingPipeline.sync_broker_positions() -- this is the reconciliation gap behind the
    2026-09-11 EMCURE incident, where such a mismatch went completely unrecorded."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=95.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "COMPLETE", "average_price": 94.5}]
    client.broker_holdings = []  # the SL-M already filled and flattened the holding
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    assert repository.load_positions() == []
    assert "NSE:AAA" not in agent.positions
    [trade] = repository.load_trades()
    assert trade.symbol == "NSE:AAA"
    assert trade.exit_price == 94.5
    assert trade.pnl == -55.0
    assert trade.position_type == "INTRADAY"
    [activity_row] = database.connection.execute(
        "SELECT event_kind, symbol, price, pnl FROM activity ORDER BY id"
    ).fetchall()
    assert tuple(activity_row) == ("broker_exit_detected", "NSE:AAA", 94.5, -55.0)
    database.close()


def test_swing_ema_position_trails_once_per_completed_daily_candle(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2025, 1, 1)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="SWING",
            atr_multiplier=2.0,
            strategy_name="EMA 9/200 swing",
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "TRIGGER PENDING"}]
    client.historical_rows[(111, "day")] = daily_candles(rows=30, start_close=100.0, step=1.0)

    now = datetime(2025, 2, 1, 10, 0)
    agent.run_once(now=now)

    [saved] = repository.load_positions()
    assert saved.stop_loss > 90.0

    first_stop = saved.stop_loss
    agent.run_once(now=now + timedelta(minutes=1))
    [saved_again] = repository.load_positions()
    assert saved_again.stop_loss == first_stop
    database.close()


def swing_position_record(**overrides) -> PositionRecord:
    values = dict(
        symbol="NSE:AAA",
        side="BUY",
        quantity=10,
        entry_price=100.0,
        stop_loss=90.0,
        entry_time=datetime(2025, 1, 1),
        protective_order_id="PAPER-STOP-000001",
        instrument_token=111,
        position_type="SWING",
        atr_multiplier=2.0,
        strategy_name="EMA 9/200 swing",
        trading_mode="LIVE",
    )
    values.update(overrides)
    return PositionRecord(**values)


def test_swing_entry_price_is_corrected_to_match_broker_average_price(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "TRIGGER PENDING"}]
    client.broker_holdings = [{"tradingsymbol": "AAA", "quantity": 10, "average_price": 102.35}]
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 1, 9, 20))

    [saved] = repository.load_positions()
    assert saved.entry_price == 102.35
    assert agent.positions["NSE:AAA"].record.entry_price == 102.35
    database.close()


def test_expired_overnight_stop_is_rearmed(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    # The exchange cancels "regular" day orders at market close: simulate that overnight expiry.
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "CANCELLED"}]
    client.ltp_response = {"NSE:AAA": {"last_price": 105.0}}
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 1, 9, 20))

    [saved] = repository.load_positions()
    assert saved.protective_order_id != "PAPER-STOP-000001"
    assert saved.protective_order_id in agent.orders.paper_protective_orders
    assert agent.orders.paper_protective_orders[saved.protective_order_id].stop_loss == 90.0
    database.close()


def test_live_overnight_stop_is_left_alone(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "TRIGGER PENDING"}]
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 1, 9, 20))

    [saved] = repository.load_positions()
    assert saved.protective_order_id == "PAPER-STOP-000001"
    database.close()


def test_position_closed_at_broker_is_dropped_from_tracking(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "COMPLETE", "average_price": 89.5}]
    client.broker_holdings = []  # the SL-M already filled and flattened the holding
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 1, 9, 20))

    assert repository.load_positions() == []
    assert "NSE:AAA" not in agent.positions
    [trade] = repository.load_trades()
    assert trade.symbol == "NSE:AAA"
    assert trade.exit_price == 89.5
    assert trade.position_type == "SWING"


def test_agent_picks_up_a_refreshed_access_token_without_a_restart(tmp_path):
    """Kite access tokens expire once every trading day, but this agent is meant to keep running
    for as long as any position (especially a multi-day swing one) stays open -- it will always
    outlive its token. It must pick up whatever token the dashboard most recently saved to
    `kite_session` (written on every login) rather than requiring a manual kill-and-relaunch each
    morning, which is exactly the problem that left a closed position stuck open for two days."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    agent = build_agent(repository, client)
    assert client.access_token == "initial-token"

    repository.save_kite_access_token("default", "fresh-token")
    agent.run_once(now=datetime(2026, 1, 1, 9, 20))

    assert client.access_token == "fresh-token"
    database.close()


def test_broker_failure_is_surfaced_on_the_heartbeat_instead_of_looking_healthy(tmp_path):
    """A broker call failing (e.g. an expired Kite session) is caught per-symbol so one bad
    lookup can't block reconciliation of every other position -- but that used to mean the
    failure never reached the heartbeat, so the dashboard kept showing a clean "RUNNING" agent
    while every broker call was silently failing. The heartbeat must reflect it instead."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    client.positions_error = RuntimeError("Incorrect `api_key` or `access_token`.")
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.0,
            entry_time=datetime(2026, 1, 1, 9, 20),
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)

    agent.run_once(now=datetime(2026, 1, 1, 9, 25))

    heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
    assert "access_token" in heartbeat.last_error
    database.close()
    database.close()
