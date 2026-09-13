from app.risk.daily_limits import DailyLimits


def test_daily_trade_count_stops_new_trades():
    limits = DailyLimits(100_000, 2)
    limits.record_trade(-1_500)
    limits.record_trade(500)
    assert not limits.can_trade()
