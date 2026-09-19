import pytest

from app.broker.charges_api import ChargesAPI, combine_broker_charges, virtual_order


def test_virtual_order_shape():
    order = virtual_order("buy-0", "NSE", "INFY", "BUY", "CNC", 10, 1500.0)

    assert order == {
        "order_id": "buy-0",
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "variety": "regular",
        "product": "CNC",
        "order_type": "MARKET",
        "quantity": 10,
        "average_price": 1500.0,
    }


def test_combine_broker_charges_sums_both_legs_and_adds_dp_charges():
    buy_charges = {"brokerage": 0.0, "transaction_tax": 0.0, "exchange_turnover_charge": 1.0, "sebi_turnover_charge": 0.1, "stamp_duty": 1.5, "gst": {"total": 0.2}}
    sell_charges = {"brokerage": 0.0, "transaction_tax": 21.0, "exchange_turnover_charge": 1.1, "sebi_turnover_charge": 0.1, "stamp_duty": 0.0, "gst": {"total": 0.22}}

    breakdown = combine_broker_charges(buy_charges, sell_charges, dp_charges=15.34)

    assert breakdown.brokerage == 0.0
    assert breakdown.stt == pytest.approx(21.0)
    assert breakdown.transaction_charges == pytest.approx(2.1)
    assert breakdown.sebi_charges == pytest.approx(0.2)
    assert breakdown.stamp_duty == pytest.approx(1.5)
    assert breakdown.dp_charges == 15.34
    assert breakdown.gst == pytest.approx(0.42)


def test_charges_api_returns_none_without_a_client():
    api = ChargesAPI(client=None)

    assert api.fetch([virtual_order("buy-0", "NSE", "INFY", "BUY", "CNC", 10, 1500.0)]) is None


def test_charges_api_returns_none_for_an_empty_order_list():
    api = ChargesAPI(client=object())

    assert api.fetch([]) is None


def test_charges_api_returns_none_when_the_broker_call_raises():
    class BrokenClient:
        def get_virtual_contract_note(self, orders):
            raise RuntimeError("boom")

    api = ChargesAPI(client=BrokenClient())

    assert api.fetch([virtual_order("buy-0", "NSE", "INFY", "BUY", "CNC", 10, 1500.0)]) is None


def test_charges_api_returns_none_when_response_length_does_not_match():
    class MismatchedClient:
        def get_virtual_contract_note(self, orders):
            return [{"charges": {}}]  # only one result for two requested orders

    api = ChargesAPI(client=MismatchedClient())
    orders = [
        virtual_order("buy-0", "NSE", "INFY", "BUY", "CNC", 10, 1500.0),
        virtual_order("sell-0", "NSE", "INFY", "SELL", "CNC", 10, 1550.0),
    ]

    assert api.fetch(orders) is None


def test_charges_api_returns_results_on_success():
    class WorkingClient:
        def get_virtual_contract_note(self, orders):
            return [{"charges": {"brokerage": 0.0}}, {"charges": {"brokerage": 0.0}}]

    api = ChargesAPI(client=WorkingClient())
    orders = [
        virtual_order("buy-0", "NSE", "INFY", "BUY", "CNC", 10, 1500.0),
        virtual_order("sell-0", "NSE", "INFY", "SELL", "CNC", 10, 1550.0),
    ]

    results = api.fetch(orders)

    assert results == [{"charges": {"brokerage": 0.0}}, {"charges": {"brokerage": 0.0}}]
