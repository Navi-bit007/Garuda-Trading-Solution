from io import StringIO

import pandas as pd

from dashboard.app import load_manual_stock_universe


class StubKiteClient:
    def instruments(self, exchange):
        assert exchange == "NSE"
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 1, "exchange": "NSE"},
            {"tradingsymbol": "TCS", "instrument_token": 2, "exchange": "NSE"},
            {"tradingsymbol": "OTHER", "instrument_token": 3, "exchange": "BSE"},
        ]


def test_manual_universe_resolves_symbols_and_preserves_sector():
    uploaded = StringIO("symbol,sector\n reliance,Energy\nTCS,Information Technology\nreliance,Energy\n")

    universe = load_manual_stock_universe(uploaded, StubKiteClient())

    assert universe.to_dict("records") == [
        {"instrument_token": 1, "symbol": "RELIANCE", "sector": "Energy"},
        {"instrument_token": 2, "symbol": "TCS", "sector": "Information Technology"},
    ]


def test_manual_universe_rejects_unknown_symbols():
    uploaded = StringIO("tradingsymbol\nUNKNOWN\n")

    with __import__("pytest").raises(ValueError, match="not found in NSE instruments"):
        load_manual_stock_universe(uploaded, StubKiteClient())