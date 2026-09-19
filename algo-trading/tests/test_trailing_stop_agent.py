from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from app.broker.market_data import MarketData
from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side, TradingMode
from app.database.database import Database
from app.database.models import DecisionLogRecord, PositionRecord
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
        self.broker_holdings_list: list = []
        self.todays_orders: list = []

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

    def holdings(self):
        return self.broker_holdings_list

    def orders(self):
        return self.todays_orders


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


def test_min_stop_improvement_pct_suppresses_tiny_moves_but_lets_real_ones_through(tmp_path):
    """A paisa-level ATR wobble is still technically "an improvement", but sending it to the
    broker burns one of Zerodha's 25 modifications on this order for no real protection benefit
    -- it must be silently skipped unless it clears the configured minimum step (a percentage of
    the reference price, from Risk & settings), while a move that does clear it still applies
    exactly as before."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    entry_time = datetime(2026, 1, 1, 9, 20)
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA",
            side="BUY",
            quantity=10,
            entry_price=100.0,
            stop_loss=90.00,
            entry_time=entry_time,
            protective_order_id="PAPER-STOP-000001",
            instrument_token=111,
            position_type="INTRADAY",
            atr_multiplier=1.5,
            trading_mode="LIVE",
        )
    )
    settings = SimpleNamespace(min_stop_improvement_pct=0.25)
    agent = build_agent(repository, client, settings=settings)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.00, "MIS", "NSE")
    agent.load_positions()
    position = agent.positions["NSE:AAA"]

    # reference_price 110 -> 0.25% threshold = Rs0.275; a Rs0.10 improvement must be rejected.
    agent._maybe_apply(position, 90.10, 110.0, "tiny move")
    [saved] = repository.load_positions()
    assert saved.stop_loss == 90.00

    # A move that clears the threshold still applies exactly as before.
    agent._maybe_apply(position, 90.40, 110.0, "real move")
    [saved] = repository.load_positions()
    assert saved.stop_loss == 90.40
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


def test_intraday_trailing_stop_move_is_recorded_to_the_unified_decision_log(tmp_path):
    """The decision log is the LLM-facing layer: every stop trail must land there with the
    exact calculation as its rationale, not just the "from X to Y" activity row, so a future
    training/forecasting pass can see *why* the stop moved, not just that it moved."""
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
            strategy_name="PRE_SPIKE_MOMENTUM",
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    [decision] = repository.load_decisions(symbol="NSE:AAA", event_type="stop_trailed")
    assert decision.strategy_name == "PRE_SPIKE_MOMENTUM"
    assert decision.decision == "TRAIL_STOP"
    assert "ATR14" in decision.rationale
    assert decision.outputs["new_stop"] == saved.stop_loss
    assert decision.correlation_id == f"NSE:AAA:{entry_time.isoformat()}"
    assert decision.outcome is None
    database.close()


def test_force_exit_closes_intraday_position_and_cancels_stop(tmp_path):
    """Zerodha charges an auto square-off penalty for any MIS position still open past its own
    RMS cutoff -- once the configured force-exit time (Risk & settings) passes, an intraday
    position must be market-closed and its SL-M cancelled by the standalone agent itself,
    since it's the only process guaranteed to be running all day (the tick-driven pipeline's
    own force-exit only fires while a dashboard session happens to be open and streaming)."""
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
            strategy_name="PRE_SPIKE_MOMENTUM",
            trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")
    client.ltp_response = {"NSE:AAA": {"last_price": 105.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=datetime(2026, 1, 1, 15, 20))

    assert repository.load_positions() == []
    assert "NSE:AAA" not in agent.positions
    assert "PAPER-STOP-000001" not in agent.orders.paper_protective_orders

    [trade] = repository.load_trades()
    assert trade.exit_reason == "Force close; Auto Square off"
    assert trade.exit_price == 105.0
    assert trade.pnl == 50.0

    [activity_row] = database.connection.execute(
        "SELECT event_kind, reason FROM activity WHERE event_kind = 'force_exit'"
    ).fetchall()
    assert tuple(activity_row) == ("force_exit", "Force close; Auto Square off")

    [decision] = repository.load_decisions(symbol="NSE:AAA", event_type="exit")
    assert decision.decision == "FORCE_CLOSE"
    assert decision.rationale == "Force close; Auto Square off"
    assert decision.outcome["pnl"] == 50.0
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


def test_modification_limit_hit_replaces_the_stop_with_a_fresh_order(tmp_path):
    """Zerodha rejects a modify_order() past its per-order cap (25, confirmed live) with
    "Maximum allowed order modifications exceeded" regardless of how valid the new trigger
    price is -- there's no way to reset that count on the same order_id, so the agent must
    cancel the capped order and place a fresh one rather than keep retrying (and leaving the
    stop stuck at its old price) or reporting a false "agent needs attention" error forever."""
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
        modify_retry_attempts=3,
        modify_retry_backoff_seconds=0.01,
    )
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "MIS", "NSE")

    def failing_modify(order_id, request):
        raise RuntimeError("Maximum allowed order modifications exceeded.")

    cancelled_order_ids: list[str] = []
    original_cancel = agent.orders.cancel

    def spying_cancel(order_id):
        cancelled_order_ids.append(order_id)
        return original_cancel(order_id)

    agent.orders.modify_protective_stop = failing_modify
    agent.orders.cancel = spying_cancel
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    assert saved.stop_loss > 90.0
    assert cancelled_order_ids == ["PAPER-STOP-000001"]
    assert saved.protective_order_id in agent.orders.paper_protective_orders
    assert agent.orders.paper_protective_orders[saved.protective_order_id].stop_loss == saved.stop_loss
    assert any("modification cap" in message for message in sent_messages)
    assert not any("critical_unprotected" in message for message in sent_messages)
    heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
    assert not heartbeat.last_error
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


def test_broker_exit_finds_real_fill_via_order_book_when_protective_order_never_filled(tmp_path):
    """Confirmed live incident (NSE:HAPPYFORGE): a position manually closed at Zerodha -- not
    via the tracked SL-M -- left order_history(protective_order_id) showing no fill at all, so
    the exit price silently fell back to the stale last-trailed stop price, making the P&L page
    show a wrong number. Today's order book must be checked for a different completed order on
    the same symbol before falling back to that estimate."""
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
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "OPEN", "average_price": 0}]
    client.todays_orders = [
        {
            "order_id": "MANUAL-000999",
            "tradingsymbol": "AAA",
            "transaction_type": "SELL",
            "status": "COMPLETE",
            "average_price": 105.0,
            "order_timestamp": "2026-01-01 09:25:00",
        }
    ]
    client.broker_holdings = []  # flattened -- closed at the broker
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [trade] = repository.load_trades()
    assert trade.exit_price == 105.0
    assert trade.pnl == 50.0
    assert "manual square-off" in trade.exit_reason
    database.close()


def test_failed_stop_trail_is_logged_to_the_decision_log(tmp_path):
    """A stop-trail attempt the broker rejects (for a reason other than the modification cap)
    must still show up in the decision log, not just get folded into the heartbeat's single
    last_error string -- otherwise a user reviewing a position's history sees no sign it was
    ever attempted, only that the stop never moved."""
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
    agent = build_agent(repository, client, modify_retry_backoff_seconds=0.01)

    def failing_modify(order_id, request):
        raise RuntimeError("Order not open")

    agent.orders.modify_protective_stop = failing_modify
    client.ltp_response = {"NSE:AAA": {"last_price": 110.0}}
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [saved] = repository.load_positions()
    assert saved.stop_loss == 90.0

    [decision] = repository.load_decisions(symbol="NSE:AAA", event_type="stop_trailed")
    assert decision.decision == "TRAIL_STOP_FAILED"
    assert "Order not open" in decision.rationale
    database.close()


def test_intraday_exit_logs_decision_and_backfills_outcome_onto_earlier_decisions(tmp_path):
    """Exit is the moment the real result becomes known -- it must both log its own decision
    row and backfill `outcome` onto every earlier decision (signal, stop trails) sharing the
    same correlation_id, so a future LLM pass can see what a stop-trail decision actually led
    to, not just what it decided in isolation."""
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
    correlation_id = f"NSE:AAA:{entry_time.isoformat()}"
    repository.save_decision(
        DecisionLogRecord(
            timestamp=entry_time,
            symbol="NSE:AAA",
            event_type="stop_trailed",
            decision="TRAIL_STOP",
            rationale="earlier trail",
            correlation_id=correlation_id,
        )
    )
    agent = build_agent(repository, client)
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "COMPLETE", "average_price": 94.5}]
    client.broker_holdings = []  # the SL-M already filled and flattened the holding
    client.historical_rows[(111, "15minute")] = intraday_candles()

    agent.run_once(now=entry_time + timedelta(minutes=5))

    [exit_decision] = repository.load_decisions(symbol="NSE:AAA", event_type="exit")
    assert exit_decision.decision == "EXIT"
    assert exit_decision.outputs["pnl"] == -55.0
    assert exit_decision.correlation_id == correlation_id

    [earlier_decision] = repository.load_decisions(symbol="NSE:AAA", event_type="stop_trailed")
    assert earlier_decision.outcome["pnl"] == -55.0
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

    now = datetime(2025, 2, 3, 10, 0)
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

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

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
    # The exchange cancels "regular" day orders at market close: simulate that overnight expiry
    # via today's order book (see _protective_stop_needs_rearm's docstring for why order_history
    # isn't used for this check).
    client.todays_orders = [{"order_id": "PAPER-STOP-000001", "status": "CANCELLED"}]
    client.ltp_response = {"NSE:AAA": {"last_price": 105.0}}
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    [saved] = repository.load_positions()
    assert saved.protective_order_id != "PAPER-STOP-000001"
    assert saved.protective_order_id in agent.orders.paper_protective_orders
    assert agent.orders.paper_protective_orders[saved.protective_order_id].stop_loss == 90.0

    # A re-arm used to be invisible outside a Telegram notification -- confirmed live: a user
    # saw a fresh SL-M order at the broker with nothing in the app explaining it. It must show
    # up in the activity ledger (for the Live monitor's "Recent stop-loss updates" panel) and
    # the decision log, even though the stop price itself didn't change.
    [activity_row] = database.connection.execute(
        "SELECT event_kind, symbol, stop_loss, previous_stop FROM activity WHERE event_kind = 'stop_rearmed'"
    ).fetchall()
    assert tuple(activity_row)[:3] == ("stop_rearmed", "NSE:AAA", 90.0)
    assert activity_row["previous_stop"] is None

    [decision] = repository.load_decisions(symbol="NSE:AAA", event_type="stop_rearmed")
    assert decision.decision == "REARM_STOP"
    assert "day order" in decision.rationale
    database.close()


def test_live_overnight_stop_is_left_alone(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.todays_orders = [{"order_id": "PAPER-STOP-000001", "status": "TRIGGER PENDING"}]
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    [saved] = repository.load_positions()
    assert saved.protective_order_id == "PAPER-STOP-000001"
    database.close()


def test_rearm_failure_is_surfaced_on_the_heartbeat_instead_of_looking_healthy(tmp_path):
    """A re-arm failure (confirmed live: Zerodha rejecting a sell on a T1 holding pending CDSL
    authorisation) used to be swallowed into a Telegram notification only -- the heartbeat stayed
    clean, so the dashboard kept showing a healthy green "RUNNING" banner while a real position
    sat genuinely unprotected. That's worse than any other broker-call failure, not less severe,
    so it must surface the same way."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    client.ltp_response = {"NSE:AAA": {"last_price": 105.0}}
    client.historical_rows[(111, "day")] = []

    def failing_place_protective_stop(request):
        raise RuntimeError("1 shares need to be authorised at CDSL (your demat depository) to sell")

    agent.orders.place_protective_stop = failing_place_protective_stop

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
    assert "CDSL" in heartbeat.last_error
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

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    assert repository.load_positions() == []
    assert "NSE:AAA" not in agent.positions
    [trade] = repository.load_trades()
    assert trade.symbol == "NSE:AAA"
    assert trade.exit_price == 89.5
    assert trade.position_type == "SWING"


