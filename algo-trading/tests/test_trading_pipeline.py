from datetime import datetime, time

import pytest
import pandas as pd

from app.config.constants import SignalAction, TradingMode
from app.config.settings import Settings
from app.database.database import Database
from app.database.models import PositionRecord
from app.database.repository import Repository
from app.execution.trading_pipeline import TradingPipeline
from app.market.candles import TickCandleBuilder
from app.strategy.base import NoSignal
from app.strategy.signal import Signal


class BuyStrategy:
    atr_period = 2
    stop_atr = 1.0

    def generate_signal(self, symbol, candles):
        latest = candles.iloc[-1]
        return Signal(symbol, SignalAction.BUY, latest["timestamp"].to_pydatetime(), float(latest["close"]), float(latest["close"] - 5), "test buy")


class PreSpikeEntryStrategy(BuyStrategy):
    name = "PRE_SPIKE_MOMENTUM"


class NamedBuyStrategy(BuyStrategy):
    name = "VWAP EMA breakout"


class NoSetupStrategy:
    name = "NO_SETUP"

    def generate_signal(self, symbol, candles):
        raise NoSignal("conditions not met")


class BrokerExitClient:
    def __init__(self, broker_quantity: int = 0, broker_average_price: float | None = None):
        self.requests = []
        self.modifications = []
        self.broker_quantity = broker_quantity
        self.broker_average_price = broker_average_price

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        self.requests.append(request)
        return f"ORDER-{len(self.requests)}"

    def modify_order(self, **request):
        self.modifications.append(request)
        return request.get("order_id")

    def positions(self):
        if self.broker_quantity == 0:
            return {"net": []}
        position = {"tradingsymbol": "AAA", "quantity": self.broker_quantity}
        if self.broker_average_price is not None:
            position["average_price"] = self.broker_average_price
        return {"net": [position]}

    def order_history(self, order_id):
        # ORDER-1 is always the entry order in these tests; fill it at the exact signal price
        # (no slippage) so entry-price assertions stay stable. Every later order id (protective
        # stop, exit) fills at 94.5, matching the exit-price assertions that already exist.
        if order_id == "ORDER-1":
            return [{"status": "COMPLETE", "filled_quantity": 10, "average_price": 100.0}]
        return [{"status": "COMPLETE", "filled_quantity": 10, "average_price": 94.5}]


class RejectedAfterAcceptClient:
    """Simulates Zerodha accepting an order over HTTP (returns an order id) and only rejecting
    it moments later, asynchronously -- the scenario behind the 2026-09-11 incident where
    entry_submitted was logged in the activity ledger with no real position at the broker."""

    def __init__(self):
        self.requests = []
        self.cancelled_order_ids = []

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        self.requests.append(request)
        return f"ORDER-{len(self.requests)}"

    def cancel_order(self, **kwargs):
        self.cancelled_order_ids.append(kwargs.get("order_id"))

    def order_history(self, order_id):
        return [{"status": "REJECTED", "filled_quantity": 0, "average_price": 0}]


class SlippedFillClient:
    """Simulates a MARKET order filling away from the last-tick price the signal was priced
    at -- the scenario behind the monitoring page showing an entry price that didn't match
    what Zerodha actually filled the order at."""

    def __init__(self):
        self.requests = []

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        self.requests.append(request)
        return f"ORDER-{len(self.requests)}"

    def order_history(self, order_id):
        return [{"status": "COMPLETE", "filled_quantity": 10, "average_price": 100.35}]


class LeveragedMarginClient:
    """Simulates Zerodha's margin calculator granting real per-stock MIS leverage."""

    def __init__(self, margin_per_share: float):
        self.requests = []
        self.margin_per_share = margin_per_share

    def instruments(self, exchange=None):
        return [{"tradingsymbol": "AAA", "tick_size": 0.05}]

    def place_order(self, **request):
        self.requests.append(request)
        return f"ORDER-{len(self.requests)}"

    def order_history(self, order_id):
        return [{"status": "COMPLETE", "filled_quantity": 10, "average_price": 100.0}]

    def order_margins(self, params):
        return [{"total": self.margin_per_share}]


