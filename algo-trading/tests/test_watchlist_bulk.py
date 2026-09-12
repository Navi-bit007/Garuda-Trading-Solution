import pandas as pd

from dashboard.app import parse_bulk_symbols, reconcile_selected_watchlist_symbols, resolve_bulk_stock_instruments


def instrument_catalog() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"tradingsymbol": "HFCL", "instrument_token": 1, "name": "HFCL Ltd", "exchange": "NSE", "instrument_type": "EQ", "instrument_key": "NSE:HFCL"},
            {"tradingsymbol": "INDUSTOWER", "instrument_token": 2, "name": "Indus Towers Ltd", "exchange": "NSE", "instrument_type": "EQ", "instrument_key": "NSE:INDUSTOWER"},
            {"tradingsymbol": "HFCL", "instrument_token": 3, "name": "HFCL Ltd", "exchange": "BSE", "instrument_type": "EQ", "instrument_key": "BSE:HFCL"},
            {"tradingsymbol": "HFCL", "instrument_token": 4, "name": "HFCL Futures", "exchange": "NSE", "instrument_type": "FUT", "instrument_key": "NSE:HFCL-FUT"},
        ]
    )


def test_parse_bulk_symbols_accepts_common_paste_formats_and_deduplicates():
    assert parse_bulk_symbols("HFCL, INDUSTOWER\nHFCL;  AFFLE") == ["HFCL", "INDUSTOWER", "AFFLE"]


def test_resolve_bulk_stocks_filters_exchange_and_equity_instruments():
    rows, missing = resolve_bulk_stock_instruments(instrument_catalog(), "HFCL, INDUSTOWER, AFFLE", "NSE")

    assert [row["instrument_key"] for row in rows] == ["NSE:HFCL", "NSE:INDUSTOWER"]
    assert missing == ["AFFLE"]


def test_resolve_bulk_stocks_can_target_bse():
    rows, missing = resolve_bulk_stock_instruments(instrument_catalog(), "HFCL", "BSE")

    assert rows[0]["instrument_key"] == "BSE:HFCL"
    assert missing == []


def test_reconcile_selected_watchlist_symbols_refreshes_tokens_and_skips_stale_entries():
    selected = {"NSE:HFCL": 999, "NSE:JGCHEM": 123, "HFCL": 999}

    resolved, skipped = reconcile_selected_watchlist_symbols(selected, instrument_catalog())

    assert resolved == {"NSE:HFCL": 1}
    assert skipped == ["NSE:JGCHEM", "NSE:HFCL"]