def test_settled_swing_holding_is_not_wrongly_closed(tmp_path):
    """A CNC buy drops out of positions() once it settles into a holding (commonly the next
    trading day or two) even though nothing was ever sold -- confirmed live against Zerodha for
    a real position (NSE:JUBLPHARMA, bought 11 Sep, still shown in holdings() with t1_quantity=1
    on 16 Sep but completely absent from positions().net). Relying on positions() alone would
    make _reconcile_swing_positions fabricate a close for every swing position a few days after
    entry, purely from settlement -- kite.holdings() must be checked before concluding "closed"."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "TRIGGER PENDING"}]
    client.broker_holdings = []  # absent from positions() -- looks closed by that check alone
    client.broker_holdings_list = [{"tradingsymbol": "AAA", "quantity": 0, "t1_quantity": 10}]
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    [saved] = repository.load_positions()
    assert saved.symbol == "NSE:AAA"
    assert "NSE:AAA" in agent.positions
    assert repository.load_trades() == []
    database.close()


def test_settled_swing_holding_still_gets_its_overnight_expired_stop_rearmed(tmp_path):
    """The daily SL-M re-arm exists precisely for a position that's a day or more past entry --
    which, in practice, is exactly when it has already settled out of positions() and into
    holdings(). The re-arm check must not be silently skipped just because the position is now
    only visible via holdings() rather than positions()."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    # This order id is from a previous day, so -- confirmed live against Zerodha -- it's simply
    # absent from today's order book entirely (client.todays_orders stays empty), the same as any
    # other "regular" day order once its trading day has passed.
    client.ltp_response = {"NSE:AAA": {"last_price": 105.0}}
    client.broker_holdings = []  # settled -- absent from positions()
    client.broker_holdings_list = [{"tradingsymbol": "AAA", "quantity": 10, "t1_quantity": 0}]
    client.historical_rows[(111, "day")] = []

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    [saved] = repository.load_positions()
    assert saved.protective_order_id != "PAPER-STOP-000001"
    assert saved.protective_order_id in agent.orders.paper_protective_orders
    assert agent.orders.paper_protective_orders[saved.protective_order_id].stop_loss == 90.0
    database.close()