def build_settings(**overrides):
    values = {"trading_mode": TradingMode.PAPER, "force_exit": time(15, 15)}
    values.update(overrides)
    return Settings(**values)


def test_tick_builder_emits_only_completed_candles():
    builder = TickCandleBuilder()
    assert builder.update({"timestamp": datetime(2026, 1, 1, 9, 15, 10), "last_price": 100, "volume_traded": 100}) is None
    assert builder.update({"timestamp": datetime(2026, 1, 1, 9, 15, 40), "last_price": 102, "volume_traded": 130}) is None
    completed = builder.update({"timestamp": datetime(2026, 1, 1, 9, 16), "last_price": 101, "volume_traded": 150})
    assert completed == {"timestamp": datetime(2026, 1, 1, 9, 15), "open": 100.0, "high": 102.0, "low": 100.0, "close": 102.0, "volume": 30.0}


def test_pipeline_submits_buy_and_trailing_sell_in_paper_mode():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    entry_events = pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 100, "volume_traded": 150},
    ])
    assert [event.kind for event in entry_events] == ["entry_submitted"]
    assert pipeline.orders.paper_orders[0].side == SignalAction.BUY

    exit_events = pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 22), "last_price": 94, "volume_traded": 180},
    ])
    assert [event.kind for event in exit_events] == ["exit_submitted"]
    assert pipeline.orders.paper_orders[1].side.value == "SELL"
    assert not pipeline.managed_positions


def test_pipeline_ignores_malformed_ticks_before_scanning():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    events = pipeline.on_ticks([
        {"instrument_token": "invalid", "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": "bad"},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 100, "volume_traded": 150},
    ])

    assert [event.kind for event in events] == ["entry_submitted"]


def test_pipeline_treats_no_signal_as_a_skipped_evaluation():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, NoSetupStrategy())

    events = pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 100, "volume_traded": 150},
    ])

    assert [event.kind for event in events] == ["signal_skipped"]
    assert events[0].reason == "conditions not met"


def test_pipeline_ignores_ticks_outside_nifty500_universe():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    events = pipeline.on_ticks([
        {"instrument_token": 999, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 999, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 101, "volume_traded": 120},
    ])
    assert events == []
    assert pipeline.orders.paper_orders == []


def test_pipeline_force_exits_at_configured_time():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 100, "volume_traded": 150},
    ])
    events = pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 15, 15), "last_price": 101, "volume_traded": 180},
    ])
    assert [event.kind for event in events] == ["exit_submitted"]
    assert events[0].reason == "configured force exit"


def test_live_pipeline_requires_authenticated_broker_client():
    with pytest.raises(ValueError, match="authenticated broker client"):
        TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy())


def test_pipeline_moves_broker_sold_position_to_recently_closed_with_realized_pnl():
    client = BrokerExitClient()
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client)

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)
    events = pipeline.sync_broker_positions()

    assert entry.kind == "entry_submitted"
    assert [event.kind for event in events] == ["broker_exit_detected"]
    assert events[0].price == 94.5
    assert events[0].pnl == -55.0
    assert not pipeline.managed_positions
    assert pipeline.recent_closed_positions[0].exit_price == 94.5
    assert pipeline.recent_closed_positions[0].pnl == -55.0


def test_entry_rejected_when_broker_accepts_then_rejects_the_order(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    client = RejectedAfterAcceptClient()
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client, repository)

    event = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)

    assert event.kind == "entry_rejected"
    assert "rejected" in event.reason.lower()
    assert not pipeline.managed_positions
    # The entry order is ORDER-1; the protective stop submitted right after it is ORDER-2, and
    # that resting protective stop must be cancelled once the entry itself turns out rejected.
    assert client.cancelled_order_ids == ["ORDER-2"]
    rows = database.connection.execute("SELECT event_kind FROM activity ORDER BY id").fetchall()
    assert [row[0] for row in rows] == ["entry_rejected"]
    database.close()


