from __future__ import annotations

from typing import Any


class PositionsAPI:
    def __init__(self, client: Any = None):
        self.client = client

    def list(self) -> list[dict]:
        if self.client is None:
            return []
        return self.client.positions().get("net", [])
