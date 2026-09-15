import pytest

from app.risk.position_sizing import calculate_quantity


def test_position_size_is_bounded_by_deployment():
    assert calculate_quantity(100_000, 1_000, 0.80) == 80


def test_position_size_never_exceeds_deployment():
    assert calculate_quantity(100_000, 1_000, 0.10) == 10


def test_leverage_multiplies_quantity():
    assert calculate_quantity(100_000, 1_000, 0.80, leverage=5.0) == 400


def test_leverage_defaults_to_one():
    assert calculate_quantity(100_000, 1_000, 0.80) == calculate_quantity(100_000, 1_000, 0.80, leverage=1.0)


def test_leverage_must_be_positive():
    with pytest.raises(ValueError, match="leverage"):
        calculate_quantity(100_000, 1_000, 0.80, leverage=0)