def test_swing_holding_lookup_failure_does_not_wrongly_close_a_position(tmp_path):
    """If the holdings() call itself fails, there's no reliable way to distinguish "genuinely
    closed" from "settled into a holding" that cycle -- better to leave the position tracked and
    retry next cycle than risk fabricating a close for something still owned."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(swing_position_record())
    agent = build_agent(repository, client)
    agent.orders.paper_protective_orders["PAPER-STOP-000001"] = OrderRequest("NSE:AAA", Side.SELL, 10, 100.0, 90.0, "CNC", "NSE")
    client.order_history_by_id["PAPER-STOP-000001"] = [{"status": "TRIGGER PENDING"}]
    client.broker_holdings = []
    client.historical_rows[(111, "day")] = []

    def failing_holdings():
        raise RuntimeError("network error")

    client.holdings = failing_holdings

    agent.run_once(now=datetime(2025, 2, 3, 9, 20))

    [saved] = repository.load_positions()
    assert saved.symbol == "NSE:AAA"
    assert repository.load_trades() == []
    database.close()


def test_agent_stops_itself_past_the_configured_shutdown_time(tmp_path):
    """Kite tokens expire daily and there's nothing left to trail once the market's closed for
    the day (Zerodha's own SL-M orders expire at close regardless of this process), so running
    overnight is pointless -- the agent should exit on its own rather than sit idle until someone
    notices and restarts it the next morning. Default agent_shutdown_time (via getattr fallback
    on the plain `object()` settings used in these tests) is 15:40."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
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

    agent.run_once(now=datetime(2026, 1, 1, 15, 41))

    assert agent.shut_down_for_the_day is True
    assert agent._stop_event.is_set() is True
    # No broker work should have happened this cycle -- positions were never even reloaded.
    assert agent.positions == {}
    database.close()


def test_agent_clears_its_heartbeat_on_end_of_day_shutdown(tmp_path):
    """A heartbeat left to simply age out reads on the dashboard as "stalled: check the process",
    which is alarming for what is actually an intentional, clean end-of-day exit -- clearing it
    immediately gives the calmer "not started yet" state instead."""
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    agent = build_agent(repository, client)
    agent.run_once(now=datetime(2026, 1, 1, 9, 20))
    assert repository.load_agent_heartbeat("trailing_stop_agent") is not None

    agent.run_once(now=datetime(2026, 1, 1, 15, 41))

    assert repository.load_agent_heartbeat("trailing_stop_agent") is None
    database.close()


def test_agent_keeps_running_before_the_shutdown_time(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    agent = build_agent(repository, client)

    agent.run_once(now=datetime(2026, 1, 1, 15, 39))

    assert agent.shut_down_for_the_day is False
    assert agent._stop_event.is_set() is False
    database.close()


def test_agent_exits_for_the_day_on_a_weekend_without_touching_the_broker(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    repository.save_position(
        PositionRecord(
            symbol="NSE:AAA", side="BUY", quantity=10, entry_price=100.0, stop_loss=90.0,
            entry_time=datetime(2026, 1, 1, 9, 20), protective_order_id="PAPER-STOP-000001",
            instrument_token=111, position_type="INTRADAY", atr_multiplier=1.5, trading_mode="LIVE",
        )
    )
    agent = build_agent(repository, client)

    agent.run_once(now=datetime(2026, 9, 19, 10, 0))  # a Saturday

    assert agent.shut_down_for_the_day is True
    assert agent._stop_event.is_set() is True
    assert agent.positions == {}  # never even loaded -- no broker calls attempted
    assert repository.load_agent_heartbeat("trailing_stop_agent") is None
    database.close()


def test_agent_exits_for_the_day_on_a_configured_holiday(tmp_path):
    database, repository = build_repository(tmp_path)
    client = StubKiteClient()
    settings = SimpleNamespace(market_holidays="2026-01-26")
    agent = build_agent(repository, client, settings=settings)

    agent.run_once(now=datetime(2026, 1, 26, 10, 0))  # a Monday, but a configured holiday

    assert agent.shut_down_for_the_day is True
    assert agent._stop_event.is_set() is True
    database.close()


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
