from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

import pandas as pd


INDEX_CONSTITUENT_URLS = {
    "NIFTY 50": "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    "NIFTY Next 50": "https://www.niftyindices.com/IndexConstituent/ind_niftynext50list.csv",
    "NIFTY 100": "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv",
    "NIFTY 200": "https://www.niftyindices.com/IndexConstituent/ind_nifty200list.csv",
    "NIFTY 500": "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "NIFTY Midcap 50": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap50list.csv",
    "NIFTY Midcap 100": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap100list.csv",
    "NIFTY Midcap 150": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
    "NIFTY Smallcap 50": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap50list.csv",
    "NIFTY Smallcap 100": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap100list.csv",
    "NIFTY Smallcap 250": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    "NIFTY Microcap 250": "https://www.niftyindices.com/IndexConstituent/ind_niftymicrocap250list.csv",
    "NIFTY LargeMidcap 250": "https://www.niftyindices.com/IndexConstituent/ind_niftylargemidcap250list.csv",
}
SUPPORTED_INDEXES = tuple(INDEX_CONSTITUENT_URLS)


def load_instruments(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"instrument file missing columns: {sorted(missing)}")
    return frame[frame["exchange"].isin(["NSE", "BSE"])].reset_index(drop=True)


def load_nifty500_token_map(instruments_path: str, membership_path: str) -> dict[int, str]:
    instruments = load_instruments(instruments_path)
    instruments = instruments[instruments["exchange"] == "NSE"].copy()
    membership = pd.read_csv(membership_path)
    symbol_column = next((column for column in ("tradingsymbol", "symbol", "Symbol") if column in membership.columns), None)
    if symbol_column is None:
        raise ValueError("NIFTY 500 membership file must contain tradingsymbol or symbol")

    members = set(membership[symbol_column].dropna().astype(str).str.strip().str.upper())
    instruments["tradingsymbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    selected = instruments[instruments["tradingsymbol"].isin(members)]
    if selected.empty:
        raise ValueError("NIFTY 500 membership did not match any NSE instruments")
    return {int(row.instrument_token): row.tradingsymbol for row in selected.itertuples()}


def load_nifty500_token_map_from_api(kite_client) -> dict[int, str]:
    """Build the NIFTY 500 token map from Kite instruments and NSE membership."""
    symbols = fetch_nifty500_symbols()
    return _intersect_kite_instruments(kite_client, symbols)


def load_nifty_index_token_map_from_api(kite_client, index_name: str) -> dict[int, str]:
    """Build an index token map from an official Nifty Indices constituent feed."""
    symbols = fetch_index_symbols(index_name)
    return _intersect_kite_instruments(kite_client, symbols)


def load_nifty_index_universe_from_api(kite_client, index_name: str) -> pd.DataFrame:
    """Build an index universe with Kite tokens and official constituent industries."""
    constituents = fetch_index_constituents(index_name)
    symbol_column = next((column for column in ("Symbol", "symbol", "tradingsymbol") if column in constituents.columns), None)
    if symbol_column is None:
        raise ValueError(f"{index_name} constituent API returned no symbol column")
    sector_column = next((column for column in ("Industry", "industry", "Sector", "sector") if column in constituents.columns), None)
    membership = constituents.loc[:, [symbol_column] + ([sector_column] if sector_column else [])].copy()
    membership["symbol"] = membership[symbol_column].astype(str).str.strip().str.upper()
    membership["sector"] = membership[sector_column].astype(str).str.strip() if sector_column else "Unknown"
    membership["sector"] = membership["sector"].replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
    membership = membership.loc[:, ["symbol", "sector"]].drop_duplicates("symbol")

    instruments = pd.DataFrame(kite_client.instruments("NSE"))
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Kite instruments response missing columns: {sorted(missing)}")
    instruments = instruments[instruments["exchange"] == "NSE"].copy()
    instruments["symbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    universe = instruments.loc[:, ["instrument_token", "symbol"]].merge(membership, on="symbol", how="inner")
    if universe.empty:
        raise ValueError(f"NSE instruments did not match the {index_name} membership response")
    return universe.drop_duplicates("instrument_token").reset_index(drop=True)


def _intersect_kite_instruments(kite_client, symbols: set[str]) -> dict[int, str]:
    instruments = pd.DataFrame(kite_client.instruments("NSE"))
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Kite instrument response missing columns: {sorted(missing)}")
    instruments = instruments[instruments["exchange"] == "NSE"].copy()
    instruments["tradingsymbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    selected = instruments[instruments["tradingsymbol"].isin(symbols)]
    if selected.empty:
        raise ValueError("NSE instruments did not match the NIFTY 500 membership response")
    return {int(row.instrument_token): row.tradingsymbol for row in selected.itertuples()}


def fetch_nifty500_symbols() -> set[str]:
    """Fetch current NIFTY 500 members from the official NSE index endpoint."""
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
    opener = build_opener(HTTPCookieProcessor())
    try:
        opener.open(Request("https://www.nseindia.com/", headers={"User-Agent": user_agent}), timeout=15).read(1)
        request = Request(
            f"https://www.nseindia.com/api/equity-stockIndices?index={quote('NIFTY 500')}",
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://www.nseindia.com/",
                "User-Agent": user_agent,
            },
        )
        with opener.open(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError("NSE NIFTY 500 membership API is unavailable from this environment") from error

    records = payload.get("data", [])
    symbols = {str(record.get("symbol", "")).strip().upper() for record in records}
    symbols.discard("")
    if not symbols:
        raise ValueError("NSE NIFTY 500 membership API returned no symbols")
    return symbols


def fetch_index_symbols(index_name: str) -> set[str]:
    """Fetch current constituents for a supported Nifty index."""
    constituents = fetch_index_constituents(index_name)
    symbol_column = next((column for column in ("Symbol", "symbol", "tradingsymbol") if column in constituents.columns), None)
    if symbol_column is None:
        raise ValueError(f"{index_name} constituent API returned no symbol column")
    symbols = set(constituents[symbol_column].dropna().astype(str).str.strip().str.upper())
    symbols.discard("")
    if not symbols:
        raise ValueError(f"{index_name} constituent API returned no symbols")
    return symbols


def fetch_index_constituents(index_name: str) -> pd.DataFrame:
    """Fetch current constituents and their official industry classifications."""
    try:
        url = INDEX_CONSTITUENT_URLS[index_name]
    except KeyError as error:
        raise ValueError(f"Unsupported Nifty index: {index_name}") from error

    request = Request(
        url,
        headers={
            "Accept": "text/csv,application/csv,text/plain,*/*",
            "Referer": "https://www.niftyindices.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        },
    )
    try:
        with build_opener().open(request, timeout=15) as response:
            frame = pd.read_csv(BytesIO(response.read()))
    except (HTTPError, URLError, TimeoutError, pd.errors.ParserError, UnicodeDecodeError) as error:
        if index_name == "NIFTY 500":
            try:
                return pd.DataFrame({"symbol": sorted(fetch_nifty500_symbols())})
            except (RuntimeError, ValueError):
                pass
        raise RuntimeError(f"{index_name} constituent API is unavailable from this environment") from error

    symbol_column = next((column for column in ("Symbol", "symbol", "tradingsymbol") if column in frame.columns), None)
    if symbol_column is None:
        raise ValueError(f"{index_name} constituent API returned no symbol column")
    symbols = set(frame[symbol_column].dropna().astype(str).str.strip().str.upper())
    symbols.discard("")
    if not symbols:
        raise ValueError(f"{index_name} constituent API returned no symbols")
    frame[symbol_column] = frame[symbol_column].astype(str).str.strip().str.upper()
    return frame.reset_index(drop=True)
