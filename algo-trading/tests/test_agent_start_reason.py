from datetime import date as real_date, datetime as real_datetime, time as datetime_time
from types import SimpleNamespace

import dashboard.app as dashboard_app
from dashboard.app import describe_agent_start_blocked_reason


def settings_with(market_holidays: str = "", agent_shutdown_time=datetime_time(15, 40)) -> SimpleNamespace:
    return SimpleNamespace(market_holidays=market_holidays, agent_shutdown_time=agent_shutdown_time)


def test_no_reason_on_an_ordinary_trading_day_within_hours(monkeypatch):
    monkeypatch.setattr(dashboard_app, "date", SimpleNamespace(today=lambda: real_date(2026, 1, 1)))  # Thursday
    monkeypatch.setattr(dashboard_app, "datetime", SimpleNamespace(now=lambda: real_datetime(2026, 1, 1, 10, 0)))

    assert describe_agent_start_blocked_reason(settings_with()) is None


def test_blocked_on_a_weekend(monkeypatch):
    monkeypatch.setattr(dashboard_app, "date", SimpleNamespace(today=lambda: real_date(2026, 9, 19)))  # Saturday
    monkeypatch.setattr(dashboard_app, "datetime", SimpleNamespace(now=lambda: real_datetime(2026, 9, 19, 10, 0)))

    reason = describe_agent_start_blocked_reason(settings_with())

    assert reason is not None
    assert "closed today" in reason


def test_blocked_on_a_configured_holiday(monkeypatch):
    monkeypatch.setattr(dashboard_app, "date", SimpleNamespace(today=lambda: real_date(2026, 1, 26)))  # Monday, Republic Day
    monkeypatch.setattr(dashboard_app, "datetime", SimpleNamespace(now=lambda: real_datetime(2026, 1, 26, 10, 0)))

    reason = describe_agent_start_blocked_reason(settings_with(market_holidays="2026-01-26"))

    assert reason is not None


def test_blocked_after_the_configured_shutdown_time(monkeypatch):
    monkeypatch.setattr(dashboard_app, "date", SimpleNamespace(today=lambda: real_date(2026, 1, 1)))  # Thursday
    monkeypatch.setattr(dashboard_app, "datetime", SimpleNamespace(now=lambda: real_datetime(2026, 1, 1, 15, 41)))

    reason = describe_agent_start_blocked_reason(settings_with())

    assert reason is not None
    assert "Trading hours are over" in reason


def test_not_blocked_right_before_the_shutdown_time(monkeypatch):
    monkeypatch.setattr(dashboard_app, "date", SimpleNamespace(today=lambda: real_date(2026, 1, 1)))
    monkeypatch.setattr(dashboard_app, "datetime", SimpleNamespace(now=lambda: real_datetime(2026, 1, 1, 15, 39)))

    assert describe_agent_start_blocked_reason(settings_with()) is None
