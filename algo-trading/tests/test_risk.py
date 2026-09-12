from app.risk.daily_limits import DailyLimits


def test_daily_loss_stops_new_trades():
    limits = DailyLimits(100_000, 0.015, 5)
    limits.record_trade(-1_500)
    assert not limits.can_trade()