def test_entry_price_reflects_broker_fill_price_not_signal_price(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    client = SlippedFillClient()
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client, repository)

    event = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)

    assert event.kind == "entry_submitted"
    assert event.price == 100.35
    assert event.entry_price == 100.35
    assert pipeline.managed_positions["AAA"].position.entry_price == 100.35
    [saved] = repository.load_positions()
    assert saved.entry_price == 100.35
    database.close()


def test_sync_broker_positions_corrects_a_stale_entry_price(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    client = BrokerExitClient(broker_quantity=10, broker_average_price=101.75)
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client, repository)
    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)
    assert entry.entry_price == 100.0

    events = pipeline.sync_broker_positions()

    assert events == []
    assert pipeline.managed_positions["AAA"].position.entry_price == 101.75
    [saved] = repository.load_positions()
    assert saved.entry_price == 101.75
    database.close()


def test_live_entry_quantity_uses_real_time_broker_leverage():
    # Price 100, margin 20 per share -> Zerodha is granting 5x leverage for this stock right now.
    client = LeveragedMarginClient(margin_per_share=20.0)
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client)

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    assert entry.kind == "entry_submitted"
    # 100_000 * 0.80 deployment * 5x leverage / 100 price == 4_000, not the unleveraged 800.
    assert pipeline.managed_positions["AAA"].position.quantity == 4_000


def test_live_entry_quantity_falls_back_to_configured_leverage_when_lookup_fails():
    class BrokenMarginClient(LeveragedMarginClient):
        def order_margins(self, params):
            raise RuntimeError("margin API unavailable")

    client = BrokenMarginClient(margin_per_share=20.0)
    pipeline = TradingPipeline(
        build_settings(trading_mode=TradingMode.LIVE, intraday_leverage_multiplier=2.0),
        {1: "AAA"},
        BuyStrategy(),
        client,
    )

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    assert entry.kind == "entry_submitted"
    # Live margin lookup failed, so this falls back to the configured 2x multiplier:
    # 100_000 * 0.80 * 2 / 100 == 1_600.
    assert pipeline.managed_positions["AAA"].position.quantity == 1_600


def test_paper_mode_entry_quantity_is_unchanged_with_default_leverage():
    # Default intraday_leverage_multiplier is 1x, so plain PAPER sizing is untouched unless a
    # user explicitly configures a higher fallback.
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    assert entry.kind == "entry_submitted"
    assert pipeline.managed_positions["AAA"].position.quantity == 800


def test_paper_mode_entry_quantity_applies_the_configured_fallback_leverage():
    # PAPER mode has no broker to ask for real leverage, so it simulates using the same
    # configured fallback multiplier LIVE would use if the live lookup failed -- this way
    # paper trading exercises the same sizing behavior a user intends to run live.
    pipeline = TradingPipeline(build_settings(intraday_leverage_multiplier=5.0), {1: "AAA"}, BuyStrategy())

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    assert entry.kind == "entry_submitted"
    assert pipeline.managed_positions["AAA"].position.quantity == 4_000


def test_intraday_position_record_persists_the_strategy_name(tmp_path):
    """The Live monitor page's "Strategy" column reads PositionRecord.strategy_name -- without
    this, every intraday position showed a blank "-" regardless of which strategy opened it."""
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, NamedBuyStrategy(), activity_repository=repository)

    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)

    assert entry.kind == "entry_submitted"
    [saved] = repository.load_positions()
    assert saved.strategy_name == "VWAP EMA breakout"
    database.close()


def test_live_pipeline_does_not_auto_close_on_trailing_stop_breach():
    client = BrokerExitClient(broker_quantity=10)
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client)
    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)
    assert entry.kind == "entry_submitted"

    events = pipeline.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 90}])

    assert events == []
    assert "AAA" in pipeline.managed_positions


