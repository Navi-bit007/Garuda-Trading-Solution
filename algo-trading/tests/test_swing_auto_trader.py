from datetime import datetime

import pandas as pd
import pytest
from kiteconnect.exceptions import InputException

from app.config.constants import TradingMode
from app.execution.swing_auto_trader import SwingAutoTrader
from app.strategy.base import NoSignal
from app.strategy.ema_9_200_swing import Ema9200SwingStrategy


def swing_frame(last_close: float = 130.0, rows: int = 201) -> pd.DataFrame:
    closes = [100.0] * (rows - 1) + [last_close]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2025-01-01", periods=rows, freq="D"),
            "open": closes,
            "high": [value + 2.0 for value in closes],
            "low": [value - 2.0 for value in closes],
            "close": closes,
            "volume": [100_000.0] * rows,
        }
    )


class StubKiteClient:
    def __init__(self, fail_protective_stop: bool = False, fail_market_exit: bool = False, holding_quantities: list[int] | None = None, filled_quantity: int = 7):
        self.requests = []
        self.modifications = []
        self.cancellations = []
        self.instrument_rows = [{"tradingsymbol": "AAA", "tick_size": 0.05}]
        self.fail_protective_stop = fail_protective_stop
        self.fail_market_exit = fail_market_exit
        self.holding_quantities = holding_quantities or [7]
        self.filled_quantity = filled_quantity

    def place_order(self, **request):
        self.requests.append(request)
        if self.fail_protective_stop and request.get("order_type") == "SL-M":
            raise RuntimeError("stop order rejected")
        if self.fail_market_exit and request.get("transaction_type") == "SELL" and request.get("order_type") == "MARKET":
            raise RuntimeError("market exit rejected")
        return f"LIVE-{len(self.requests):06d}"

    def modify_order(self, **request):
        self.modifications.append(request)
        return request["order_id"]

    def cancel_order(self, **request):
        self.cancellations.append(request)
        return request["order_id"]

    def instruments(self, exchange=None):
        return self.instrument_rows

    def positions(self):
        quantity = self.holding_quantities[0] if len(self.holding_quantities) == 1 else self.holding_quantities.pop(0)
        return {"net": [{"tradingsymbol": "AAA", "quantity": quantity}]}

    def order_history(self, order_id):
        return [{"status": "COMPLETE", "filled_quantity": self.filled_quantity}]


def test_swing_strategy_requires_a_fresh_daily_ema9_cross():
    strategy = Ema9200SwingStrategy()
    evaluation = strategy.evaluate("AAA", swing_frame(), 1)

    assert evaluation.signal.side == "BUY"
    assert evaluation.signal.timestamp == datetime(2025, 7, 20)
    assert evaluation.ema9 > evaluation.ema200
    assert evaluation.signal.metadata["timeframe"] == "day"

    with pytest.raises(NoSignal, match="freshly cross"):
        strategy.generate_signal("AAA", swing_frame(last_close=100.0))


def test_swing_trader_places_cnc_entry_and_zerodha_side_stop_then_trails():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    result = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame())

    assert len(result.candidates) == 1
    order_result = trader.submit_candidate(result.candidates[0], amount_limit=1_000, quantity_limit=10)

    assert order_result.status == "submitted"
    assert order_result.quantity == 7
    assert client.requests[0]["product"] == "CNC"
    assert client.requests[0]["exchange"] == "NSE"
    assert client.requests[0]["order_type"] == "MARKET"
    assert client.requests[1]["product"] == "CNC"
    assert client.requests[1]["exchange"] == "NSE"
    assert client.requests[1]["order_type"] == "SL-M"
    assert client.requests[1]["market_protection"] == -1
    assert client.requests[1]["tradingsymbol"] == "AAA"

    trailing_frame = swing_frame(last_close=130.0, rows=202)
    trailing_frame.loc[201, "close"] = 150.0
    trailing_frame.loc[201, "open"] = 150.0
    trailing_frame.loc[201, "high"] = 152.0
    trailing_frame.loc[201, "low"] = 148.0
    new_stop = trader.trail_position("NSE:AAA", trailing_frame)

    assert new_stop is not None
    assert client.modifications[0]["order_id"] == order_result.protective_order_id
    assert client.modifications[0]["product"] == "CNC"
    assert client.modifications[0]["order_type"] == "SL-M"
    assert client.modifications[0]["market_protection"] == -1
    assert client.modifications[0]["trigger_price"] == new_stop


def test_swing_trader_caps_quantity_by_amount():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    result = trader.submit_candidate(candidate, amount_limit=100, quantity_limit=100)

    assert result.status == "rejected"
    assert result.reason == "amount limit is smaller than one share"
    assert client.requests == []


