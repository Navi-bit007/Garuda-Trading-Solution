import pytest

from app.execution.charges import (
    DELIVERY_DP_CHARGE_PER_SELL,
    DELIVERY_STAMP_DUTY_RATE,
    DELIVERY_STT_RATE,
    GST_RATE,
    INTRADAY_BROKERAGE_CAP,
    INTRADAY_BROKERAGE_RATE,
    INTRADAY_STAMP_DUTY_RATE,
    INTRADAY_STT_RATE,
    NSE_TRANSACTION_CHARGE_RATE,
    SEBI_CHARGE_RATE,
    estimate_equity_charges,
)


def test_delivery_trade_has_zero_brokerage_and_stt_on_both_legs():
    buy_value, sell_value = 10_000.0, 11_000.0

    charges = estimate_equity_charges("SWING", buy_value, sell_value)

    assert charges.brokerage == 0.0
    assert charges.stt == pytest.approx((buy_value + sell_value) * DELIVERY_STT_RATE)
    assert charges.stamp_duty == pytest.approx(buy_value * DELIVERY_STAMP_DUTY_RATE)


def test_delivery_trade_charges_a_flat_dp_fee_only_when_actually_sold():
    sold = estimate_equity_charges("SWING", 10_000.0, 11_000.0)
    not_yet_sold = estimate_equity_charges("SWING", 10_000.0, 0.0)

    assert sold.dp_charges == DELIVERY_DP_CHARGE_PER_SELL
    assert not_yet_sold.dp_charges == 0.0


def test_intraday_trade_has_no_dp_charge_and_stt_only_on_the_sell_leg():
    buy_value, sell_value = 10_000.0, 10_050.0

    charges = estimate_equity_charges("INTRADAY", buy_value, sell_value)

    assert charges.dp_charges == 0.0
    assert charges.stt == pytest.approx(sell_value * INTRADAY_STT_RATE)
    assert charges.stamp_duty == pytest.approx(buy_value * INTRADAY_STAMP_DUTY_RATE)


def test_intraday_brokerage_is_capped_per_side():
    # A large enough leg value makes 0.03% exceed the ₹20/side cap.
    buy_value, sell_value = 200_000.0, 200_000.0

    charges = estimate_equity_charges("INTRADAY", buy_value, sell_value)

    assert charges.brokerage == pytest.approx(INTRADAY_BROKERAGE_CAP * 2)


def test_intraday_brokerage_uses_the_percentage_below_the_cap():
    buy_value, sell_value = 1_000.0, 1_000.0

    charges = estimate_equity_charges("INTRADAY", buy_value, sell_value)

    expected_per_side = min(buy_value * INTRADAY_BROKERAGE_RATE, INTRADAY_BROKERAGE_CAP)
    assert charges.brokerage == pytest.approx(expected_per_side * 2)


def test_gst_applies_only_to_brokerage_sebi_and_transaction_charges():
    buy_value, sell_value = 10_000.0, 11_000.0

    charges = estimate_equity_charges("SWING", buy_value, sell_value)

    expected_gst = (charges.brokerage + charges.sebi_charges + charges.transaction_charges) * GST_RATE
    assert charges.gst == pytest.approx(expected_gst)
    # STT (₹21 here) dwarfs the GST base -- confirms GST didn't leak onto it.
    assert charges.gst < charges.stt


def test_transaction_and_sebi_charges_scale_with_total_turnover():
    buy_value, sell_value = 10_000.0, 11_000.0
    turnover = buy_value + sell_value

    charges = estimate_equity_charges("SWING", buy_value, sell_value)

    assert charges.transaction_charges == pytest.approx(turnover * NSE_TRANSACTION_CHARGE_RATE)
    assert charges.sebi_charges == pytest.approx(turnover * SEBI_CHARGE_RATE)


def test_total_is_the_sum_of_every_component():
    charges = estimate_equity_charges("SWING", 10_000.0, 11_000.0)

    assert charges.total == pytest.approx(
        charges.brokerage
        + charges.stt
        + charges.transaction_charges
        + charges.sebi_charges
        + charges.stamp_duty
        + charges.dp_charges
        + charges.gst
    )