def test_live_pipeline_breakeven_flags_target_1_hit_without_modifying_broker_stop(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    client = BrokerExitClient(broker_quantity=10)
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client, repository)
    signal = Signal(
        "AAA",
        SignalAction.BUY,
        datetime(2026, 1, 1, 9, 25),
        100,
        95,
        "high conviction",
        score=95,
        target_1=101,
        target_2=102,
        metadata={"move_stop_to_breakeven_after_target_1": True},
    )
    assert pipeline.submit_strategy_entry(signal, quantity=10).kind == "entry_submitted"

    events = pipeline.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 101}])

    assert [event.kind for event in events] == ["breakeven_activated"]
    assert pipeline.managed_positions["AAA"].position.stop_loss == 100
    assert pipeline.managed_positions["AAA"].target_1_hit is True
    assert client.modifications == []
    [saved] = repository.load_positions()
    assert saved.target_1_hit is True
    assert saved.stop_loss == 100
    database.close()


def test_pipeline_rejects_entry_when_deployment_size_is_zero():
    pipeline = TradingPipeline(build_settings(max_capital_deployment=0.0000001), {1: "AAA"}, BuyStrategy())
    events = pipeline.on_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 20), "last_price": 100, "volume_traded": 100},
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 21), "last_price": 100, "volume_traded": 150},
    ])
    assert [event.kind for event in events] == ["entry_rejected"]
    assert pipeline.orders.paper_orders == []


@pytest.mark.parametrize(
    ("price", "stop_loss", "quantity", "reason"),
    [
        (0, 95, None, "entry price must be positive (received 0)"),
        (100, 0, None, "stop loss must be positive (received 0)"),
        (100, 95, 0, "quantity must be positive (requested 0)"),
    ],
)
def test_pipeline_rejects_malformed_strategy_entries(price, stop_loss, quantity, reason):
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    signal = Signal("AAA", SignalAction.BUY, datetime(2026, 1, 1, 9, 25), price, stop_loss, "malformed signal")

    event = pipeline.submit_strategy_entry(signal, quantity=quantity)

    assert event.kind == "entry_rejected"
    assert event.reason == reason
    assert pipeline.orders.paper_orders == []


def test_manual_entry_is_deployment_sized_and_monitor_closes_position():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    entry = pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    assert entry.kind == "entry_submitted"
    assert pipeline.managed_positions["AAA"].position.quantity == 800

    exits = pipeline.monitor_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 94},
    ])

    assert [event.kind for event in exits] == ["exit_submitted"]
    assert exits[0].reason == "trailing stop"
    assert not pipeline.managed_positions


def test_app_initiated_exit_persists_trade_history(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)
    pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25))

    exits = pipeline.monitor_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 94},
    ])

    assert [event.kind for event in exits] == ["exit_submitted"]
    [trade] = repository.load_trades()
    assert trade.symbol == "AAA"
    assert trade.entry_price == 100
    assert trade.exit_price == 94
    assert trade.exit_reason == "trailing stop"
    assert trade.position_type == "INTRADAY"
    database.close()


def test_broker_detected_exit_persists_trade_history(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    client = BrokerExitClient()
    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), client, repository)
    pipeline.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)

    events = pipeline.sync_broker_positions()

    assert [event.kind for event in events] == ["broker_exit_detected"]
    [trade] = repository.load_trades()
    assert trade.symbol == "AAA"
    assert trade.exit_price == 94.5
    assert "broker-side position closed" in trade.exit_reason
    database.close()


def test_manual_entry_rejects_stop_above_entry():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())

    event = pipeline.submit_manual_entry("AAA", 100, 101)

    assert event.kind == "entry_rejected"
    assert pipeline.orders.paper_orders == []


