from app.risk.daily_limits import DailyLimits
from app.risk.exposure import Exposure
from app.risk.risk_manager import RiskManager


def test_daily_trade_count_stops_new_trades():
    limits = DailyLimits(100_000, 2)
    limits.record_trade(-1_500)
    limits.record_trade(500)
    assert not limits.can_trade()


def test_exposure_allows_more_notional_value_with_leverage():
    exposure = Exposure(100_000, 0.80)
    # Without leverage, 90,000 of notional exposure exceeds the 80,000 cap.
    assert not exposure.can_add(0, 90_000)
    # At 5x leverage the same notional value fits comfortably inside the cap.
    assert exposure.can_add(0, 90_000, leverage=5.0)


def test_risk_manager_quantity_scales_with_supplied_leverage():
    manager = RiskManager(100_000, 3, DailyLimits(100_000, 5), Exposure(100_000, 0.80))
    assert manager.quantity(1_000) == 80
    assert manager.quantity(1_000, leverage=5.0) == 400


def test_risk_manager_falls_back_to_configured_default_leverage():
    manager = RiskManager(100_000, 3, DailyLimits(100_000, 5), Exposure(100_000, 0.80), default_leverage=5.0)
    # No leverage supplied for this call -> uses the configured default, not 1x.
    assert manager.quantity(1_000) == 400
    # An explicit override still takes priority over the configured default.
    assert manager.quantity(1_000, leverage=2.0) == 160


def test_risk_manager_approve_entry_respects_leverage():
    manager = RiskManager(100_000, 3, DailyLimits(100_000, 5), Exposure(100_000, 0.80))
    # 400 shares at 1,000 each is 400,000 notional -- rejected without leverage.
    approved, reason = manager.approve_entry(0, 0, 1_000, quantity=400)
    assert not approved
    assert "exposure cap exceeded" in reason
    assert "400,000" in reason
    # The same request is approved once the real 5x leverage for this stock is supplied.
    approved, reason = manager.approve_entry(0, 0, 1_000, quantity=400, leverage=5.0)
    assert approved
    assert reason == ""


def test_risk_manager_approve_entry_reports_daily_trade_limit_with_exact_counts():
    limits = DailyLimits(100_000, 2)
    limits.record_trade(500)
    limits.record_trade(-200)
    manager = RiskManager(100_000, 3, limits, Exposure(100_000, 0.80))

    approved, reason = manager.approve_entry(0, 0, 1_000, quantity=1)

    assert not approved
    assert reason == "daily trade limit reached (2/2 trades today)"


def test_risk_manager_approve_entry_reports_open_position_limit_with_exact_counts():
    manager = RiskManager(100_000, 2, DailyLimits(100_000, 5), Exposure(100_000, 0.80))

    approved, reason = manager.approve_entry(2, 0, 1_000, quantity=1)

    assert not approved
    assert reason == "maximum open positions reached (2/2)"
