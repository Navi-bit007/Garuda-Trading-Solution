from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.broker.order_api import OrderAPI, OrderRequest
from app.config.constants import Side
from app.database.models import ActivityRecord, DecisionLogRecord, PositionRecord, TradeRecord
from app.database.repository import Repository


def exchange_of(symbol: str) -> str:
    return symbol.split(":", 1)[0].strip().upper() if ":" in symbol else "NSE"


def tradingsymbol_of(symbol: str) -> str:
    return symbol.split(":", 1)[1].strip().upper() if ":" in symbol else symbol.strip().upper()


def product_code_for(position_type: str) -> str:
    """MIS for intraday, CNC for swing -- swing positions settle into a Zerodha holding, so
    their protective stop and exit orders must use CNC or the broker rejects them outright."""
    return "CNC" if position_type == "SWING" else "MIS"


def correlation_id_for(record: PositionRecord) -> str:
    """A stable id spanning one position's whole life, matching the format every other decision-
    log writer in this app uses (see TrailingStopAgent._correlation_id) so a manual exit's
    decision row threads onto the same trade history as its entry and stop-trail rows."""
    return f"{record.symbol}:{record.entry_time.isoformat()}"


@dataclass(frozen=True)
class ExitOutcome:
    success: bool
    symbol: str
    order_id: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    cancel_error: str | None = None
    error: str | None = None


def close_position_at_market(
    repository: Repository,
    orders: OrderAPI,
    record: PositionRecord,
    price: float,
    timestamp: datetime,
    reason: str,
    event_kind: str,
    decision: str,
    notifier: Any = None,
) -> ExitOutcome:
    """Market-close an open position and cancel its resting protective stop, recording the same
    trade/activity/decision-log trail an automated exit would -- shared by the trailing-stop
    agent's force-exit and the dashboard's manual "Exit position" action so both stay in lockstep
    with the decision-log's correlation/outcome-linking invariants instead of drifting apart.

    Order matters: the market exit is placed FIRST, and the protective stop is only cancelled
    AFTER that succeeds. If the market order itself fails, the position is left exactly as it
    was -- still open, still protected -- rather than cancelling protection for an exit that
    never happened.
    """
    exit_side = Side.SELL if record.side == "BUY" else Side.BUY
    product = product_code_for(record.position_type)
    exchange = exchange_of(record.symbol)
    try:
        order_id = orders.place(
            OrderRequest(record.symbol, exit_side, record.quantity, price, product=product, exchange=exchange)
        )
    except Exception as error:
        if notifier is not None:
            notifier.send(f"critical_unprotected: exit order failed for {record.symbol}: {error}")
        return ExitOutcome(success=False, symbol=record.symbol, error=str(error))

    cancel_error: str | None = None
    try:
        if record.protective_order_id:
            orders.cancel(record.protective_order_id)
    except Exception as error:
        # The market exit already succeeded -- the position is genuinely flat at the broker --
        # so a stale SL-M cancel failing here must not stop the trade from being recorded.
        cancel_error = str(error)

    multiplier = 1 if record.side == "BUY" else -1
    pnl = multiplier * (price - record.entry_price) * record.quantity
    repository.save_trade(
        TradeRecord(
            symbol=record.symbol,
            entry_time=record.entry_time,
            exit_time=timestamp,
            entry_price=record.entry_price,
            exit_price=price,
            quantity=record.quantity,
            pnl=pnl,
            side=record.side,
            position_type=record.position_type,
            strategy_name=record.strategy_name,
            exit_reason=reason,
        )
    )
    repository.delete_position(record.symbol)
    repository.save_activity(
        ActivityRecord(
            event_kind=event_kind,
            symbol=record.symbol,
            timestamp=timestamp,
            mode="LIVE",
            price=price,
            order_id=record.protective_order_id,
            side=exit_side.value,
            quantity=record.quantity,
            entry_price=record.entry_price,
            stop_loss=record.stop_loss,
            pnl=pnl,
            reason=reason,
        )
    )
    correlation_id = correlation_id_for(record)
    repository.save_decision(
        DecisionLogRecord(
            timestamp=timestamp,
            symbol=record.symbol,
            event_type="exit",
            strategy_name=record.strategy_name or "",
            mode=record.trading_mode,
            decision=decision,
            rationale=reason,
            inputs={},
            outputs={"exit_price": price, "pnl": pnl},
            correlation_id=correlation_id,
            source_table="positions",
            source_id=record.symbol,
        )
    )
    repository.link_decision_outcome(correlation_id, {"exit_price": price, "pnl": pnl, "exit_reason": reason})
    if notifier is not None:
        notifier.send(f"{reason}: {record.symbol} closed at {price:.2f}, pnl={pnl:.2f}")
    return ExitOutcome(success=True, symbol=record.symbol, order_id=order_id, exit_price=price, pnl=pnl, cancel_error=cancel_error)
