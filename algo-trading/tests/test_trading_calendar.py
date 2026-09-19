from datetime import date

import pytest

from app.market.trading_calendar import is_trading_day, parse_market_holidays


def test_weekday_with_no_holidays_is_a_trading_day():
    assert is_trading_day(date(2026, 9, 18)) is True  # Friday


def test_saturday_is_not_a_trading_day():
    assert is_trading_day(date(2026, 9, 19)) is False  # Saturday


def test_sunday_is_not_a_trading_day():
    assert is_trading_day(date(2026, 9, 20)) is False  # Sunday


def test_configured_holiday_on_a_weekday_is_not_a_trading_day():
    holidays = {date(2026, 1, 26)}  # Republic Day, a Monday in 2026

    assert is_trading_day(date(2026, 1, 26), holidays) is False


def test_a_weekday_not_in_the_holiday_list_is_still_a_trading_day():
    holidays = {date(2026, 1, 26)}

    assert is_trading_day(date(2026, 9, 18), holidays) is True


def test_parse_market_holidays_reads_a_comma_separated_list():
    holidays = parse_market_holidays("2026-01-26, 2026-03-04,2026-10-02")

    assert holidays == {date(2026, 1, 26), date(2026, 3, 4), date(2026, 10, 2)}


def test_parse_market_holidays_handles_an_empty_string():
    assert parse_market_holidays("") == set()


def test_parse_market_holidays_rejects_a_malformed_date():
    with pytest.raises(ValueError):
        parse_market_holidays("not-a-date")
