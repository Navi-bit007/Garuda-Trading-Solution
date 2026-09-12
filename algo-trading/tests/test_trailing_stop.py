from app.execution.trailing_stop import TrailingStop


def test_long_trailing_stop_only_moves_up():
    stop = TrailingStop(entry_price=100, initial_stop=95, atr_multiplier=1.5, side="BUY")
    assert stop.update(102, 2) == 99
    assert stop.update(100, 1) == 99
