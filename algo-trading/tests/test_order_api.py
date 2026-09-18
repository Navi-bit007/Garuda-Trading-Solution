from app.broker.order_api import OrderAPI
from app.config.constants import TradingMode


class StubMarginsClient:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.requested_segments: list[str] = []

    def margins(self, segment):
        self.requested_segments.append(segment)
        if self.error is not None:
            raise self.error
        return self.response


def test_available_margin_is_none_without_a_client():
    orders = OrderAPI(TradingMode.PAPER, None)

    assert orders.available_margin() is None


def test_available_margin_is_none_when_client_has_no_margins_method():
    orders = OrderAPI(TradingMode.LIVE, object())

    assert orders.available_margin() is None


def test_available_margin_reads_the_equity_segments_net_figure():
    client = StubMarginsClient(response={"net": 12345.67, "available": {"cash": 99999.0}})
    orders = OrderAPI(TradingMode.LIVE, client)

    assert orders.available_margin() == 12345.67
    assert client.requested_segments == ["equity"]


def test_available_margin_returns_none_when_the_broker_call_fails():
    client = StubMarginsClient(error=RuntimeError("network error"))
    orders = OrderAPI(TradingMode.LIVE, client)

    assert orders.available_margin() is None


def test_available_margin_returns_none_on_an_unexpected_response_shape():
    client = StubMarginsClient(response={"available": {"cash": 5000.0}})  # missing "net"
    orders = OrderAPI(TradingMode.LIVE, client)

    assert orders.available_margin() is None