def test_manual_entry_rejects_quantity_above_deployment_limit():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())

    event = pipeline.submit_manual_entry("AAA", 100, 95, quantity=801)

    assert event.kind == "entry_rejected"
    assert "capital deployment limit" in event.reason
    assert pipeline.orders.paper_orders == []


def test_manual_sell_tracks_short_position_and_closes_with_buy():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    entry = pipeline.submit_manual_entry("AAA", 100, 105, quantity=10, action=SignalAction.SELL)

    assert entry.kind == "entry_submitted"
    assert pipeline.managed_positions["AAA"].position.side.value == "SELL"
    assert pipeline.orders.paper_orders[0].side.value == "SELL"

    exits = pipeline.monitor_ticks([
        {"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 106},
    ])

    assert [event.kind for event in exits] == ["exit_submitted"]
    assert pipeline.orders.paper_orders[1].side.value == "BUY"


def test_manual_exit_closes_open_position_with_opposite_side():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    pipeline.submit_manual_entry("AAA", 100, 95, quantity=10)

    event = pipeline.submit_manual_exit("AAA", 102)

    assert event.kind == "exit_submitted"
    assert event.reason == "manual exit"
    assert pipeline.orders.paper_orders[1].side.value == "SELL"
    assert not pipeline.managed_positions


def test_exit_submits_market_order_before_canceling_protective_stop(monkeypatch):
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    pipeline.submit_manual_entry("AAA", 100, 95, quantity=10)

    def fail_cancel(order_id):
        raise RuntimeError("cancel rejected")

    monkeypatch.setattr(pipeline.orders, "cancel", fail_cancel)
    event = pipeline.submit_manual_exit("AAA", 102)

    assert event.kind == "critical_unprotected"
    assert "protective stop cancellation failed" in event.reason
    assert pipeline.orders.paper_orders[-1].side == SignalAction.SELL
    assert not pipeline.managed_positions


def test_pipeline_restores_open_positions_from_repository(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    first = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)
    first.submit_manual_entry("AAA", 100, 95, datetime(2026, 1, 1, 9, 25), quantity=10)

    restored = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)

    assert restored.managed_positions["AAA"].position.quantity == 10
    assert restored.managed_positions["AAA"].position.stop_loss == 95
    database.close()


def test_pipeline_never_adopts_a_swing_position_sharing_its_watchlist_symbol(tmp_path):
    """A SWING position (persisted by SwingAutoTrader) can share a symbol with the intraday
    watchlist. Restoring it into this pipeline's own managed_positions would make intraday
    treat it as its own -- corrupting the record the next time anything re-saves it (defaulting
    position_type back to INTRADAY, overwriting strategy_name with whatever intraday strategy
    is selected) and double-counting it against intraday's own capital/position limits. This is
    exactly what happened to a real NSE:JUBLPHARMA swing position."""
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    repository.save_position(
        PositionRecord(
            symbol="AAA",
            side="BUY",
            quantity=1,
            entry_price=1018.4,
            stop_loss=961.8,
            entry_time=datetime(2026, 9, 11),
            protective_order_id="260915190379505",
            position_type="SWING",
            strategy_name="EMA 9/200 swing",
            atr_multiplier=2.0,
            trading_mode="LIVE",
        )
    )

    pipeline = TradingPipeline(build_settings(trading_mode=TradingMode.LIVE), {1: "AAA"}, BuyStrategy(), broker_client=object(), activity_repository=repository)

    assert "AAA" not in pipeline.managed_positions
    [saved] = repository.load_positions()
    assert saved.position_type == "SWING"
    assert saved.strategy_name == "EMA 9/200 swing"
    database.close()


def test_pipeline_blocks_a_submitted_strategy_signal_after_position_is_closed(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)
    timestamp = datetime(2026, 1, 1, 9, 25)
    signal = Signal("AAA", SignalAction.BUY, timestamp, 100, 95, "strong signal", score=80)

    assert pipeline.submit_strategy_entry(signal, quantity=10).kind == "entry_submitted"
    assert pipeline.submit_manual_exit("AAA", 102, timestamp).kind == "exit_submitted"
    duplicate = pipeline.submit_strategy_entry(signal, quantity=10)

    assert duplicate.kind == "entry_skipped"
    assert "already submitted" in duplicate.reason
    database.close()


