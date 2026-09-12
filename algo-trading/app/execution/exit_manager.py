from __future__ import annotations

from datetime import datetime, time

from app.execution.position_manager import PositionManager


class ExitManager:
    def __init__(self, positions: PositionManager, force_exit: time):
        self.positions = positions
        self.force_exit = force_exit

    def symbols_to_exit(self, timestamp: datetime) -> list[str]:
        if timestamp.time() < self.force_exit:
            return []
        return list(self.positions.positions)
