from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from app.config.constants import NSE_TICK_SIZE, Side, TradingMode


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: int
    price: float
    stop_loss: float | None = None
    product: str = "MIS"
    exchange: str = "NSE"


class OrderAPI:
    def __init__(self, mode: TradingMode, client: Any = None):
        self.mode = mode
        self.client = client
        self.paper_orders: list[OrderRequest] = []
        self.paper_protective_orders: dict[str, OrderRequest] = {}
        self._tick_sizes: dict[str, float] = {}
        self._loaded_tick_exchanges: set[str] = set()

    def place(self, request: OrderRequest) -> str:
        if request.quantity <= 0 or request.price <= 0:
            raise ValueError("order quantity and price must be positive")
        if self.mode == TradingMode.PAPER:
            self.paper_orders.append(request)
            return f"PAPER-{len(self.paper_orders):06d}"
        if self.client is None:
            raise RuntimeError("live orders require a connected Kite client")
        exchange, tradingsymbol = self._split_symbol(request.symbol, request.exchange)
        return self.client.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=request.side.value,
            quantity=request.quantity,
            product=request.product,
            order_type="MARKET",
            market_protection=-1,
        )

    def place_protective_stop(self, request: OrderRequest) -> str:
        if request.quantity <= 0 or request.stop_loss is None or request.stop_loss <= 0:
            raise ValueError("protective stop requires positive quantity and trigger price")
        request = self._normalise_protective_stop(request)
        if self.mode == TradingMode.PAPER:
            order_id = f"PAPER-STOP-{len(self.paper_protective_orders) + 1:06d}"
            self.paper_protective_orders[order_id] = request
            return order_id
        if self.client is None:
            raise RuntimeError("live protective stops require a connected Kite client")
        try:
            return self._place_live_protective_stop(request)
        except Exception as error:
            if not self._is_protective_trigger_rejection(error):
                raise
            retry = self._normalise_protective_stop(request, safety_ticks=1)
            return self._place_live_protective_stop(retry)

    def modify_protective_stop(self, order_id: str, request: OrderRequest) -> None:
        if not order_id or request.quantity <= 0 or request.stop_loss is None or request.stop_loss <= 0:
            raise ValueError("protective stop modification requires an order id, quantity, and trigger price")
        request = self._normalise_protective_stop(request)
        if self.mode == TradingMode.PAPER:
            if order_id not in self.paper_protective_orders:
                raise ValueError("paper protective stop does not exist")
            self.paper_protective_orders[order_id] = request
            return
        if self.client is None:
            raise RuntimeError("live protective stop modification requires a connected Kite client")
        try:
            self._modify_live_protective_stop(order_id, request)
        except Exception as error:
            if not self._is_protective_trigger_rejection(error):
                raise
            retry = self._normalise_protective_stop(request, safety_ticks=1)
            self._modify_live_protective_stop(order_id, retry)

    def cancel(self, order_id: str) -> None:
        if not order_id:
            return
        if self.mode == TradingMode.PAPER:
            self.paper_protective_orders.pop(order_id, None)
            return
        if self.client is None:
            raise RuntimeError("live order cancellation requires a connected Kite client")
        self.client.cancel_order(variety="regular", order_id=order_id)

    def required_intraday_margin(self, symbol: str) -> float | None:
        """Ask the broker for the real per-share MIS margin it requires for `symbol` right now,
        via Kite's order-margin calculator -- the same endpoint the order ticket UI uses to show
        e.g. "Required Rs94.13 (5x)". This reflects the actual, current leverage Zerodha is
        granting for this specific stock (SEBI peak-margin category, volatility, liquidity),
        rather than a single number assumed for every stock. Returns None if the client can't
        answer (PAPER mode, no client, or the endpoint call fails) so callers can fall back.
        """
        if self.client is None or not hasattr(self.client, "order_margins"):
            return None
        exchange, tradingsymbol = self._split_symbol(symbol, "NSE")
        response = self.client.order_margins(
            [
                {
                    "exchange": exchange,
                    "tradingsymbol": tradingsymbol,
                    "transaction_type": "BUY",
                    "variety": "regular",
                    "product": "MIS",
                    "order_type": "MARKET",
                    "quantity": 1,
                    "price": 0,
                    "trigger_price": 0,
                }
            ]
        )
        margin_per_share = float(response[0]["total"])
        return margin_per_share if margin_per_share > 0 else None

    def register_tick_size(self, symbol: str, tick_size: float) -> None:
        exchange, tradingsymbol = self._split_symbol(symbol, "NSE")
        if tick_size <= 0:
            raise ValueError("tick size must be positive")
        self._tick_sizes[f"{exchange}:{tradingsymbol}"] = float(tick_size)

    def refresh_tick_size(self, symbol: str, exchange: str = "NSE") -> float:
        resolved_exchange, tradingsymbol = self._split_symbol(symbol, exchange)
        self._load_tick_sizes(resolved_exchange)
        tick_size = self._tick_sizes.get(f"{resolved_exchange}:{tradingsymbol}")
        if tick_size is None:
            raise ValueError(
                f"tick size unavailable for {resolved_exchange}:{tradingsymbol}; refusing to submit an unverified protective stop"
            )
        return tick_size

    def tick_size(self, symbol: str, exchange: str = "NSE") -> float:
        resolved_exchange, tradingsymbol = self._split_symbol(symbol, exchange)
        tick_size = self._tick_sizes.get(f"{resolved_exchange}:{tradingsymbol}")
        if tick_size is not None:
            return tick_size
        return self.refresh_tick_size(symbol, exchange)

    @staticmethod
    def _split_symbol(symbol: str, exchange: str) -> tuple[str, str]:
        if ":" not in symbol:
            return exchange, symbol
        symbol_exchange, tradingsymbol = symbol.split(":", 1)
        return symbol_exchange.strip().upper(), tradingsymbol.strip().upper()

    def _normalise_protective_stop(self, request: OrderRequest, safety_ticks: int = 0) -> OrderRequest:
        exchange, tradingsymbol = self._split_symbol(request.symbol, request.exchange)
        tick_size = self._tick_size(exchange, tradingsymbol)
        price = Decimal(str(request.stop_loss))
        tick = Decimal(str(tick_size))
        reference_price = self._last_traded_price(exchange, tradingsymbol) or request.price
        reference = Decimal(str(reference_price))
        if request.side == Side.SELL:
            rounded = (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
            if rounded >= reference:
                rounded = (reference / tick).to_integral_value(rounding=ROUND_FLOOR) * tick - tick
        else:
            rounded = (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick
            if rounded <= reference:
                rounded = (reference / tick).to_integral_value(rounding=ROUND_CEILING) * tick + tick
        if safety_ticks > 0:
            rounded += tick * safety_ticks if request.side == Side.BUY else -tick * safety_ticks
        if rounded <= 0:
            raise ValueError("protective stop could not be placed at a valid tick below or above the market price")
        return OrderRequest(
            request.symbol,
            request.side,
            request.quantity,
            request.price,
            float(rounded),
            request.product,
            request.exchange,
        )

    def _last_traded_price(self, exchange: str, tradingsymbol: str) -> float | None:
        if self.client is None or not hasattr(self.client, "quote"):
            return None
        instrument = f"{exchange}:{tradingsymbol}"
        try:
            quotes = self.client.quote([instrument])
            quote = quotes.get(instrument) or next(iter(quotes.values()), {})
            price = float(quote.get("last_price", 0))
        except (AttributeError, TypeError, ValueError, StopIteration):
            return None
        return price if price > 0 else None

    @staticmethod
    def _is_protective_trigger_rejection(error: Exception) -> bool:
        message = str(error).lower()
        return "tick size" in message or "trigger price for stoploss" in message

    def _place_live_protective_stop(self, request: OrderRequest) -> str:
        exchange, tradingsymbol = self._split_symbol(request.symbol, request.exchange)
        return self.client.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=request.side.value,
            quantity=request.quantity,
            product=request.product,
            order_type="SL-M",
            trigger_price=request.stop_loss,
            market_protection=-1,
        )

    def _modify_live_protective_stop(self, order_id: str, request: OrderRequest) -> None:
        # Kite Connect's modify_order() has no `product` parameter at all -- product type isn't
        # something you change on an existing order, only on a fresh place_order() -- so passing
        # it here always raised a TypeError and silently killed every trailing-stop update at
        # the broker (retried 3x, then given up on, every single cycle, for every live position).
        self.client.modify_order(
            variety="regular",
            order_id=order_id,
            quantity=request.quantity,
            order_type="SL-M",
            trigger_price=request.stop_loss,
            market_protection=-1,
        )

    def _tick_size(self, exchange: str, tradingsymbol: str) -> float:
        key = f"{exchange}:{tradingsymbol}"
        if key not in self._tick_sizes:
            self._load_tick_sizes(exchange)
        if key not in self._tick_sizes:
            self._load_tick_sizes(exchange)
        tick_size = self._tick_sizes.get(key)
        if tick_size is not None:
            return tick_size
        if self.mode == TradingMode.LIVE:
            raise ValueError(f"tick size unavailable for {key}; refusing to submit an unverified protective stop")
        return NSE_TICK_SIZE

    def _load_tick_sizes(self, exchange: str) -> None:
        self._loaded_tick_exchanges.add(exchange)
        if self.client is None or not hasattr(self.client, "instruments"):
            return
        try:
            instruments = self.client.instruments(exchange)
        except Exception:
            return
        for instrument in instruments:
            try:
                tradingsymbol = str(instrument["tradingsymbol"]).strip().upper()
                tick_size = float(instrument["tick_size"])
            except (KeyError, TypeError, ValueError):
                continue
            if tradingsymbol and tick_size > 0:
                self._tick_sizes[f"{exchange}:{tradingsymbol}"] = tick_size
