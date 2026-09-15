import pytest

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import SignalAction, Side, TradingMode
from app.execution.order_manager import OrderManager
from app.strategy.signal import Signal


class StubKiteClient:
    def __init__(self, instruments=None, protective_rejections=None, instrument_batches=None, margin_response=None):
        self.requests = []
        self.instrument_rows = instruments if instruments is not None else [{"tradingsymbol": "TEST", "tick_size": 0.05}]
        self.instrument_batches = list(instrument_batches or [])
        self.protective_rejections = list(protective_rejections or [])
        self.margin_response = margin_response
        self.margin_requests = []

    def place_order(self, **request):
        self.request = request
        self.requests.append(request)
        if request.get("order_type") == "SL-M" and self.protective_rejections:
            raise RuntimeError(self.protective_rejections.pop(0))
        return "LIVE-000001"

    def instruments(self, exchange):
        if self.instrument_batches:
            return self.instrument_batches.pop(0)
        return self.instrument_rows

    def order_margins(self, params):
        self.margin_requests.append(params)
        return self.margin_response


def test_paper_order_is_recorded_without_broker():
    api = OrderAPI(TradingMode.PAPER)
    manager = OrderManager(api, TradingMode.PAPER)
    signal = Signal("TEST", SignalAction.BUY, __import__("datetime").datetime(2026, 1, 1), 100, 95)
    assert manager.submit_signal(signal, 10) == "PAPER-000001"
    assert api.paper_orders[0].side == SignalAction.BUY


def test_live_order_requires_stop_loss():
    manager = OrderManager(OrderAPI(TradingMode.LIVE), TradingMode.LIVE)
    signal = Signal("TEST", SignalAction.BUY, __import__("datetime").datetime(2026, 1, 1), 100)
    with pytest.raises(ValueError, match="stop loss"):
        manager.submit_signal(signal, 10)


def test_live_market_order_uses_automatic_market_protection():
    client = StubKiteClient()
    api = OrderAPI(TradingMode.LIVE, client)
    manager = OrderManager(api, TradingMode.LIVE)
    signal = Signal("NSE:TEST", SignalAction.SELL, __import__("datetime").datetime(2026, 1, 1), 100, 105)

    assert manager.submit_signal(signal, 10) == "LIVE-000001"
    assert client.request["exchange"] == "NSE"
    assert client.request["tradingsymbol"] == "TEST"
    assert client.request["order_type"] == "MARKET"
    assert client.request["market_protection"] == -1


def test_live_plain_symbol_defaults_to_nse():
    client = StubKiteClient()
    api = OrderAPI(TradingMode.LIVE, client)

    api.place(OrderRequest("TEST", Side.BUY, 1, 100))

    assert client.request["exchange"] == "NSE"
    assert client.request["tradingsymbol"] == "TEST"


def test_live_entry_can_attach_a_broker_side_protective_stop():
    client = StubKiteClient([{"tradingsymbol": "TEST", "tick_size": 0.05}])
    api = OrderAPI(TradingMode.LIVE, client)
    manager = OrderManager(api, TradingMode.LIVE)
    signal = Signal("BSE:TEST", SignalAction.BUY, __import__("datetime").datetime(2026, 1, 1), 100, 95)

    manager.submit_signal(signal, 10)
    stop_id = manager.submit_protective_stop(signal, 10)

    assert stop_id == "LIVE-000001"
    assert client.requests[-1]["exchange"] == "BSE"
    assert client.requests[-1]["tradingsymbol"] == "TEST"
    assert client.requests[-1]["order_type"] == "SL-M"
    assert client.requests[-1]["transaction_type"] == "SELL"
    assert client.requests[-1]["trigger_price"] == 95


