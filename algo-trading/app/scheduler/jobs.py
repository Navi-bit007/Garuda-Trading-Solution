from __future__ import annotations

from datetime import datetime


def run_cycle(cycle, timestamp: datetime) -> object:
    return cycle(timestamp)
