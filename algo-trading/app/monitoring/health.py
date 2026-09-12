from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class HealthStatus:
    ok: bool
    checked_at: datetime
    message: str


def healthy(message: str = "ok") -> HealthStatus:
    return HealthStatus(True, datetime.now(timezone.utc), message)