def test_emergency_halt_blocks_new_entries():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    pipeline.halt_entries()

    rejected = pipeline.submit_manual_entry("AAA", 100, 95, quantity=10)

    assert rejected.kind == "entry_rejected"
    assert "halted" in rejected.reason


def test_target_one_closes_a_position_before_the_trailing_stop():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    signal = Signal("AAA", SignalAction.BUY, datetime(2026, 1, 1, 9, 25), 100, 95, "strong signal", score=80, target_1=105)

    assert pipeline.submit_strategy_entry(signal, quantity=10).kind == "entry_submitted"
    exits = pipeline.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 105}])

    assert exits[0].kind == "exit_submitted"
    assert exits[0].reason == "target 1 reached"


def test_high_conviction_target_one_moves_stop_to_breakeven_and_target_two_exits():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy())
    signal = Signal(
        "AAA",
        SignalAction.BUY,
        datetime(2026, 1, 1, 9, 25),
        100,
        95,
        "high conviction",
        score=95,
        target_1=101,
        target_2=102,
        metadata={"move_stop_to_breakeven_after_target_1": True},
    )

    assert pipeline.submit_strategy_entry(signal, quantity=10).kind == "entry_submitted"
    breakeven = pipeline.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 101}])

    assert [event.kind for event in breakeven] == ["breakeven_activated"]
    assert pipeline.managed_positions["AAA"].position.stop_loss == 100
    protective_order_id = pipeline.managed_positions["AAA"].protective_order_id
    assert pipeline.orders.paper_protective_orders[protective_order_id].stop_loss == 100

    exits = pipeline.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 27), "last_price": 102}])

    assert [event.kind for event in exits] == ["exit_submitted"]
    assert exits[0].reason == "target 2 reached"
    assert not pipeline.managed_positions


def test_high_conviction_target_one_milestone_restores_from_repository(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    first = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)
    signal = Signal(
        "AAA",
        SignalAction.BUY,
        datetime(2026, 1, 1, 9, 25),
        100,
        95,
        "high conviction",
        target_1=101,
        target_2=102,
        metadata={"move_stop_to_breakeven_after_target_1": True},
    )
    first.submit_strategy_entry(signal, quantity=10)
    first.monitor_ticks([{"instrument_token": 1, "timestamp": datetime(2026, 1, 1, 9, 26), "last_price": 101}])

    restored = TradingPipeline(build_settings(), {1: "AAA"}, BuyStrategy(), activity_repository=repository)

    assert restored.managed_positions["AAA"].target_1_hit is True
    assert restored.managed_positions["AAA"].position.stop_loss == 100
    database.close()


def test_pre_spike_entry_uses_requested_quantity_and_ema9_candle_exit():
    pipeline = TradingPipeline(build_settings(), {1: "AAA"}, PreSpikeEntryStrategy())
    signal = Signal("AAA", SignalAction.BUY, datetime(2026, 1, 1, 9, 25), 100, 95, "strong signal", score=80)

    entry = pipeline.submit_strategy_entry(signal, quantity=7)

    assert entry.kind == "entry_submitted"
    assert pipeline.managed_positions["AAA"].position.quantity == 7
    candles = [
        {
            "timestamp": datetime(2026, 1, 1, 9, 30 + index),
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 100,
        }
        for index, close in enumerate([100.0] * 9 + [98.0])
    ]

    exits = pipeline.monitor_candle_closes({"AAA": pd.DataFrame(candles)})

    assert [event.kind for event in exits] == ["exit_submitted"]
    assert exits[0].reason == "candle close below EMA9"
    assert exits[0].price == 98.0
    assert not pipeline.managed_positions
