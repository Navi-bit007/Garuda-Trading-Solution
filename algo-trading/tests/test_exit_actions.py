from datetime import datetime

from app.broker.order_api import OrderAPI
from app.config.constants import TradingMode
from app.database.database import Database
from app.database.models import DecisionLogRecord, PositionRecord
from app.database.repository import Repository
from app.execution.exit_actions import (
    ExitOutcome,
    close_position_at_market,
    correlation_id_for,
    exchange_of,
    product_code_for,
    tradingsymbol_of,
)


def build_repository(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    return database, Repository(database)


def intraday_position(**overrides) -> PositionRecord:
    defaults = dict(
        symbol="NSE:AAA",
        side="BUY",
        quantity=10,
        entry_price=100.0,
        stop_loss=90.0,
        entry_time=datetime(2026, 1, 1, 9, 20),
        protective_order_id="PAPER-STOP-000001",
        position_type="INTRADAY",
        strategy_name="PRE_SPIKE_MOMENTUM",
        trading_mode="LIVE",
    )
    defaults.update(overrides)
    return PositionRecord(**defaults)


def test_exchange_and_tradingsymbol_helpers_split_on_colon():
    assert exchange_of("NSE:AAA") == "NSE"
    assert tradingsymbol_of("NSE:AAA") == "AAA"
    assert exchange_of("AAA") == "NSE"
    assert tradingsymbol_of("aaa") == "AAA"


def test_product_code_is_cnc_for_swing_and_mis_otherwise():
    assert product_code_for("SWING") == "CNC"
    assert product_code_for("INTRADAY") == "MIS"


def test_correlation_id_matches_symbol_and_entry_time():
    record = intraday_position()
    assert correlation_id_for(record) == "NSE:AAA:2026-01-01T09:20:00"


def test_successful_exit_intraday_places_mis_order_and_records_everything(tmp_path):
    database, repository = build_repository(tmp_path)
    record = intraday_position()
    repository.save_position(record)
    orders = OrderAPI(TradingMode.PAPER, None)
    orders.paper_protective_orders["PAPER-STOP-000001"] = object()

    outcome = close_position_at_market(
        repository,
        orders,
        record,
        price=105.0,
        timestamp=datetime(2026, 1, 1, 15, 20),
        reason="Manual exit (Live monitor)",
        event_kind="manual_exit",
        decision="MANUAL_EXIT",
    )

    assert outcome == ExitOutcome(
        success=True, symbol="NSE:AAA", order_id="PAPER-000001", exit_price=105.0, pnl=50.0, cancel_error=None
    )
    assert [request.product for request in orders.paper_orders] == ["MIS"]
    assert "PAPER-STOP-000001" not in orders.paper_protective_orders
    assert repository.load_positions() == []

    [trade] = repository.load_trades()
    assert trade.exit_price == 105.0
    assert trade.pnl == 50.0
    assert trade.exit_reason == "Manual exit (Live monitor)"
    assert trade.position_type == "INTRADAY"

    [decision] = repository.load_decisions(symbol="NSE:AAA", event_type="exit")
    assert decision.decision == "MANUAL_EXIT"
    assert decision.rationale == "Manual exit (Live monitor)"
    assert decision.correlation_id == "NSE:AAA:2026-01-01T09:20:00"
    assert decision.outcome["pnl"] == 50.0
    database.close()


def test_successful_exit_swing_places_cnc_order(tmp_path):
    database, repository = build_repository(tmp_path)
    record = intraday_position(position_type="SWING", protective_order_id="PAPER-STOP-000002")
    repository.save_position(record)
    orders = OrderAPI(TradingMode.PAPER, None)
    orders.paper_protective_orders["PAPER-STOP-000002"] = object()

    outcome = close_position_at_market(
        repository,
        orders,
        record,
        price=110.0,
        timestamp=datetime(2026, 1, 1, 15, 20),
        reason="Manual exit (Live monitor)",
        event_kind="manual_exit",
        decision="MANUAL_EXIT",
    )

    assert outcome.success is True
    assert [request.product for request in orders.paper_orders] == ["CNC"]
    [trade] = repository.load_trades()
    assert trade.position_type == "SWING"
    database.close()


def test_market_order_failure_leaves_position_and_stop_untouched(tmp_path):
    database, repository = build_repository(tmp_path)
    record = intraday_position()
    repository.save_position(record)
    orders = OrderAPI(TradingMode.PAPER, None)
    orders.paper_protective_orders["PAPER-STOP-000001"] = object()

    def failing_place(request):
        raise RuntimeError("insufficient margin")

    orders.place = failing_place
    sent_messages: list[str] = []

    class RecordingNotifier:
        def send(self, message: str) -> None:
            sent_messages.append(message)

    outcome = close_position_at_market(
        repository,
        orders,
        record,
        price=105.0,
        timestamp=datetime(2026, 1, 1, 15, 20),
        reason="Manual exit (Live monitor)",
        event_kind="manual_exit",
        decision="MANUAL_EXIT",
        notifier=RecordingNotifier(),
    )

    assert outcome.success is False
    assert "insufficient margin" in outcome.error
    [remaining] = repository.load_positions()
    assert remaining.symbol == "NSE:AAA"
    assert remaining.stop_loss == 90.0
    assert repository.load_trades() == []
    assert repository.load_decisions() == []
    assert "PAPER-STOP-000001" in orders.paper_protective_orders
    assert any("critical_unprotected" in message for message in sent_messages)
    database.close()


def test_cancel_failure_after_successful_exit_still_records_the_trade(tmp_path):
    database, repository = build_repository(tmp_path)
    record = intraday_position()
    repository.save_position(record)
    orders = OrderAPI(TradingMode.PAPER, None)
    # Deliberately do not register the protective order id, so cancel() raises.
    orders.paper_protective_orders.clear()

    def failing_cancel(order_id):
        raise RuntimeError("order not found")

    orders.cancel = failing_cancel

    outcome = close_position_at_market(
        repository,
        orders,
        record,
        price=105.0,
        timestamp=datetime(2026, 1, 1, 15, 20),
        reason="Manual exit (Live monitor)",
        event_kind="manual_exit",
        decision="MANUAL_EXIT",
    )

    assert outcome.success is True
    assert outcome.cancel_error == "order not found"
    assert repository.load_positions() == []
    [trade] = repository.load_trades()
    assert trade.exit_price == 105.0
    database.close()


def test_outcome_backfills_onto_an_earlier_decision_with_the_same_correlation_id(tmp_path):
    database, repository = build_repository(tmp_path)
    record = intraday_position()
    repository.save_position(record)
    correlation_id = correlation_id_for(record)
    repository.save_decision(
        DecisionLogRecord(
            timestamp=datetime(2026, 1, 1, 9, 20),
            symbol="NSE:AAA",
            event_type="entry",
            decision="ENTER",
            rationale="signal fired",
            correlation_id=correlation_id,
        )
    )
    orders = OrderAPI(TradingMode.PAPER, None)
    orders.paper_protective_orders["PAPER-STOP-000001"] = object()

    close_position_at_market(
        repository,
        orders,
        record,
        price=105.0,
        timestamp=datetime(2026, 1, 1, 15, 20),
        reason="Manual exit (Live monitor)",
        event_kind="manual_exit",
        decision="MANUAL_EXIT",
    )

    decisions = repository.load_decisions(correlation_id=correlation_id)
    assert len(decisions) == 2
    assert all(decision.outcome is not None and decision.outcome["pnl"] == 50.0 for decision in decisions)
    database.close()