def test_live_protective_stop_uses_symbol_tick_size():
    client = StubKiteClient(
        [
            {"tradingsymbol": "ORIENTTECH", "tick_size": 0.01},
            {"tradingsymbol": "SUNTV", "tick_size": 0.05},
            {"tradingsymbol": "VOLTAS", "tick_size": 0.10},
        ]
    )
    api = OrderAPI(TradingMode.LIVE, client)

    api.place_protective_stop(OrderRequest("NSE:ORIENTTECH", Side.SELL, 1, 100, 99.987))
    api.place_protective_stop(OrderRequest("NSE:SUNTV", Side.SELL, 1, 100, 99.987))
    api.place_protective_stop(OrderRequest("NSE:VOLTAS", Side.SELL, 1, 100, 99.987))

    assert client.requests[0]["trigger_price"] == 99.98
    assert client.requests[1]["trigger_price"] == 99.95
    assert client.requests[2]["trigger_price"] == 99.9


def test_short_protective_stop_rounds_up_to_tick_size():
    client = StubKiteClient([{"tradingsymbol": "SUNTV", "tick_size": 0.05}])
    api = OrderAPI(TradingMode.LIVE, client)

    api.place_protective_stop(OrderRequest("NSE:SUNTV", Side.BUY, 1, 100, 100.023))

    assert client.requests[0]["trigger_price"] == 100.05


def test_sell_protective_stop_is_strictly_below_reference_price():
    client = StubKiteClient([{"tradingsymbol": "SHANKESH", "tick_size": 0.01}])
    api = OrderAPI(TradingMode.LIVE, client)

    api.place_protective_stop(OrderRequest("NSE:SHANKESH", Side.SELL, 1, 108.97, 108.97))

    assert client.requests[0]["trigger_price"] == 108.96


def test_protective_stop_retries_one_tick_after_broker_validation_rejection():
    client = StubKiteClient(
        [{"tradingsymbol": "RAYMOND", "tick_size": 0.05}],
        ["Tick size for this script is 0.05. Kindly enter trigger price in the multiple of tick size for this script"],
    )
    api = OrderAPI(TradingMode.LIVE, client)

    api.place_protective_stop(OrderRequest("NSE:RAYMOND", Side.SELL, 1, 100, 99.97))

    assert [request["trigger_price"] for request in client.requests] == [99.95, 99.90]


def test_live_tick_lookup_refreshes_when_symbol_is_missing_from_cached_dump():
    client = StubKiteClient(
        instrument_batches=[
            [{"tradingsymbol": "OTHER", "tick_size": 0.05}],
            [{"tradingsymbol": "PINELABS", "tick_size": 0.01}],
        ]
    )
    api = OrderAPI(TradingMode.LIVE, client)

    api.place_protective_stop(OrderRequest("NSE:PINELABS", Side.SELL, 1, 100, 99.997))

    assert client.requests[0]["trigger_price"] == 99.99


def test_live_protective_stop_fails_closed_when_tick_size_is_unavailable():
    client = StubKiteClient([{"tradingsymbol": "OTHER", "tick_size": 0.05}])
    api = OrderAPI(TradingMode.LIVE, client)

    with pytest.raises(ValueError, match="tick size unavailable"):
        api.place_protective_stop(OrderRequest("NSE:PWL", Side.SELL, 1, 100, 99.997))

    assert client.requests == []


def test_required_intraday_margin_reads_the_brokers_margin_calculator():
    client = StubKiteClient(margin_response=[{"total": 94.13}])
    api = OrderAPI(TradingMode.LIVE, client)

    margin = api.required_intraday_margin("NSE:ZENSARTECH")

    assert margin == 94.13
    [request] = client.margin_requests
    [order] = request
    assert order["exchange"] == "NSE"
    assert order["tradingsymbol"] == "ZENSARTECH"
    assert order["product"] == "MIS"
    assert order["quantity"] == 1


def test_required_intraday_margin_is_none_without_a_live_client():
    api = OrderAPI(TradingMode.PAPER)
    assert api.required_intraday_margin("NSE:ZENSARTECH") is None


def test_required_intraday_margin_is_none_for_a_non_positive_result():
    client = StubKiteClient(margin_response=[{"total": 0}])
    api = OrderAPI(TradingMode.LIVE, client)
    assert api.required_intraday_margin("NSE:ZENSARTECH") is None
