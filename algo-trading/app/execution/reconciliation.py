from __future__ import annotations

from app.execution.position_manager import PositionManager


def reconcile(local: PositionManager, broker_positions: list[dict]) -> list[str]:
    broker_symbols = {position.get("tradingsymbol") for position in broker_positions}
    return sorted(set(local.positions) ^ broker_symbols)