def test_swing_trader_flattens_holding_when_broker_stop_is_rejected():
    client = StubKiteClient(fail_protective_stop=True)
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    result = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)

    assert result.status == "flattened_after_protective_stop_failure"
    assert "automatic SELL submitted" in result.reason
    assert client.requests[0]["transaction_type"] == "BUY"
    assert client.requests[0]["order_type"] == "MARKET"
    assert client.requests[1]["transaction_type"] == "SELL"
    assert client.requests[1]["order_type"] == "SL-M"
    assert client.requests[2]["transaction_type"] == "SELL"
    assert client.requests[2]["order_type"] == "MARKET"
    assert "NSE:AAA" not in trader.active_positions


def test_swing_trader_waits_for_holding_before_submitting_stop():
    client = StubKiteClient(holding_quantities=[0, 7])
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    trader.ENTRY_FILL_POLL_SECONDS = 0
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    result = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)

    assert result.status == "submitted"
    assert [request["order_type"] for request in client.requests] == ["MARKET", "SL-M"]
    assert client.requests[1]["quantity"] == 7


def test_swing_trend_exit_cancels_broker_protective_stop():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]
    entry = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)
    assert entry.status == "submitted"
    trader.active_positions["NSE:AAA"].strategy_name = "SWING_TREND_BREAKOUT"

    exit_close = trader.active_positions["NSE:AAA"].stop_loss + 1.0
    exit_frame = swing_frame(last_close=exit_close, rows=202)
    exit_frame.loc[:200, ["open", "high", "low", "close"]] = [exit_close + 10.0, exit_close + 12.0, exit_close + 8.0, exit_close + 10.0]
    exit_frame.loc[201, ["open", "high", "low", "close"]] = [exit_close, exit_close + 2.0, exit_close - 2.0, exit_close]

    result = trader.manage_position("NSE:AAA", exit_frame)

    assert result is not None
    assert result.status == "exited"
    assert client.cancellations == [{"variety": "regular", "order_id": entry.protective_order_id}]
    assert "NSE:AAA" not in trader.active_positions


def test_swing_trader_keeps_pending_entry_retryable_until_holding_is_confirmed():
    client = StubKiteClient(holding_quantities=[0], filled_quantity=0)
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    trader.ENTRY_FILL_TIMEOUT_SECONDS = 0
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    first = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)
    second = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)

    assert first.status == "entry_pending"
    assert second.status == "entry_pending"
    assert len(client.requests) == 1
    assert "NSE:AAA" not in trader.active_positions


def test_swing_trader_reports_critical_state_when_flattening_also_fails():
    client = StubKiteClient(fail_protective_stop=True, fail_market_exit=True)
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    result = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)

    assert result.status == "critical_unprotected"
    assert "automatic SELL also failed" in result.reason
    assert trader.active_positions["NSE:AAA"].protective_order_id is None


def test_swing_scan_isolates_broker_rate_limit_errors():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)

    def rate_limited_loader(token):
        raise Exception("Too many requests")

    result = trader.scan({"NSE:AAA": 1}, rate_limited_loader)

    assert result.candidates == ()
    assert result.errors == ("NSE:AAA: Too many requests",)


def test_swing_scan_isolates_invalid_instrument_token():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)

    def load_candles(token):
        if token == 2:
            raise InputException("invalid token")
        return swing_frame()

    result = trader.scan({"NSE:AAA": 1, "NSE:BAD": 2}, load_candles)

    assert [candidate.symbol for candidate in result.candidates] == ["NSE:AAA"]
    assert result.errors == ("NSE:BAD: invalid token",)


def test_swing_scan_skips_symbols_without_ema_warmup_history():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)

    result = trader.scan({"NSE:NEWSTOCK": 1}, lambda token: swing_frame(rows=200))

    assert result.candidates == ()
    assert result.errors == ()
    assert result.insufficient_history == ("NSE:NEWSTOCK",)


def test_stop_prices_are_rounded_down_to_the_instrument_tick():
    assert SwingAutoTrader._round_down_to_tick(123.47, 0.05) == 123.45
    assert SwingAutoTrader._round_down_to_tick(123.479, 0.01) == 123.47


def test_repeated_scan_does_not_submit_the_same_signal_twice():
    client = StubKiteClient()
    trader = SwingAutoTrader(client, TradingMode.LIVE)
    candidate = trader.scan({"NSE:AAA": 1}, lambda token: swing_frame()).candidates[0]

    first = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)
    second = trader.submit_candidate(candidate, amount_limit=1_000, quantity_limit=10)

    assert first.status == "submitted"
    assert second.status == "skipped"
    assert second.reason == "position is already open"
    assert len(client.requests) == 2
