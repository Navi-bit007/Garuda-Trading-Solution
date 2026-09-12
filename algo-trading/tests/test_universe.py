import pandas as pd

from app.market import universe


class FakeKiteClient:
    def instruments(self, exchange):
        assert exchange == "NSE"
        return [
            {"tradingsymbol": "AAA", "instrument_token": 101, "exchange": "NSE"},
            {"tradingsymbol": "BBB", "instrument_token": 202, "exchange": "NSE"},
            {"tradingsymbol": "CCC", "instrument_token": 303, "exchange": "BSE"},
        ]


def test_api_universe_intersects_nifty500_members_with_nse_instruments(monkeypatch):
    monkeypatch.setattr(universe, "fetch_index_symbols", lambda index_name: {"AAA", "CCC"})

    assert universe.load_nifty_index_token_map_from_api(FakeKiteClient(), "NIFTY 500") == {101: "AAA"}


def test_api_universe_keeps_official_sector_for_nse_members(monkeypatch):
    monkeypatch.setattr(
        universe,
        "fetch_index_constituents",
        lambda index_name: pd.DataFrame(
            [
                {"Symbol": "AAA", "Industry": "Technology"},
                {"Symbol": "CCC", "Industry": "Financial Services"},
            ]
        ),
    )

    result = universe.load_nifty_index_universe_from_api(FakeKiteClient(), "NIFTY 500")

    assert result.to_dict("records") == [{"instrument_token": 101, "symbol": "AAA", "sector": "Technology"}]
