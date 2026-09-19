from __future__ import annotations

from dataclasses import dataclass

# Rates sourced from https://zerodha.com/charges/#tab-equities and, for DP charges,
# https://zerodha.com/charges/ (Depository/other charges tab) -- both fetched 2026-09-19.
# Equity cash market only: this app never trades F&O, and every position is either "INTRADAY"
# (Zerodha product MIS) or "SWING" (product CNC/delivery) -- see
# app/execution/exit_actions.py:product_code_for. STT and stamp duty rates are government-set
# and can change; re-check zerodha.com/charges periodically and update the constants below.
NSE_TRANSACTION_CHARGE_RATE = 0.0000307  # 0.00307% of turnover, both sides
SEBI_CHARGE_RATE = 10 / 1_00_00_000  # ₹10 per crore of turnover
GST_RATE = 0.18  # on (brokerage + SEBI charges + transaction charges) only

DELIVERY_STT_RATE = 0.001  # 0.1% on both buy and sell
DELIVERY_STAMP_DUTY_RATE = 0.00015  # 0.015%, buy side only
# Flat, GST-inclusive, charged once per scrip whenever a delivery (CNC) holding is sold --
# regardless of quantity. Not part of the equities charges tab (it's under "Depository and other
# charges"), but a real cost, so it's included here. Never applies to intraday (MIS never settles
# into a holding, so there's nothing for a depository to charge for).
DELIVERY_DP_CHARGE_PER_SELL = 15.34

INTRADAY_BROKERAGE_RATE = 0.0003  # 0.03% per executed order (i.e. per side)
INTRADAY_BROKERAGE_CAP = 20.0  # ...or ₹20 per side, whichever is lower
INTRADAY_STT_RATE = 0.00025  # 0.025%, sell side only
INTRADAY_STAMP_DUTY_RATE = 0.00003  # 0.003%, buy side only


@dataclass(frozen=True)
class ChargeBreakdown:
    brokerage: float
    stt: float
    transaction_charges: float
    sebi_charges: float
    stamp_duty: float
    dp_charges: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.transaction_charges + self.sebi_charges + self.stamp_duty + self.dp_charges + self.gst


def estimate_equity_charges(position_type: str, buy_value: float, sell_value: float) -> ChargeBreakdown:
    """Estimate Zerodha's brokerage + tax charges for one round-trip equity trade.

    `buy_value`/`sell_value` are each price x quantity for that leg. This is precise percentage
    math, not a replica of Zerodha's exact per-order rounding, so it can differ from the real
    contract note by a few paise/rupees.
    """
    turnover = buy_value + sell_value
    transaction_charges = turnover * NSE_TRANSACTION_CHARGE_RATE
    sebi_charges = turnover * SEBI_CHARGE_RATE
    if position_type == "INTRADAY":
        brokerage = min(buy_value * INTRADAY_BROKERAGE_RATE, INTRADAY_BROKERAGE_CAP) + min(
            sell_value * INTRADAY_BROKERAGE_RATE, INTRADAY_BROKERAGE_CAP
        )
        stt = sell_value * INTRADAY_STT_RATE
        stamp_duty = buy_value * INTRADAY_STAMP_DUTY_RATE
        dp_charges = 0.0
    else:
        brokerage = 0.0
        stt = turnover * DELIVERY_STT_RATE
        stamp_duty = buy_value * DELIVERY_STAMP_DUTY_RATE
        dp_charges = DELIVERY_DP_CHARGE_PER_SELL if sell_value > 0 else 0.0
    gst = (brokerage + sebi_charges + transaction_charges) * GST_RATE
    return ChargeBreakdown(
        brokerage=brokerage,
        stt=stt,
        transaction_charges=transaction_charges,
        sebi_charges=sebi_charges,
        stamp_duty=stamp_duty,
        dp_charges=dp_charges,
        gst=gst,
    )
