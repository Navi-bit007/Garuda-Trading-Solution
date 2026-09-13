from app.risk.position_sizing import calculate_quantity


def test_position_size_is_bounded_by_deployment():
    assert calculate_quantity(100_000, 1_000, 0.80) == 80


def test_position_size_never_exceeds_deployment():
    assert calculate_quantity(100_000, 1_000, 0.10) == 10
