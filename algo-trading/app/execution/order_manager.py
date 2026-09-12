from __future__ import annotations

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side, SignalAction, TradingMode
from app.strategy.signal import Signal


class OrderManager:
    def __init__(self, orders: OrderAPI, mode: TradingMode):
        self.orders = orders
        self.mode = mode

    def submit_signal(self, signal: Signal, quantity: int) -> str:
        if not signal.is_entry:
            raise ValueError("only entry signals can be submitted")
        if self.mode == TradingMode.LIVE and not signal.stop_loss:
            raise ValueError("live entries require a stop loss")
        if self.mode == TradingMode.LIVE:
            self.orders.refresh_tick_size(signal.symbol)
        request = OrderRequest(signal.symbol, signal.action, quantity, signal.price, signal.stop_loss)
        return self.orders.place(request)

    def submit_protective_stop(self, signal: Signal, quantity: int) -> str:
        if signal.stop_loss is None:
            raise ValueError("protective stop requires a stop loss")
        exit_side = Side.SELL if signal.action == SignalAction.BUY else Side.BUY
        return self.orders.place_protective_stop(OrderRequest(signal.symbol, exit_side, quantity, signal.price, signal.stop_loss))
