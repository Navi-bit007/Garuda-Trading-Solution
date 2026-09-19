from __future__ import annotations

from datetime import date

WEEKEND_ISO_WEEKDAYS = (6, 7)  # Saturday, Sunday (date.isoweekday(): Monday=1 ... Sunday=7)


def parse_market_holidays(raw: str) -> set[date]:
    """Parse a comma-separated "YYYY-MM-DD,YYYY-MM-DD" string (Settings.market_holidays) into a
    set of dates. Blank entries and surrounding whitespace are ignored; an invalid date raises
    ValueError immediately rather than silently dropping a holiday someone meant to configure."""
    return {date.fromisoformat(entry.strip()) for entry in raw.split(",") if entry.strip()}


def is_trading_day(check_date: date, holidays: set[date] | None = None) -> bool:
    """NSE is closed on weekends and on configured holidays -- both mean no exchange session at
    all, so nothing (scanning, order placement, stop trailing) should run against the broker."""
    if check_date.isoweekday() in WEEKEND_ISO_WEEKDAYS:
        return False
    if holidays and check_date in holidays:
        return False
    return True
