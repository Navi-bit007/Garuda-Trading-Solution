from app.risk.position_sizing import calculate_quantity


def test_position_size_is_bounded_by_risk_and_deployment():
    assert calculate_quantity(100_000, 100, 95, 0.005, 0.80) == 100


def test_position_size_never_exceeds_deployment():
    assert calculate_quantity(100_000, 1_000, 999, 0.005, 0.10) == 10
