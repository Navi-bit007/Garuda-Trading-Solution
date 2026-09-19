from __future__ import annotations

import logging
from typing import Any

from app.execution.charges import ChargeBreakdown

logger = logging.getLogger(__name__)


def virtual_order(order_id: str, exchange: str, tradingsymbol: str, transaction_type: str, product: str, quantity: int, price: float) -> dict:
    """One leg of a round trip, in the shape Kite Connect's Charges API
    (`get_virtual_contract_note`, `POST /charges/orders`) expects. `order_id` doesn't need to be
    a real order -- for a hypothetical/virtual order it's just an opaque label; the response
    comes back in the same order as the request, not keyed by this id.
    """
    return {
        "order_id": order_id,
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "transaction_type": transaction_type,
        "variety": "regular",
        "product": product,
        "order_type": "MARKET",
        "quantity": quantity,
        "average_price": price,
    }


def combine_broker_charges(buy_charges: dict, sell_charges: dict, dp_charges: float) -> ChargeBreakdown:
    """Build a `ChargeBreakdown` from Zerodha's own Charges API response for one round trip (a
    buy leg's `charges` dict + a sell leg's). DP (Depository Participant) charges aren't part of
    that response -- they're a settlement-time fee, not an order-level one -- so they're still
    supplied locally (see `app.execution.charges.DELIVERY_DP_CHARGE_PER_SELL`).
    """
    return ChargeBreakdown(
        brokerage=float(buy_charges.get("brokerage", 0)) + float(sell_charges.get("brokerage", 0)),
        stt=float(buy_charges.get("transaction_tax", 0)) + float(sell_charges.get("transaction_tax", 0)),
        transaction_charges=float(buy_charges.get("exchange_turnover_charge", 0)) + float(sell_charges.get("exchange_turnover_charge", 0)),
        sebi_charges=float(buy_charges.get("sebi_turnover_charge", 0)) + float(sell_charges.get("sebi_turnover_charge", 0)),
        stamp_duty=float(buy_charges.get("stamp_duty", 0)) + float(sell_charges.get("stamp_duty", 0)),
        dp_charges=dp_charges,
        gst=float(buy_charges.get("gst", {}).get("total", 0)) + float(sell_charges.get("gst", {}).get("total", 0)),
    )


class ChargesAPI:
    """Wraps Kite Connect's Charges API (`kite.get_virtual_contract_note`) -- Zerodha's own
    per-order brokerage/STT/exchange-transaction/SEBI/stamp-duty/GST calculator, the same one
    used to generate a real contract note. More accurate than
    `app.execution.charges.estimate_equity_charges`, which reimplements the published rate card
    locally and exists as the fallback for whenever this isn't available (no broker session,
    PAPER mode, or the call itself fails).
    """

    def __init__(self, client: Any = None):
        self.client = client

    def fetch(self, orders: list[dict]) -> list[dict] | None:
        """`orders` is a flat list of virtual orders (see `virtual_order`). Returns Zerodha's
        response list, same length and order as the request, or None if there's no client, the
        request list is empty, or the call fails/returns something unexpected -- callers should
        fall back to the local estimate in that case, not raise.
        """
        if self.client is None or not orders:
            return None
        try:
            results = self.client.get_virtual_contract_note(orders)
        except Exception:
            logger.exception("Broker charges lookup failed; falling back to the local estimate")
            return None
        if not isinstance(results, list) or len(results) != len(orders):
            logger.warning("Broker charges response shape unexpected (%d orders, %r back); falling back to the local estimate", len(orders), type(results))
            return None
        return results
