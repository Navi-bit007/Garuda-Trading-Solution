from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as datetime_time, timedelta
import json
from html import escape
import logging
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) in sys.path:
    sys.path.remove(str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

# Streamlit reruns this module on every interaction/fragment tick, so guard against attaching
# duplicate handlers to the root logger each time. Without a real log file here, an exception
# caught only as a transient st.warning/st.error (e.g. broker reconciliation failing because a
# Kite access token expired) leaves no trace anywhere once the toast disappears on the next
# rerun -- this is what made the missing-broker-order incident on 2026-09-11 hard to diagnose.
_DASHBOARD_LOG_PATH = PROJECT_ROOT / "data" / "dashboard.log"
if not logging.getLogger().handlers:
    _DASHBOARD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(_DASHBOARD_LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )
logger = logging.getLogger(__name__)

from app.config.constants import Side, SignalAction, TradingMode
from app.broker.authentication import AccessToken, AuthenticationError, exchange_request_token
from app.broker.kite_client import KiteClient
from app.broker.market_data import MarketData
from app.broker.positions_api import PositionsAPI
from app.config.settings import get_settings
from app.database.database import Database
from app.database.models import ActivityRecord, DynamicWatchlistRecord, NotificationRecord, SignalRecord, WatchlistRecord
from app.database.repository import Repository
from app.execution.position_manager import Position, PositionManager
from app.execution.reconciliation import reconcile
from app.execution.agent_launcher import agent_heartbeat_is_fresh, launch_trailing_stop_agent, maybe_autostart_trailing_agent, stop_trailing_stop_agent, trailing_agent_env_from_settings
from app.execution.swing_auto_trader import SwingAutoTrader, SwingOrderResult, SwingScanResult
from app.market.candles import validate_ohlcv
from app.market.indicators import atr as compute_atr
from app.market.dynamic_watchlist import (
    DYNAMIC_AUTO_REFRESH_CANDLES,
    DYNAMIC_INTERVAL,
    DYNAMIC_LOOKBACK_DAYS,
    DYNAMIC_MAX_WORKERS,
    DYNAMIC_WATCHLIST_NAME,
    auto_refresh_slot,
    first_session_candles,
    filter_dynamic_watchlist,
)
from app.market.historical_scan import HistoricalScanResult, scan_historical_watchlist
from app.market.scanner import Nifty500Scanner
from app.market.signal_scanner import SignalScanResult, StrategySignalScanner
from app.market.universe import SUPPORTED_INDEXES, load_nifty_index_universe_from_api
from app.monitoring.notifications import Notifier, save_signal_notifications
from app.monitoring.signal_engine import build_signal_engine
from app.strategy.crossover import CrossoverStrategy
from app.risk.position_sizing import calculate_quantity
from app.execution.trading_pipeline import TradingPipeline
from app.strategy.atr_momentum import AtrMomentumStrategy
from app.strategy.ema_200_close import Ema200CloseStrategy
from app.strategy.ema_9_200_swing import Ema9200SwingStrategy
from app.strategy.ema_9_200_progressive import Ema9200ProgressiveStrategy
from app.strategy.swing_trend_breakout import SwingTrendBreakoutStrategy
from app.strategy.ema_trend import EmaTrendStrategy
from app.strategy.high_conviction_long import HighConvictionLongStrategy
from app.strategy.opening_range_breakout import OpeningRangeBreakoutStrategy
from app.strategy.previous_day_high_breakout import PreviousDayHighBreakoutStrategy
from app.strategy.pre_spike_momentum import PreSpikeMomentumConfig, PreSpikeMomentumStrategy
from app.strategy.preset_builder import (
    CURRENT_STRATEGY_TYPE,
    DEFAULT_PARAMETERS,
    EMA_ONLY_DEFAULT_PARAMETERS,
    EMA_ONLY_STRATEGY_LABEL,
    EMA_ONLY_STRATEGY_TYPE,
    build_ema_preset_strategy,
    build_preset_strategy,
    current_strategy,
)
from app.strategy.signal import Signal
from app.strategy.vwap_momentum import VwapMomentumStrategy
from app.strategy.vwap_ema_breakout import MarketRegimeContext, TimeframeConfirmation, VwapEmaBreakoutStrategy
from app.market.backtest_data import KiteHistoricalDataLoader
from backtest.engine import BacktestEngine
from backtest.metrics import calculate_metrics
from backtest.runner import run_strategy_backtest


EDITABLE_SETTINGS = (
    "trading_mode",
    "initial_capital",
    "max_open_positions",
    "max_trades_per_day",
    "max_capital_deployment",
    "market_open",
    "entry_start",
    "entry_end",
    "force_exit",
    "trailing_atr_multiplier",
    "min_stop_improvement_pct",
    "swing_capital_limit",
    "swing_quantity_limit",
    "swing_trailing_atr_multiplier",
    "swing_max_open_positions",
    "intraday_capital_limit",
    "intraday_leverage_multiplier",
)

SWING_LOOKBACK_DAYS = 600
# Kite Connect's historical-candle endpoint is rate-limited (documented ~3 req/s); this bounds
# how many symbols the intraday/swing scanners fetch candles for concurrently. Matches
# DYNAMIC_MAX_WORKERS' reasoning for the dynamic watchlist scan.
SCAN_MAX_WORKERS = 4


def settings_values(settings) -> dict:
    return {name: getattr(settings, name) for name in EDITABLE_SETTINGS}


def serialize_frontend_settings(values: dict) -> dict:
    serialized = {}
    for name, value in values.items():
        if isinstance(value, TradingMode):
            serialized[name] = value.value
        elif isinstance(value, datetime_time):
            serialized[name] = value.isoformat()
        else:
            serialized[name] = value
    return serialized


def deserialize_frontend_settings(values: dict) -> dict:
    overrides = {}
    for name, value in values.items():
        if name not in EDITABLE_SETTINGS:
            continue
        if name == "trading_mode":
            value = TradingMode(value)
        elif name in {"market_open", "entry_start", "entry_end", "force_exit"}:
            value = datetime_time.fromisoformat(value)
        overrides[name] = value
    return overrides


def apply_frontend_settings(base_settings, overrides: dict):
    if not overrides:
        return base_settings
    values = base_settings.model_dump() if hasattr(base_settings, "model_dump") else base_settings.dict()
    values.update(overrides)
    return type(base_settings)(**values)


def get_frontend_settings(st, base_settings):
    if "frontend_settings" not in st.session_state:
        persisted = get_dashboard_repository(st).load_dashboard_settings(base_settings.user_id)
        try:
            persisted_settings = apply_frontend_settings(base_settings, deserialize_frontend_settings(persisted))
        except (TypeError, ValueError):
            persisted_settings = base_settings
        st.session_state.frontend_settings = settings_values(persisted_settings)
    return apply_frontend_settings(base_settings, st.session_state.frontend_settings)


def broker_credentials_configured(settings) -> bool:
    api_secret = settings.kite_api_secret.get_secret_value()
    return bool(settings.kite_api_key.strip() and api_secret.strip())


def broker_access_token_configured(settings, runtime_access_token: str = "") -> bool:
    return bool(runtime_access_token.strip() or settings.kite_access_token.get_secret_value().strip())


def load_activity(database_path: str = "data/trading.sqlite3") -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    database = Database(database_path)
    database.initialize()
    try:
        orders = pd.read_sql_query("SELECT * FROM orders ORDER BY created_at DESC", database.connection)
        trades = pd.read_sql_query("SELECT * FROM trades ORDER BY exit_time DESC", database.connection)
        activity = pd.read_sql_query("SELECT * FROM activity ORDER BY timestamp DESC, id DESC", database.connection)
    finally:
        database.close()
    return orders, trades, activity


def build_strategy(name: str, fast: int, slow: int, opening_bars: int, atr_period: int):
    if name == "VWAP EMA breakout":
        return VwapEmaBreakoutStrategy(atr_period=atr_period)
    if name == "EMA trend":
        return EmaTrendStrategy(fast=fast, slow=slow, atr_period=atr_period)
    if name == "VWAP momentum":
        return VwapMomentumStrategy(atr_period=atr_period)
    if name == "Opening range breakout":
        return OpeningRangeBreakoutStrategy(opening_bars=opening_bars)
    return AtrMomentumStrategy(period=atr_period)


CURRENT_STRATEGY_LABEL = "VWAP EMA breakout (current)"
EMA_PROGRESSIVE_LIVE_LABEL = "EMA 9/200 progressive"
PRE_SPIKE_LIVE_LABEL = "Pre-Spike Momentum"
PREVIOUS_DAY_HIGH_LABEL = "Previous day high breakout"
EMA_200_CLOSE_LIVE_LABEL = "EMA 200 close-above"
HIGH_CONVICTION_LIVE_LABEL = "High-conviction long"
BACKTEST_PROGRESSIVE_LABEL = "EMA 9/200 progressive"
BACKTEST_SWING_TREND_LABEL = "Trend breakout (next-session confirmation)"
BACKTEST_STRATEGY_LABELS = [
    BACKTEST_PROGRESSIVE_LABEL,
    PRE_SPIKE_LIVE_LABEL,
    PREVIOUS_DAY_HIGH_LABEL,
    EMA_200_CLOSE_LIVE_LABEL,
    HIGH_CONVICTION_LIVE_LABEL,
    CURRENT_STRATEGY_LABEL,
    EMA_ONLY_STRATEGY_LABEL,
    "EMA 9/200 swing",
    BACKTEST_SWING_TREND_LABEL,
]
DAILY_SCAN_STRATEGIES = {
    "EMA 9/200 swing": Ema9200SwingStrategy,
    "EMA 200 close-above": Ema200CloseStrategy,
    "Previous day high breakout": PreviousDayHighBreakoutStrategy,
}
SWING_STRATEGIES = {
    "EMA 9/200 swing": "EMA 9/200 swing",
    "Trend breakout (next-session confirmation)": "SWING_TREND_BREAKOUT",
}
WORKSPACE_PAGES = [
    "Overview",
    "Live monitor",
    "P&L statement",
    "Watchlists",
    "Scanner & signals",
    "Backtesting",
    "Swing auto trading",
    "Intratrading",
    "Risk & settings",
    "Kite authentication",
]
SIDEBAR_PAGE_ICONS = {
    "Overview": "dashboard",
    "Live monitor": "monitor_heart",
    "P&L statement": "receipt_long",
    "Watchlists": "list_alt",
    "Scanner & signals": "radar",
    "Backtesting": "history",
    "Swing auto trading": "trending_up",
    "Intratrading": "bolt",
    "Risk & settings": "settings",
    "Kite authentication": "key",
}
SIDEBAR_LOGO_SVG_BASE64 = (
    "PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCI+"
    "CjxyZWN0IHg9IjQuNSIgeT0iMSIgd2lkdGg9IjEuMyIgaGVpZ2h0PSI0IiByeD0iMC42NSIgZmlsbD0i"
    "IzE0MTcxYiIvPgo8cmVjdCB4PSI3LjIiIHk9IjEiIHdpZHRoPSIxLjMiIGhlaWdodD0iNCIgcng9IjAu"
    "NjUiIGZpbGw9IiMxNDE3MWIiLz4KPHJlY3QgeD0iMTAuMCIgeT0iMSIgd2lkdGg9IjEuMyIgaGVpZ2h0"
    "PSI0IiByeD0iMC42NSIgZmlsbD0iIzE0MTcxYiIvPgo8cmVjdCB4PSIxMi43IiB5PSIxIiB3aWR0aD0i"
    "MS4zIiBoZWlnaHQ9IjQiIHJ4PSIwLjY1IiBmaWxsPSIjMTQxNzFiIi8+CjxyZWN0IHg9IjE1LjUiIHk9"
    "IjEiIHdpZHRoPSIxLjMiIGhlaWdodD0iNCIgcng9IjAuNjUiIGZpbGw9IiMxNDE3MWIiLz4KPHJlY3Qg"
    "eD0iMTguMiIgeT0iMSIgd2lkdGg9IjEuMyIgaGVpZ2h0PSI0IiByeD0iMC42NSIgZmlsbD0iIzE0MTcx"
    "YiIvPgo8cmVjdCB4PSIyIiB5PSI0IiB3aWR0aD0iMjAiIGhlaWdodD0iMTgiIHJ4PSIyLjIiIGZpbGw9"
    "IiMxNDE3MWIiLz4KPHJlY3QgeD0iNS41IiB5PSI3LjMiIHdpZHRoPSIxMyIgaGVpZ2h0PSIxLjYiIHJ4"
    "PSIwLjgiIGZpbGw9IiNmZmZmZmYiLz4KPHJlY3QgeD0iNS41IiB5PSIxMC4xIiB3aWR0aD0iOCIgaGVp"
    "Z2h0PSIxLjYiIHJ4PSIwLjgiIGZpbGw9IiNmZmZmZmYiLz4KPGNpcmNsZSBjeD0iMTIiIGN5PSIxNiIg"
    "cj0iNS4zIiBmaWxsPSIjZmZmZmZmIi8+Cjx0ZXh0IHg9IjEyIiB5PSIxOC42IiBmb250LXNpemU9IjciIGZv"
    "bnQtZmFtaWx5PSJBcmlhbCwgc2Fucy1zZXJpZiIgZm9udC13ZWlnaHQ9IjcwMCIgdGV4dC1hbmNob3I9Im1p"
    "ZGRsZSIgZmlsbD0iIzE0MTcxYiI+JDwvdGV4dD4KPHJlY3QgeD0iNS41IiB5PSIxOS42IiB3aWR0aD0iMTMi"
    "IGhlaWdodD0iMiIgcng9IjEiIGZpbGw9IiNmZmZmZmYiLz4KPC9zdmc+"
)


def load_dashboard_strategy_options(
    repository: Repository,
    include_pre_spike: bool = False,
    include_previous_day_high: bool = False,
) -> dict[str, tuple[str, dict]]:
    options = {
        CURRENT_STRATEGY_LABEL: (CURRENT_STRATEGY_TYPE, dict(DEFAULT_PARAMETERS)),
        EMA_ONLY_STRATEGY_LABEL: (EMA_ONLY_STRATEGY_TYPE, dict(EMA_ONLY_DEFAULT_PARAMETERS)),
        HIGH_CONVICTION_LIVE_LABEL: (HighConvictionLongStrategy.name, {}),
    }
    if include_pre_spike:
        options[PRE_SPIKE_LIVE_LABEL] = (PreSpikeMomentumStrategy.name, {})
    if include_previous_day_high:
        options[PREVIOUS_DAY_HIGH_LABEL] = (PreviousDayHighBreakoutStrategy.name, {})
    for preset in repository.load_strategy_presets():
        options[f"Custom: {preset.name}"] = (preset.strategy_type, dict(preset.parameters))
    return options


def build_dashboard_strategy(repository: Repository, selected_label: str, pre_spike_config: PreSpikeMomentumConfig | None = None):
    if selected_label == CURRENT_STRATEGY_LABEL:
        return current_strategy()
    if selected_label == EMA_ONLY_STRATEGY_LABEL:
        return EmaTrendStrategy(**EMA_ONLY_DEFAULT_PARAMETERS)
    if selected_label == PRE_SPIKE_LIVE_LABEL:
        return PreSpikeMomentumStrategy(config=pre_spike_config)
    if selected_label == PREVIOUS_DAY_HIGH_LABEL:
        return PreviousDayHighBreakoutStrategy()
    if selected_label == HIGH_CONVICTION_LIVE_LABEL:
        return HighConvictionLongStrategy()
    prefix, name = selected_label.split(": ", 1)
    if prefix != "Custom":
        raise ValueError("unknown strategy selection")
    preset = next((item for item in repository.load_strategy_presets() if item.name == name), None)
    if preset is None:
        raise ValueError("selected custom strategy no longer exists")
    if preset.strategy_type == EMA_ONLY_STRATEGY_TYPE:
        return build_ema_preset_strategy(preset.name, preset.parameters)
    return build_preset_strategy(preset.name, preset.parameters)


def build_backtest_strategy(selected_label: str, interval: str):
    if selected_label == BACKTEST_PROGRESSIVE_LABEL:
        return Ema9200ProgressiveStrategy(timeframe=interval)
    if selected_label == PRE_SPIKE_LIVE_LABEL:
        return PreSpikeMomentumStrategy(timeframe=interval)
    if selected_label == PREVIOUS_DAY_HIGH_LABEL:
        return PreviousDayHighBreakoutStrategy()
    if selected_label == EMA_200_CLOSE_LIVE_LABEL:
        return Ema200CloseStrategy()
    if selected_label == HIGH_CONVICTION_LIVE_LABEL:
        return HighConvictionLongStrategy()
    if selected_label == CURRENT_STRATEGY_LABEL:
        return current_strategy()
    if selected_label == EMA_ONLY_STRATEGY_LABEL:
        return EmaTrendStrategy(**EMA_ONLY_DEFAULT_PARAMETERS)
    if selected_label == "EMA 9/200 swing":
        return Ema9200SwingStrategy()
    if selected_label == BACKTEST_SWING_TREND_LABEL:
        return SwingTrendBreakoutStrategy()
    raise ValueError(f"unsupported backtest strategy: {selected_label}")


def build_live_signal_strategy(selected_label: str, timeframe: str, pre_spike_config: PreSpikeMomentumConfig | None = None):
    if selected_label == PRE_SPIKE_LIVE_LABEL:
        strategy = PreSpikeMomentumStrategy(config=pre_spike_config)
        strategy.timeframe = timeframe
        return strategy
    if selected_label == PREVIOUS_DAY_HIGH_LABEL:
        return PreviousDayHighBreakoutStrategy()
    if selected_label == EMA_200_CLOSE_LIVE_LABEL:
        return Ema200CloseStrategy()
    if selected_label == HIGH_CONVICTION_LIVE_LABEL:
        return HighConvictionLongStrategy()
    if selected_label == EMA_PROGRESSIVE_LIVE_LABEL:
        return None
    raise ValueError(f"unknown live signal strategy: {selected_label}")


def render_pre_spike_config(st, key_prefix: str) -> PreSpikeMomentumConfig:
    defaults = PreSpikeMomentumConfig()
    with st.expander("Pre-Spike Momentum filters", expanded=True):
        st.caption("Signals use the latest completed 5-minute candle. Set a confirmation toggle to make that condition mandatory.")
        score_column, demand_column, move_column = st.columns(3)
        minimum_score = score_column.number_input(
            "Minimum score",
            min_value=0,
            max_value=100,
            value=defaults.minimum_score,
            step=1,
            key=f"{key_prefix}_minimum_score",
            help="The weighted setup score required before a signal can be generated.",
        )
        minimum_rvol = demand_column.number_input(
            "Minimum RVOL (x)",
            min_value=0.0,
            max_value=20.0,
            value=defaults.minimum_rvol,
            step=0.1,
            key=f"{key_prefix}_minimum_rvol",
            help="Current candle volume divided by the average volume for the same time slot.",
        )
        minimum_price_change_pct = move_column.number_input(
            "Minimum 5-minute move (%)",
            min_value=0.0,
            max_value=20.0,
            value=defaults.minimum_price_change_pct,
            step=0.05,
            key=f"{key_prefix}_minimum_price_change_pct",
            help="Minimum price expansion when no breakout confirmation is required.",
        )

        volume_column, compression_column, close_column = st.columns(3)
        minimum_volume_buildup_ratio = volume_column.number_input(
            "Minimum volume buildup (x)",
            min_value=0.0,
            max_value=20.0,
            value=1.5,
            step=0.1,
            key=f"{key_prefix}_minimum_volume_buildup_ratio",
            help="Recent three-candle average volume divided by the earlier baseline average.",
        )
        maximum_compression_pct = compression_column.number_input(
            "Maximum compression range (%)",
            min_value=0.0,
            max_value=20.0,
            value=defaults.maximum_compression_pct,
            step=0.1,
            key=f"{key_prefix}_maximum_compression_pct",
            help="The previous 12 candles must stay within this high-to-low percentage range when compression is required.",
        )
        minimum_close_location = close_column.number_input(
            "Minimum close location",
            min_value=0.0,
            max_value=1.0,
            value=defaults.minimum_close_location,
            step=0.05,
            key=f"{key_prefix}_minimum_close_location",
            help="Where the close must sit inside the candle range. 0.70 means the top 30% of the range.",
        )

        st.markdown("**Required confirmations**")
        confirmation_columns = st.columns(4)
        require_vwap_rising = confirmation_columns[0].toggle(
            "Rising VWAP",
            value=defaults.require_vwap_rising,
            key=f"{key_prefix}_require_vwap_rising",
        )
        require_price_above_ema20 = confirmation_columns[1].toggle(
            "Price > EMA20",
            value=defaults.require_price_above_ema20,
            key=f"{key_prefix}_require_price_above_ema20",
        )
        require_ema9_above_ema20 = confirmation_columns[2].toggle(
            "EMA9 > EMA20",
            value=defaults.require_ema9_above_ema20,
            key=f"{key_prefix}_require_ema9_above_ema20",
        )
        require_ema20_above_ema50 = confirmation_columns[3].toggle(
            "EMA20 > EMA50",
            value=defaults.require_ema20_above_ema50,
            key=f"{key_prefix}_require_ema20_above_ema50",
        )
        confirmation_columns = st.columns(5)
        require_previous_day_breakout = confirmation_columns[0].toggle(
            "Previous-day high breakout",
            value=defaults.require_previous_day_breakout,
            key=f"{key_prefix}_require_previous_day_breakout",
        )
        require_twenty_day_breakout = confirmation_columns[1].toggle(
            "20-day high breakout",
            value=defaults.require_twenty_day_breakout,
            key=f"{key_prefix}_require_twenty_day_breakout",
        )
        require_bullish_quality = confirmation_columns[2].toggle(
            "Strong bullish close",
            value=defaults.require_bullish_quality,
            key=f"{key_prefix}_require_bullish_quality",
        )
        require_volume_buildup = confirmation_columns[3].toggle(
            "Volume buildup",
            value=defaults.require_volume_buildup,
            key=f"{key_prefix}_require_volume_buildup",
        )
        require_range_compression = confirmation_columns[4].toggle(
            "Range compression",
            value=defaults.require_range_compression,
            key=f"{key_prefix}_require_range_compression",
        )

    return PreSpikeMomentumConfig(
        minimum_score=int(minimum_score),
        minimum_rvol=float(minimum_rvol),
        minimum_price_change_pct=float(minimum_price_change_pct),
        minimum_volume_buildup_ratio=float(minimum_volume_buildup_ratio),
        maximum_compression_pct=float(maximum_compression_pct),
        minimum_close_location=float(minimum_close_location),
        require_vwap_rising=require_vwap_rising,
        require_price_above_ema20=require_price_above_ema20,
        require_ema9_above_ema20=require_ema9_above_ema20,
        require_ema20_above_ema50=require_ema20_above_ema50,
        require_previous_day_breakout=require_previous_day_breakout,
        require_twenty_day_breakout=require_twenty_day_breakout,
        require_bullish_quality=require_bullish_quality,
        require_volume_buildup=require_volume_buildup,
        require_range_compression=require_range_compression,
    )


def inject_styles(st) -> None:
    st.markdown(
        """
        <style>
        :root { --ink: #17211b; --muted: #66736a; --paper: #f4f6ef; --line: #d8dfd3; --mint: #b9e8cf; }
        html, body, .stApp { font-family: "Segoe UI Variable", "Segoe UI", sans-serif; color: var(--ink) !important; }
        .stApp { background: var(--paper); }
        [data-testid="stMainBlockContainer"] { padding-top: 1.25rem; }
        [data-testid="stSidebar"] { background: #e6efe5; border-right: 1px solid var(--line); }
        [data-testid="stSidebar"] * { color: var(--ink) !important; opacity: 1 !important; }
        [data-testid="stSidebar"] [data-testid="stRadio"] { width: 100%; }
        [data-testid="stSidebar"] [data-testid="stRadio"] > div[role="radiogroup"] { width: 100%; gap: 7px; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label {
            width: 100%;
            box-sizing: border-box;
            min-height: 44px;
            padding: 10px 12px;
            border: 1px solid transparent;
            border-radius: 7px;
            background: rgba(255,255,255,.38);
            cursor: pointer;
            transition: background .15s ease, border-color .15s ease;
        }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:hover { background: rgba(255,255,255,.78); border-color: #9aa89a; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) { background: #176b4d; border-color: #176b4d; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) * { color: #ffffff !important; }
        [data-testid="stSidebar"] [data-testid="stRadio"] label p { font-size: .94rem; font-weight: 600; }
        [data-testid="stSidebar"] [data-testid="stButton"] button { min-height: 44px; font-weight: 650; }
        [data-testid="stSidebar"] [data-testid="stCaption"] { font-size: .8rem; }
        /* The native radio dot is purely decorative here -- selection is already shown via the
           pill background/border above, so hide the dot and keep only the icon + label. */
        [data-testid="stSidebar"] [data-testid="stRadioOption"] div:has(> [data-testid="stMarkdownContainer"]) > *:not([data-testid="stMarkdownContainer"]) {
            display: none !important;
        }
        [data-testid="stSidebar"] [data-testid="stRadio"] label [data-testid="stIconMaterial"] { font-size: 1.45rem; }
        [data-testid="stMetric"] { background: rgba(255,255,255,.72); border: 1px solid var(--line); padding: 15px 17px; border-radius: 7px; }
        [data-testid="stMetric"] * { color: var(--ink) !important; opacity: 1 !important; }
        [data-testid="stMetricDelta"] svg { fill: currentColor; }
        .stApp [data-testid="stWidgetLabel"], .stApp [data-testid="stWidgetLabel"] * { color: var(--ink) !important; opacity: 1 !important; }
        .stApp input:not([type="range"]), .stApp textarea { color: var(--ink) !important; background: #ffffff !important; caret-color: var(--ink) !important; }
        .stApp input:not([type="range"])::placeholder, .stApp textarea::placeholder { color: var(--muted) !important; opacity: 1 !important; }
        .stApp [data-testid="stButton"] button { color: var(--ink) !important; background: #ffffff !important; border-color: #9aa89a !important; }
        .stApp [data-testid="stButton"] button:hover { color: var(--ink) !important; background: #edf4ed !important; border-color: #176b4d !important; }
        .stApp [data-testid="stFormSubmitButton"] button { color: #ffffff !important; background: #176b4d !important; border-color: #176b4d !important; }
        .stApp [data-testid="stFormSubmitButton"] button:hover { color: #ffffff !important; background: #0f553b !important; border-color: #0f553b !important; }
        .stApp [data-testid="stButtonGroup"] button { color: var(--ink) !important; background: #ffffff !important; border-color: #9aa89a !important; }
        .stApp [data-testid="stButtonGroup"] button[data-selected="true"] { color: #ffffff !important; background: #176b4d !important; border-color: #176b4d !important; }
        [data-testid="stAppDeployButton"] button { color: #ffffff !important; }
        [data-testid="stAppDeployButton"] button:hover { color: #ffffff !important; background: rgba(255,255,255,.12) !important; }
        h1, h2, h3 { color: var(--ink) !important; letter-spacing: 0; }
        .eyebrow { color: #4b745d; font-size: .72rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; }
        .status { display: inline-flex; align-items: center; gap: 8px; border: 1px solid #9bc9ac; background: var(--mint); color: #1d5434; border-radius: 999px; padding: 5px 10px; font-size: .78rem; font-weight: 700; }
        .status-dot { width: 7px; height: 7px; background: #20844b; border-radius: 50%; }
        .empty { border: 1px dashed #b6c4b7; padding: 22px; border-radius: 7px; color: var(--muted); background: rgba(255,255,255,.38); }
        .watchlist-card { min-height: 220px; }
        .watchlist-card h3 { margin: 0; font-size: 1.15rem; }
        .watchlist-count { color: var(--muted); font-size: .86rem; margin: 4px 0 14px; }
        .symbol-preview { color: #315543; font-size: .86rem; min-height: 42px; line-height: 1.7; }
        .engine-panel { border: 1px solid #b9d2c1; border-left: 4px solid #176b4d; background: #edf7ef; padding: 16px 18px; border-radius: 7px; }
        .engine-panel strong { color: #176b4d; }
        .scan-activity { display: flex; align-items: center; gap: 14px; margin: 14px 0; padding: 14px 17px; border: 1px solid #8fc7a4; border-radius: 9px; background: linear-gradient(105deg, #e2f6e7, #f7fbf4 62%, #e8f5ed); box-shadow: 0 5px 18px rgba(23,107,77,.09); }
        .scan-activity-mark { position: relative; display: grid; place-items: center; width: 38px; height: 38px; flex: 0 0 38px; border: 1px solid #65ad82; border-radius: 50%; background: #d5f0dc; }
        .scan-activity-mark::before, .scan-activity-mark::after { content: ""; position: absolute; border: 1px solid #4aab70; border-radius: 50%; animation: scan-pulse 1.8s ease-out infinite; }
        .scan-activity-mark::before { inset: 6px; }
        .scan-activity-mark::after { inset: 1px; animation-delay: .6s; }
        .scan-activity-dot { width: 9px; height: 9px; border-radius: 50%; background: #176b4d; box-shadow: 0 0 0 4px rgba(23,107,77,.12); }
        .scan-activity-copy { min-width: 0; flex: 1; }
        .scan-activity-copy strong { display: block; color: #176b4d; font-size: .96rem; }
        .scan-activity-copy span { color: #53645a; font-size: .82rem; }
        .scan-activity-beam { display: flex; gap: 4px; height: 5px; margin-top: 9px; overflow: hidden; border-radius: 99px; background: #cce5d2; }
        .scan-activity-beam span { width: 24%; border-radius: inherit; background: #20844b; animation: scan-beam 1.5s ease-in-out infinite; }
        .scan-activity-beam span:nth-child(2) { animation-delay: .18s; }
        .scan-activity-beam span:nth-child(3) { animation-delay: .36s; }
        .scan-activity-beam span:nth-child(4) { animation-delay: .54s; }
        .scan-activity-live { align-self: flex-start; color: #176b4d; border: 1px solid #8fc7a4; border-radius: 999px; padding: 4px 8px; font-size: .68rem; font-weight: 800; letter-spacing: .08em; }
        .scan-activity.stalled { border-color: #d4a84e; background: #fff8e7; }
        .scan-activity.stalled .scan-activity-mark { border-color: #d4a84e; background: #fff0c4; }
        .scan-activity.stalled .scan-activity-dot { background: #b7791f; }
        .scan-activity.error { border-color: #d68c8c; background: #fff1f1; }
        .scan-activity.error .scan-activity-mark { border-color: #d68c8c; background: #ffe1e1; }
        .scan-activity.error .scan-activity-dot { background: #b42318; }
        .scan-activity.stalled .scan-activity-beam span, .scan-activity.error .scan-activity-beam span { animation-play-state: paused; opacity: .35; }
        @keyframes scan-pulse { 0% { opacity: .8; transform: scale(.7); } 70%, 100% { opacity: 0; transform: scale(1.55); } }
        @keyframes scan-beam { 0%, 100% { opacity: .35; transform: translateX(-18%); } 50% { opacity: 1; transform: translateX(300%); } }
        .search-result { padding: 10px 0; border-bottom: 1px solid var(--line); }
        .search-result:last-child { border-bottom: 0; }
        .search-symbol { font-weight: 750; color: var(--ink); }
        .search-name { color: var(--muted); font-size: .88rem; }
        .selection-tray { border: 1px solid #a9cbb4; background: #f2faf3; padding: 14px 16px; border-radius: 7px; margin: 12px 0; }
        .selection-tray strong { color: #176b4d; }
        .result-meta { color: var(--muted); font-size: .82rem; padding-top: 2px; }
        .auth-step { border: 1px solid var(--line); background: rgba(255,255,255,.55); border-radius: 9px; padding: 18px 20px; margin-bottom: 14px; }
        .auth-step.locked { opacity: .55; }
        .auth-step-head { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
        .auth-step-number { display: grid; place-items: center; width: 26px; height: 26px; flex: 0 0 26px; border-radius: 50%; background: #176b4d; color: #ffffff !important; font-size: .8rem; font-weight: 800; }
        .auth-step.locked .auth-step-number { background: #9aa89a; }
        .auth-step.done .auth-step-number { background: #176b4d; }
        .auth-step-title { font-weight: 700; font-size: 1rem; color: var(--ink); }
        .auth-step-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
        .auth-step-row [data-testid="stButton"], .auth-step-row [data-testid="stLinkButton"] { margin: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f"""
        <style>
        /* Streamlit's own sidebar-collapse arrow is redundant now -- the logo button below is
           the only expand/collapse control we want, so hide the native one entirely. */
        [data-testid="stSidebarCollapseButton"] {{ display: none !important; }}
        [data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {{ align-items: center; }}
        [data-testid="stSidebar"] .st-key-sidebar_logo_toggle [data-testid="stButton"] button {{
            background: #ffffff url("data:image/svg+xml;base64,{SIDEBAR_LOGO_SVG_BASE64}") no-repeat center / 38px 38px !important;
            color: transparent !important;
            font-size: 0 !important;
            min-height: 52px !important;
            height: 52px !important;
            width: 52px !important;
            padding: 0 !important;
        }}
        [data-testid="stSidebar"] .st-key-sidebar_logo_toggle [data-testid="stButton"] button:hover {{
            background: #edf4ed url("data:image/svg+xml;base64,{SIDEBAR_LOGO_SVG_BASE64}") no-repeat center / 38px 38px !important;
            border-color: #176b4d !important;
        }}
        .sidebar-brand-title {{ font-size: 1.32rem; font-weight: 800; line-height: 52px; margin: 0; }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def runtime_access_token(st, settings) -> str:
    if st.session_state.get("kite_logged_out"):
        return ""
    session_token = st.session_state.get("kite_access_token", "")
    if session_token.strip():
        return session_token.strip()
    entered_token = st.session_state.get("kite_access_token_input", "")
    if entered_token.strip():
        return entered_token.strip()
    # Streamlit gives every browser tab its own st.session_state, even a duplicated tab --
    # there's no way to share one AppSession across tabs. Without this, each new/duplicated tab
    # would have to repeat the Kite login flow even though another tab is already authenticated
    # for the day. The DB-persisted token (set on successful login below) lets any tab pick up
    # that same session; it's cleared on explicit logout and naturally goes stale once Zerodha
    # invalidates it at the next trading day's reset.
    persisted_token = get_dashboard_repository(st).load_kite_access_token(dashboard_user_id(settings))
    if persisted_token.strip():
        st.session_state.kite_access_token = persisted_token.strip()
        return persisted_token.strip()
    return settings.kite_access_token.get_secret_value().strip()


KITE_TOKEN_VALIDATION_CACHE_SECONDS = 60


def verified_kite_access_token(st, settings) -> str:
    """Like runtime_access_token, but actively confirms the token still works at Zerodha before
    trusting it, instead of only checking that a token string is stored.

    Kite invalidates every access token once a trading day, with no refresh grant -- a token
    persisted from yesterday is worthless today, but `runtime_access_token` alone can't tell the
    difference, so the app used to keep declaring "Kite authenticated" the next morning right up
    until some other page's broker call failed. Cached per token for
    KITE_TOKEN_VALIDATION_CACHE_SECONDS so this doesn't cost an API call on every rerun.

    Only Kite's own `TokenException` (a real "this token is dead" answer from the broker) is
    treated as invalid; any other error (a network blip, Kite being briefly unreachable) leaves
    the cached result untouched and the token is trusted as-is -- otherwise a transient failure
    here would force everyone into a false "please sign in again" on every hiccup.
    """
    token = runtime_access_token(st, settings)
    if not token or not broker_credentials_configured(settings):
        return token
    cache_key = f"kite_token_verified::{token}"
    cached = st.session_state.get(cache_key)
    now = datetime.now()
    if cached is not None and (now - cached["checked_at"]).total_seconds() < KITE_TOKEN_VALIDATION_CACHE_SECONDS:
        return token if cached["valid"] else ""
    try:
        from kiteconnect.exceptions import TokenException
    except ImportError:
        return token
    try:
        connect_kite(settings, token).client.profile()
    except TokenException:
        st.session_state[cache_key] = {"checked_at": now, "valid": False}
        repository = get_dashboard_repository(st)
        st.session_state.pop("kite_access_token", None)
        repository.clear_kite_access_token(dashboard_user_id(settings))
        st.session_state.kite_session_expired_notice = True
        return ""
    except Exception:
        return token
    st.session_state[cache_key] = {"checked_at": now, "valid": True}
    return token


def log_out_of_kite(st, repository=None, user_id: str = "") -> None:
    for key in (
        "kite_access_token",
        "kite_request_token",
        "kite_access_token_input",
        "kite_auth_notice",
        "dashboard_kite_client",
        "dashboard_pipeline",
    ):
        st.session_state.pop(key, None)
    st.session_state.kite_logged_out = True
    st.session_state.pop("active_page", None)
    st.query_params.clear()
    if repository is not None and user_id:
        repository.clear_kite_access_token(user_id)


def connect_kite(settings, access_token: str) -> KiteClient:
    client = KiteClient(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    client.connect(AccessToken(access_token))
    return client


def load_watchlist_instruments(settings, access_token: str) -> pd.DataFrame:
    client = connect_kite(settings, access_token)
    instruments = pd.DataFrame(client.client.instruments())
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Kite instrument response missing columns: {sorted(missing)}")
    if "name" not in instruments.columns:
        instruments["name"] = ""
    for column in ("instrument_type", "segment", "expiry"):
        if column not in instruments.columns:
            instruments[column] = ""
    instruments["tradingsymbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    instruments["exchange"] = instruments["exchange"].astype(str).str.strip().str.upper()
    instruments["name"] = instruments["name"].fillna("").astype(str).str.strip()
    instruments["instrument_type"] = instruments["instrument_type"].fillna("").astype(str).str.strip().replace("", "Equity")
    instruments["segment"] = instruments["segment"].fillna("").astype(str).str.strip()
    instruments["expiry"] = instruments["expiry"].fillna("").astype(str).str.strip()
    instruments = instruments.loc[
        instruments["exchange"].isin(["NSE", "BSE"]) & instruments["tradingsymbol"].ne(""),
        ["tradingsymbol", "instrument_token", "name", "exchange", "instrument_type", "segment", "expiry"],
    ].copy()
    instruments["instrument_key"] = instruments["exchange"] + ":" + instruments["tradingsymbol"]
    return instruments.drop_duplicates("instrument_key").sort_values(["tradingsymbol", "exchange"]).reset_index(drop=True)


def add_instruments_to_watchlists(repository: Repository, user_id: str, watchlists: list[WatchlistRecord], instrument_rows: list[dict], destinations: list[str]) -> int:
    selected_by_name = {watchlist.name: watchlist for watchlist in watchlists}
    additions = 0
    for destination in destinations:
        watchlist = selected_by_name.get(destination)
        if watchlist is None:
            continue
        symbols = dict(watchlist.symbols)
        before = len(symbols)
        symbols.update({row["instrument_key"]: int(row["instrument_token"]) for row in instrument_rows})
        repository.update_watchlist_symbols(user_id, destination, symbols)
        additions += len(symbols) - before
    return additions


def parse_bulk_symbols(text: str) -> list[str]:
    symbols = re.split(r"[,;\s]+", text.upper())
    return list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))


def resolve_bulk_stock_instruments(instruments: pd.DataFrame, text: str, exchange: str) -> tuple[list[dict], list[str]]:
    symbols = parse_bulk_symbols(text)
    if not symbols or instruments.empty:
        return [], symbols
    exchange_instruments = instruments.loc[
        (instruments["exchange"] == exchange)
        & instruments["tradingsymbol"].isin(symbols)
        & instruments["instrument_type"].isin(["EQ", "Equity"])
    ].copy()
    exchange_instruments["symbol_rank"] = exchange_instruments["tradingsymbol"].map({symbol: index for index, symbol in enumerate(symbols)})
    exchange_instruments = exchange_instruments.sort_values("symbol_rank").drop_duplicates("tradingsymbol")
    rows = [
        {
            "instrument_key": row.instrument_key,
            "symbol": row.tradingsymbol,
            "exchange": row.exchange,
            "name": row.name,
            "instrument_token": int(row.instrument_token),
        }
        for row in exchange_instruments.itertuples()
    ]
    found = set(exchange_instruments["tradingsymbol"])
    return rows, [symbol for symbol in symbols if symbol not in found]


def split_watchlist_symbol(symbol: str) -> tuple[str, str]:
    if ":" in symbol:
        exchange, tradingsymbol = symbol.split(":", 1)
        return exchange, tradingsymbol
    return "NSE", symbol


def reconcile_selected_watchlist_symbols(
    selected_symbols: dict[str, int],
    instruments: pd.DataFrame,
) -> tuple[dict[str, int], list[str]]:
    """Resolve persisted watchlist entries against the current equity catalog."""
    if instruments.empty:
        return {}, list(selected_symbols)

    catalog = instruments.copy()
    if "instrument_key" not in catalog.columns:
        catalog["instrument_key"] = (
            catalog["exchange"].astype(str).str.strip().str.upper()
            + ":"
            + catalog["tradingsymbol"].astype(str).str.strip().str.upper()
        )
    if "instrument_type" in catalog.columns:
        catalog = catalog.loc[catalog["instrument_type"].isin(["EQ", "Equity"])]
    catalog = catalog.drop_duplicates("instrument_key")
    current_by_key = {
        str(row.instrument_key).upper(): int(row.instrument_token)
        for row in catalog.itertuples()
    }

    resolved: dict[str, int] = {}
    skipped: list[str] = []
    used_tokens: set[int] = set()
    for stored_symbol in selected_symbols:
        exchange, tradingsymbol = split_watchlist_symbol(str(stored_symbol))
        key = f"{exchange.strip().upper()}:{tradingsymbol.strip().upper()}"
        token = current_by_key.get(key)
        if not key.split(":", 1)[1] or token is None or token in used_tokens:
            skipped.append(key)
            continue
        resolved[key] = token
        used_tokens.add(token)
    return resolved, skipped


def load_watchlist_prices(settings, access_token: str, rows: pd.DataFrame) -> dict[int, float]:
    if rows.empty:
        return {}
    client = connect_kite(settings, access_token)
    instruments = [f"{row.exchange}:{row.tradingsymbol}" for row in rows.itertuples()]
    quotes = client.client.quote(instruments)
    prices = {}
    for quote in quotes.values():
        token = int(quote.get("instrument_token", 0))
        if token and quote.get("last_price") is not None:
            prices[token] = float(quote["last_price"])
    return prices


def load_live_candles_from_client(kite_client, instrument_token: int, interval: str, lookback_days: int, include_current: bool = False) -> pd.DataFrame:
    end = datetime.now()
    rows = MarketData(kite_client).historical(instrument_token, end - timedelta(days=lookback_days), end, interval)
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = validate_ohlcv(pd.DataFrame(rows).rename(columns={"date": "timestamp"}))
    interval_minutes = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30, "60minute": 60}
    if interval == "day":
        current_period_start = pd.Timestamp.now(tz=frame["timestamp"].dt.tz).floor("D") if frame["timestamp"].dt.tz else pd.Timestamp.now().floor("D")
    elif interval in interval_minutes:
        floor_frequency = f"{interval_minutes[interval]}min"
        current_period_start = pd.Timestamp.now(tz=frame["timestamp"].dt.tz).floor(floor_frequency) if frame["timestamp"].dt.tz else pd.Timestamp.now().floor(floor_frequency)
    else:
        return frame
    if include_current:
        return frame.reset_index(drop=True)
    return frame.loc[frame["timestamp"] < current_period_start].reset_index(drop=True)


@st.cache_data(ttl=3600, max_entries=2000, show_spinner=False)
def load_swing_daily_candles_cached(
    _kite_client,
    cache_identity: str,
    instrument_token: int,
    scan_day: str,
) -> pd.DataFrame:
    """Reuse daily history across swing fragment reruns without caching the broker client."""
    del cache_identity, scan_day
    for attempt in range(3):
        try:
            return load_live_candles_from_client(_kite_client, instrument_token, "day", SWING_LOOKBACK_DAYS)
        except Exception as error:
            if "too many requests" not in str(error).lower() or attempt == 2:
                raise
            time.sleep(float(attempt + 1))
    raise RuntimeError("daily candle request failed after retries")


@st.cache_data(ttl=86400, max_entries=10, show_spinner=False)
def load_swing_tick_sizes_cached(_kite_client, cache_identity: str) -> dict[int, float]:
    del cache_identity
    instruments = pd.DataFrame(_kite_client.instruments())
    if "instrument_token" not in instruments.columns or "tick_size" not in instruments.columns:
        return {}
    instruments = instruments.loc[:, ["instrument_token", "tick_size"]].dropna()
    return {
        int(row.instrument_token): float(row.tick_size)
        for row in instruments.itertuples()
        if float(row.tick_size) > 0
    }


def load_historical_daily_candles_from_client(
    kite_client,
    instrument_token: int,
    selected_date: date,
    lookback_days: int = 600,
) -> pd.DataFrame:
    end = datetime.combine(selected_date + timedelta(days=1), datetime_time.min)
    start = end - timedelta(days=lookback_days)
    rows = MarketData(kite_client).historical(instrument_token, start, end, "day")
    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame = validate_ohlcv(pd.DataFrame(rows).rename(columns={"date": "timestamp"}))
    timestamps = pd.to_datetime(frame["timestamp"], errors="raise")
    local_timestamps = timestamps.dt.tz_convert("Asia/Kolkata") if timestamps.dt.tz is not None else timestamps
    return frame.loc[local_timestamps.dt.date <= selected_date].reset_index(drop=True)


def load_dynamic_watchlist_rows(
    settings,
    access_token: str,
    source: WatchlistRecord,
    evaluation_date: date,
    require_breakout: bool = False,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    if not source.symbols:
        return filter_dynamic_watchlist({}, {}), []
    client = connect_kite(settings, access_token)
    current_date = pd.Timestamp.now(tz="Asia/Kolkata").date()
    candles_by_symbol: dict[str, pd.DataFrame] = {}
    current_prices: dict[str, float] = {}
    errors: list[str] = []
    if evaluation_date == current_date:
        try:
            instruments = [f"{split_watchlist_symbol(symbol)[0]}:{split_watchlist_symbol(symbol)[1]}" for symbol in source.symbols]
            quotes = client.client.quote(instruments)
            for symbol, quote in quotes.items():
                if quote.get("last_price") is not None:
                    current_prices[symbol] = float(quote["last_price"])
            for symbol, quote in quotes.items():
                token = int(quote.get("instrument_token", 0))
                if token and quote.get("last_price") is not None:
                    matching_symbol = next((stored for stored, value in source.symbols.items() if value == token), None)
                    if matching_symbol:
                        current_prices[matching_symbol] = float(quote["last_price"])
        except Exception as error:
            errors.append(f"quotes: {error}")
        for symbol in source.symbols:
            current_prices.setdefault(symbol, float("nan"))
    def load_symbol_candles(symbol: str, instrument_token: int) -> tuple[pd.DataFrame | None, str | None]:
        try:
            if evaluation_date == current_date:
                candles = load_live_candles_from_client(client.client, instrument_token, DYNAMIC_INTERVAL, DYNAMIC_LOOKBACK_DAYS, include_current=True)
            else:
                end = datetime.combine(evaluation_date + timedelta(days=1), datetime_time.min)
                start = end - timedelta(days=DYNAMIC_LOOKBACK_DAYS)
                rows = MarketData(client.client).historical(instrument_token, start, end, DYNAMIC_INTERVAL)
                candles = validate_ohlcv(pd.DataFrame(rows).rename(columns={"date": "timestamp"})) if rows else pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            return first_session_candles(candles, evaluation_date, settings.market_open), None
        except Exception as error:
            return None, f"{symbol}: {error}"

    total_symbols = len(source.symbols)
    worker_count = min(DYNAMIC_MAX_WORKERS, total_symbols)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="dynamic-watchlist") as executor:
        futures = {
            executor.submit(load_symbol_candles, symbol, instrument_token): symbol
            for symbol, instrument_token in source.symbols.items()
        }
        for completed_symbols, future in enumerate(as_completed(futures), start=1):
            symbol = futures[future]
            candles, error = future.result()
            if candles is not None:
                candles_by_symbol[symbol] = candles
            if error:
                errors.append(error)
            if progress_callback:
                progress_callback(completed_symbols, total_symbols, symbol)
    return filter_dynamic_watchlist(source.symbols, candles_by_symbol, current_prices, require_breakout), errors


def load_live_nifty_regime(kite_client, lookback_days: int = 5) -> MarketRegimeContext:
    quotes = kite_client.quote(["NSE:NIFTY 50"])
    quote = next(iter(quotes.values()), {})
    token = int(quote.get("instrument_token", 0))
    if not token:
        raise ValueError("Kite quote response did not contain a NIFTY 50 index token")
    candles = load_live_candles_from_client(kite_client, token, "5minute", lookback_days)
    return MarketRegimeContext.from_candles(candles)


def select_sector_universe(st, universe: pd.DataFrame, key: str) -> tuple[pd.DataFrame, str]:
    sectors = sorted(universe["sector"].dropna().astype(str).unique())
    selected_sector = st.selectbox("Sector", ["All sectors", *sectors], key=key)
    if selected_sector == "All sectors":
        return universe, selected_sector
    return universe[universe["sector"] == selected_sector].reset_index(drop=True), selected_sector


def load_manual_stock_universe(uploaded_file, kite_client) -> pd.DataFrame:
    """Resolve an uploaded symbol list against the current NSE instrument master."""
    try:
        uploaded = pd.read_csv(uploaded_file)
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as error:
        raise ValueError("manual universe CSV could not be read") from error
    columns = {str(column).strip().lower(): column for column in uploaded.columns}
    symbol_column = next((columns[name] for name in ("symbol", "tradingsymbol") if name in columns), None)
    if symbol_column is None:
        raise ValueError("manual universe CSV must contain a symbol or tradingsymbol column")
    sector_column = next((columns[name] for name in ("sector", "industry") if name in columns), None)
    manual = pd.DataFrame({"symbol": uploaded[symbol_column].astype(str).str.strip().str.upper()})
    manual["sector"] = uploaded[sector_column].astype(str).str.strip() if sector_column else "Unknown"
    manual["sector"] = manual["sector"].replace({"": "Unknown", "nan": "Unknown", "None": "Unknown"})
    manual = manual.loc[manual["symbol"].ne("") & manual["symbol"].ne("NAN")].drop_duplicates("symbol")
    if manual.empty:
        raise ValueError("manual universe CSV did not contain any symbols")
    instruments = pd.DataFrame(kite_client.instruments("NSE"))
    required = {"tradingsymbol", "instrument_token", "exchange"}
    missing = required - set(instruments.columns)
    if missing:
        raise ValueError(f"Kite instruments response missing columns: {sorted(missing)}")
    instruments = instruments.loc[instruments["exchange"] == "NSE", ["tradingsymbol", "instrument_token"]].copy()
    instruments["symbol"] = instruments["tradingsymbol"].astype(str).str.strip().str.upper()
    universe = instruments.loc[:, ["instrument_token", "symbol"]].merge(manual, on="symbol", how="inner")
    missing_symbols = sorted(set(manual["symbol"]) - set(universe["symbol"]))
    if missing_symbols:
        preview = ", ".join(missing_symbols[:10])
        suffix = " ..." if len(missing_symbols) > 10 else ""
        raise ValueError(f"manual universe contains symbols not found in NSE instruments: {preview}{suffix}")
    return universe.drop_duplicates("instrument_token").reset_index(drop=True)


def load_live_scanner_snapshot(settings, access_token: str, token_to_symbol: dict[int, str], scanner_limit: int = 20) -> pd.DataFrame:
    client = connect_kite(settings, access_token)
    return Nifty500Scanner(token_to_symbol, max_candidates=scanner_limit).rank_ticks(load_live_quote_ticks(client.client, token_to_symbol))


def load_live_quote_ticks(kite_client, token_to_symbol: dict[int, str]) -> list[dict]:
    instruments = [f"NSE:{symbol}" for symbol in token_to_symbol.values()]
    quotes = kite_client.quote(instruments)
    ticks = []
    for quote in quotes.values():
        token = int(quote.get("instrument_token", 0))
        if "last_price" not in quote:
            continue
        ticks.append(
            {
                "instrument_token": token,
                "last_price": quote["last_price"],
                "ohlc": quote.get("ohlc", {}),
                "volume": quote.get("volume", 0),
                "timestamp": datetime.now(),
            }
        )
    return ticks


def get_dashboard_pipeline(st, settings, access_token: str, token_to_symbol: dict[int, str], strategy=None) -> TradingPipeline:
    pipeline = st.session_state.get("dashboard_pipeline")
    if pipeline is not None and pipeline.settings.trading_mode != settings.trading_mode:
        if pipeline.managed_positions:
            raise RuntimeError("close all open positions before changing trading mode")
        st.session_state.pop("dashboard_pipeline", None)
        st.session_state.pop("dashboard_kite_client", None)
        pipeline = None
    repository = get_dashboard_repository(st)
    if pipeline is None:
        client = connect_kite(settings, access_token)
        broker_client = client.client if settings.trading_mode == TradingMode.LIVE else None
        pipeline = TradingPipeline(settings, token_to_symbol, strategy or VwapEmaBreakoutStrategy(), broker_client, repository)
        st.session_state.dashboard_kite_client = client
        st.session_state.dashboard_pipeline = pipeline
    else:
        if getattr(pipeline, "activity_repository", None) is None:
            pipeline.activity_repository = repository
        if strategy is not None:
            pipeline.strategy = strategy
        pipeline.extend_universe(token_to_symbol)
    return pipeline


def record_dashboard_events(st, events) -> None:
    history = st.session_state.get("dashboard_events", [])
    st.session_state.dashboard_events = (history + list(events))[-12:]


def record_signal_notifications(st, signals: pd.DataFrame, strategy_label: str, universe_label: str, sector: str, settings) -> list[NotificationRecord]:
    if signals.empty:
        return []
    notifier = Notifier(
        bool(getattr(settings, "enable_telegram", False)),
        getattr(settings, "telegram_bot_token", "").get_secret_value() if hasattr(getattr(settings, "telegram_bot_token", ""), "get_secret_value") else str(getattr(settings, "telegram_bot_token", "")),
        str(getattr(settings, "telegram_chat_id", "")),
    )
    new_notifications = save_signal_notifications(
        get_dashboard_repository(st),
        signals,
        strategy_label,
        universe_label,
        sector,
        notifier,
    )
    if new_notifications:
        st.toast(f"{len(new_notifications)} new signal notification{'s' if len(new_notifications) != 1 else ''}", icon=":material/notifications:")
    return new_notifications


def get_dashboard_repository(st) -> Repository:
    repository = st.session_state.get("dashboard_repository")
    if repository is not None and (
        not getattr(repository.database, "thread_safe", False)
        or not hasattr(repository, "save_dashboard_settings")
    ):
        stale_pipeline = st.session_state.get("dashboard_pipeline")
        st.session_state.pop("dashboard_repository", None)
        st.session_state.pop("dashboard_database", None)
        repository = None
        if stale_pipeline is not None:
            stale_pipeline.activity_repository = None
    if repository is None:
        database = Database()
        database.initialize()
        repository = Repository(database)
        st.session_state.dashboard_database = database
        st.session_state.dashboard_repository = repository
        stale_pipeline = st.session_state.get("dashboard_pipeline")
        if stale_pipeline is not None:
            stale_pipeline.activity_repository = repository
    return repository


def save_dashboard_settings(repository: Repository, user_id: str, settings: dict) -> None:
    with repository.database.lock:
        repository.database.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dashboard_settings (
                user_id TEXT PRIMARY KEY,
                settings TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        repository.database.connection.commit()
    save_method = getattr(repository, "save_dashboard_settings", None)
    if callable(save_method):
        save_method(user_id, settings)
        return
    with repository.database.lock:
        repository.database.connection.execute(
            """
            INSERT INTO dashboard_settings (user_id, settings, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                settings=excluded.settings,
                updated_at=excluded.updated_at
            """,
            (user_id, json.dumps(settings, sort_keys=True), datetime.now().isoformat()),
        )
        repository.database.connection.commit()


def local_position_manager(st) -> PositionManager:
    pipeline = st.session_state.get("dashboard_pipeline")
    if pipeline is not None:
        return pipeline.positions
    manager = PositionManager()
    for record in get_dashboard_repository(st).load_positions():
        manager.add(
            Position(
                record.symbol,
                Side(record.side),
                record.quantity,
                record.entry_price,
                record.stop_loss,
                record.entry_time,
                record.target_1,
                record.target_2,
            )
        )
    return manager


def broker_connection_state(st, settings, access_token: str) -> tuple[str, str]:
    if not broker_credentials_configured(settings):
        return "Not configured", "Add Kite API credentials in .env."
    if not access_token:
        return "Token required", "Authenticate with Kite to enable broker checks."
    checked = st.session_state.get("broker_connection_check")
    if checked:
        return checked["status"], checked["detail"]
    if st.session_state.get("dashboard_kite_client") is not None:
        return "Session connected", "Kite client is available in this dashboard session."
    return "Ready to connect", "Run a broker check before sending live orders."


def render_broker_reconciliation(st, settings, access_token: str) -> None:
    with st.expander("Broker reconciliation", expanded=False):
        if settings.trading_mode != TradingMode.LIVE:
            st.info("Broker reconciliation is available in LIVE mode. PAPER positions are maintained in the local ledger.", icon=":material/science:")
            return
        if not broker_credentials_configured(settings) or not access_token:
            st.warning("Connect Kite before checking broker positions.", icon=":material/lock:")
            return
        st.caption("Compare the local managed ledger with Kite net positions before resuming live entries after a restart or disconnect.")
        if st.button("Refresh broker positions", key="refresh_broker_positions", icon=":material/refresh:"):
            try:
                client = st.session_state.get("dashboard_kite_client") or connect_kite(settings, access_token)
                st.session_state.dashboard_kite_client = client
                broker_positions = [position for position in PositionsAPI(client.client).list() if int(position.get("quantity", 0) or 0) != 0]
                local = local_position_manager(st)
                missing_symbols = reconcile(local, broker_positions)
                broker_by_symbol = {str(position.get("tradingsymbol")): position for position in broker_positions}
                quantity_mismatches = []
                for symbol, position in local.positions.items():
                    broker_position = broker_by_symbol.get(symbol)
                    if broker_position is None:
                        continue
                    broker_quantity = int(broker_position.get("quantity", 0) or 0)
                    broker_side = "BUY" if broker_quantity > 0 else "SELL"
                    if abs(broker_quantity) != position.quantity or broker_side != position.side.value:
                        quantity_mismatches.append(symbol)
                st.session_state.broker_reconciliation = {
                    "checked_at": datetime.now(),
                    "broker_positions": broker_positions,
                    "missing_symbols": missing_symbols,
                    "quantity_mismatches": quantity_mismatches,
                }
            except Exception as error:
                logger.exception("Manual broker reconciliation failed")
                if "timed out" in str(error).lower():
                    st.error("Kite API did not respond in time. It may be under heavy load right now — wait a moment and click Refresh broker positions again.")
                else:
                    st.error(f"Broker positions could not be loaded: {error}")
        reconciliation = st.session_state.get("broker_reconciliation")
        if reconciliation is None:
            st.caption("No broker comparison has been run in this session.")
            return
        missing_symbols = reconciliation["missing_symbols"]
        quantity_mismatches = reconciliation["quantity_mismatches"]
        broker_positions = reconciliation["broker_positions"]
        if not missing_symbols and not quantity_mismatches:
            st.success("Local and broker positions match by symbol, side, and quantity.", icon=":material/check_circle:")
        else:
            st.error("Reconciliation found differences. Keep automatic entries halted until they are resolved.", icon=":material/error:")
            if missing_symbols:
                st.write(f"Symbol differences: {', '.join(missing_symbols)}")
            if quantity_mismatches:
                st.write(f"Side or quantity differences: {', '.join(quantity_mismatches)}")
        broker_display = [
            {
                "Symbol": position.get("tradingsymbol", ""),
                "Quantity": int(position.get("quantity", 0) or 0),
                "Average price": float(position.get("average_price", 0) or 0),
                "Last price": float(position.get("last_price", 0) or 0),
                "P&L": float(position.get("pnl", 0) or 0),
            }
            for position in broker_positions
        ]
        if broker_display:
            st.dataframe(
                pd.DataFrame(broker_display),
                width="stretch",
                hide_index=True,
                column_config={
                    "Average price": st.column_config.NumberColumn(format="₹%.2f"),
                    "Last price": st.column_config.NumberColumn(format="₹%.2f"),
                    "P&L": st.column_config.NumberColumn(format="₹%.2f"),
                },
            )
        st.caption(f"Last checked {reconciliation['checked_at'].strftime('%H:%M:%S')}")


def render_control_center(st, settings, activity: pd.DataFrame) -> None:
    st.subheader("Trading control center")
    access_token = runtime_access_token(st, settings)
    connection_status, connection_detail = broker_connection_state(st, settings, access_token)
    pipeline = st.session_state.get("dashboard_pipeline")
    managed_positions = local_position_manager(st).positions
    open_exposure = sum(position.entry_price * position.quantity for position in managed_positions.values())
    trades_today = int(activity.loc[activity["timestamp"].astype(str).str.startswith(date.today().isoformat()), "event_kind"].eq("exit_submitted").sum()) if not activity.empty else 0
    selected_watchlists = get_dashboard_repository(st).load_watchlists(dashboard_user_id(settings), selected_only=True)
    signal_engine_status = "Monitoring" if selected_watchlists else "Waiting"
    if st.session_state.get("emergency_halt") or (pipeline is not None and pipeline.halted):
        signal_engine_status = "Halted"
    checked = st.session_state.get("broker_connection_check")
    checked_time = checked["checked_at"].strftime("%H:%M:%S") if checked else "not checked"
    last_market_data = st.session_state.get("last_market_data_at")
    last_market_data_label = last_market_data.strftime("%H:%M:%S") if isinstance(last_market_data, datetime) else "not available"
    status_columns = st.columns(3)
    status_columns[0].metric("Broker", connection_status)
    status_columns[1].metric("Signal engine", signal_engine_status)
    status_columns[2].metric("Open positions", f"{len(managed_positions)} / {settings.max_open_positions}")
    st.caption(f"{connection_detail} Last check: {checked_time} · Last market data: {last_market_data_label}")
    action_columns = st.columns(3)
    if action_columns[0].button("Check broker connection", key="check_broker_connection", icon=":material/cloud_done:"):
        try:
            client = st.session_state.get("dashboard_kite_client") or connect_kite(settings, access_token)
            client.client.profile()
            st.session_state.dashboard_kite_client = client
            st.session_state.broker_connection_check = {
                "status": "Connected",
                "detail": "Kite authentication and API request succeeded.",
                "checked_at": datetime.now(),
            }
        except Exception as error:
            st.session_state.broker_connection_check = {
                "status": "Disconnected",
                "detail": str(error),
                "checked_at": datetime.now(),
            }
        st.rerun()
    action_columns[1].metric("Selected stocks", sum(len(item.symbols) for item in selected_watchlists))
    action_columns[2].metric("Trades today", f"{trades_today} / {settings.max_trades_per_day}", f"Exposure ₹{open_exposure:,.0f}")
    st.caption("The backend signal engine evaluates selected watchlists during market hours, whether this dashboard is open or not.")
    if st.session_state.get("emergency_halt"):
        st.warning("New entries are halted. Existing positions remain monitored.", icon=":material/pause_circle:")
        if st.button("Resume new entries", key="overview_resume_entries", icon=":material/play_arrow:"):
            if pipeline is not None:
                pipeline.resume_entries()
            st.session_state.emergency_halt = False
            st.rerun()
    else:
        with st.form("overview_emergency_halt_form"):
            halt_confirmed = st.checkbox("Confirm that new BUY/SELL entries should be halted.", key="overview_halt_confirmation")
            halt_submitted = st.form_submit_button("Emergency stop: halt new entries", type="secondary", width="stretch", icon=":material/stop_circle:")
        if halt_submitted:
            if not halt_confirmed:
                st.warning("Confirm the emergency halt before submitting it.")
            else:
                if pipeline is not None:
                    pipeline.halt_entries()
                st.session_state.automatic_enabled = False
                st.session_state.emergency_halt = True
                st.rerun()
    render_broker_reconciliation(st, settings, access_token)


def render_position_monitor(st, settings, access_token: str) -> None:
    pipeline = st.session_state.get("dashboard_pipeline")
    if pipeline is None:
        st.caption("No open positions. Positions are monitored automatically while this dashboard session is open.")
        return

    if pipeline.managed_positions:
        try:
            client = st.session_state.get("dashboard_kite_client") or connect_kite(settings, access_token)
            st.session_state.dashboard_kite_client = client
            events = pipeline.sync_broker_positions()
            position_symbols = set(pipeline.managed_positions)
            position_tokens = {token: symbol for token, symbol in pipeline.scanner.token_to_symbol.items() if symbol in position_symbols}
            live_ticks = load_live_quote_ticks(client.client, position_tokens) if position_tokens else []
            st.session_state.last_market_data_at = datetime.now()
            events.extend(pipeline.monitor_ticks(live_ticks))
            ema9_positions = {
                symbol: token
                for token, symbol in position_tokens.items()
                if pipeline.managed_positions.get(symbol) is not None
                and pipeline.managed_positions[symbol].exit_on_ema9_close
            }
            if ema9_positions:
                completed_candles = {
                    symbol: load_live_candles_from_client(client.client, token, "5minute", 5)
                    for symbol, token in ema9_positions.items()
                }
                events.extend(pipeline.monitor_candle_closes(completed_candles))
            record_dashboard_events(st, events)
        except Exception as error:
            logger.exception("Position monitoring cycle failed for %s open intraday position(s); positions were not reconciled against the broker this cycle", len(pipeline.managed_positions))
            st.warning(f"Position monitoring paused: {error}")

    positions = []
    for symbol, managed in pipeline.managed_positions.items():
        current_price = pipeline.last_prices.get(symbol, managed.position.entry_price)
        positions.append(
            {
                "Symbol": symbol,
                "Side": managed.position.side.value,
                "Quantity": managed.position.quantity,
                "Entry": managed.position.entry_price,
                "Last": current_price,
                "Stop": managed.trailing_stop.stop,
                "P&L": managed.position.unrealized_pnl(current_price),
            }
        )
    if positions:
        st.dataframe(
            pd.DataFrame(positions),
            width="stretch",
            hide_index=True,
            column_config={
                "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                "Last": st.column_config.NumberColumn(format="₹%.2f"),
                "Stop": st.column_config.NumberColumn(format="₹%.2f"),
                "P&L": st.column_config.NumberColumn(format="₹%.2f"),
            },
        )
        st.caption("Exit controls")
        for row_number, (symbol, managed) in enumerate(list(pipeline.managed_positions.items())):
            current_price = pipeline.last_prices.get(symbol, managed.position.entry_price)
            with st.container(border=True):
                st.markdown(
                    f"**{symbol}** · {managed.position.side.value} · {managed.position.quantity} shares · "
                    f"entry ₹{managed.position.entry_price:,.2f} · stop ₹{managed.trailing_stop.stop:,.2f}"
                )
                with st.form(f"overview_exit_form_{symbol}_{row_number}"):
                    exit_price = st.number_input(
                        "Exit value",
                        min_value=0.05,
                        value=round(float(current_price), 2),
                        step=0.05,
                        format="%.2f",
                        key=f"overview_exit_price_{symbol}_{row_number}",
                    )
                    confirmation_label = (
                        "I understand this will send a live MARKET exit order."
                        if settings.trading_mode == TradingMode.LIVE
                        else "I confirm this PAPER exit order."
                    )
                    confirmed = st.checkbox(confirmation_label, key=f"overview_exit_confirm_{symbol}_{row_number}")
                    submit_exit = st.form_submit_button(f"Exit {symbol}", type="primary", width="stretch")
                if submit_exit:
                    if not confirmed:
                        st.warning("Confirm the exit before submitting it.")
                    else:
                        event = pipeline.submit_manual_exit(symbol, float(exit_price))
                        record_dashboard_events(st, [event])
                        if event.kind == "exit_submitted":
                            st.success(f"{event.order_id}: {symbol} exit submitted.")
                        else:
                            st.error(f"Exit rejected: {event.reason}")
    else:
        st.success("All monitored positions are closed.")
    if pipeline.recent_closed_positions:
        st.subheader("Recently closed")
        st.caption("Closed trades are kept here after Zerodha confirms the broker-side exit.")
        closed_rows = [
            {
                "Symbol": closed.symbol,
                "Side": closed.side.value,
                "Quantity": closed.quantity,
                "Entry": closed.entry_price,
                "Exit": closed.exit_price,
                "Realized P&L": closed.pnl,
                "Closed at": closed.closed_at,
                "Exit order": closed.order_id or "Unavailable",
                "Reason": closed.reason,
            }
            for closed in pipeline.recent_closed_positions
        ]
        st.dataframe(
            pd.DataFrame(closed_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                "Exit": st.column_config.NumberColumn(format="₹%.2f"),
                "Realized P&L": st.column_config.NumberColumn(format="₹%.2f"),
                "Closed at": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
            },
        )
    recent_events = st.session_state.get("dashboard_events", [])[-5:]
    if recent_events:
        st.caption(" · ".join(f"{event.kind}: {event.symbol}" for event in recent_events))


def render_live_monitor(st, settings) -> None:
    @st.fragment(run_every=10)
    def render_live_monitor_content() -> None:
        st.markdown('<div class="eyebrow">Unified position tracker</div>', unsafe_allow_html=True)
        st.title("Live monitor")
        st.caption(
            "Every open LIVE-mode intraday and swing position, read straight from the database the "
            "standalone trailing-stop agent maintains -- cross-checked against Zerodha's own "
            "live positions so drift between the two is visible immediately. PAPER-mode positions are "
            "simulated locally and are not shown here or managed by the trailing-stop agent."
        )
        repository = get_dashboard_repository(st)

        heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
        heartbeat_age = (datetime.now() - heartbeat.heartbeat_at).total_seconds() if heartbeat else None
        if heartbeat is None:
            agent_state = "stopped"
        elif heartbeat.last_error:
            agent_state = "error"
        elif heartbeat_age is not None and heartbeat_age > 90:
            agent_state = "stalled"
        else:
            agent_state = "running"
        if agent_state in {"running", "stalled", "error"}:
            state_copy = {
                "running": ("Trailing-stop agent active", f"Last heartbeat {heartbeat_age:.0f}s ago -- protective stops are being kept current"),
                "stalled": ("Trailing-stop agent heartbeat is stale", f"No heartbeat in {heartbeat_age:.0f}s -- positions may not be protected; check the standalone agent process"),
                "error": ("Trailing-stop agent needs attention", escape((heartbeat.last_error if heartbeat else "")[:240] or "The latest agent cycle reported an error")),
            }[agent_state]
            render_scan_activity_banner(st, agent_state, *state_copy)
        else:
            warning_column, action_column = st.columns([3, 1])
            with warning_column:
                st.warning("Trailing-stop agent has no heartbeat yet -- start it to protect open positions.")
            with action_column:
                access_token = runtime_access_token(st, settings)
                start_disabled = not broker_credentials_configured(settings) or not access_token
                if st.button("Start agent now", icon=":material/play_arrow:", disabled=start_disabled, width="stretch"):
                    launch_trailing_stop_agent(
                        settings.kite_api_key,
                        settings.kite_api_secret.get_secret_value(),
                        access_token,
                        repository,
                        extra_env=trailing_agent_env_from_settings(settings),
                    )
                    st.rerun()

        records = [record for record in repository.load_positions() if record.trading_mode == "LIVE"]
        if not records:
            st.markdown('<div class="empty">No open LIVE positions.</div>', unsafe_allow_html=True)
            return

        activity = load_activity()[2]
        # "stop_trailed" is an ATR/EMA-driven move; "stop_rearmed" is a same-price re-placement
        # after Zerodha's overnight day-order expiry -- both are broker-side changes to the
        # resting SL-M and belong in the same history, or a freshly re-armed order (a new
        # order id at the broker, confirmed by "Broker status") would show no explanation at all.
        stop_update_kinds = ["stop_trailed", "stop_rearmed"]
        stop_trail_events = pd.DataFrame(columns=["timestamp", "symbol", "reason", "stop_loss", "previous_stop"])
        if not activity.empty:
            stop_trail_events = activity.loc[
                activity["event_kind"].isin(stop_update_kinds), ["timestamp", "symbol", "reason", "stop_loss", "previous_stop"]
            ].copy()
            stop_trail_events["timestamp"] = pd.to_datetime(stop_trail_events["timestamp"], errors="coerce")
        stop_trail_counts = stop_trail_events["symbol"].value_counts().to_dict() if not stop_trail_events.empty else {}
        # st.dataframe has no per-cell hover tooltip -- column_config "help" is one static string
        # for the whole column -- so instead of a hover, show the latest move's from/to prices
        # directly as a column: the most recent stop-price-moving activity row per symbol (a
        # re-arm never changes the price, so it's naturally skipped below via previous_stop).
        latest_stop_move: dict[str, str] = {}
        if not stop_trail_events.empty:
            for _, event_row in stop_trail_events.sort_values("timestamp").iterrows():
                previous_stop = event_row["previous_stop"]
                if pd.notna(previous_stop):
                    latest_stop_move[event_row["symbol"]] = f"₹{float(previous_stop):,.2f} → ₹{float(event_row['stop_loss']):,.2f}"

        access_token = runtime_access_token(st, settings)
        quotes: dict = {}
        broker_open_tradingsymbols: set[str] | None = None
        atr_values: dict[str, float] = {}
        if broker_credentials_configured(settings) and access_token:
            try:
                client = connect_kite(settings, access_token)
                keys = [record.symbol if ":" in record.symbol else f"NSE:{record.symbol}" for record in records]
                quotes = client.client.ltp(keys)
                broker_positions = client.client.positions().get("net", [])
                broker_open_tradingsymbols = {
                    str(position.get("tradingsymbol", "")).strip().upper()
                    for position in broker_positions
                    if int(position.get("quantity", 0) or 0) != 0
                }
                # A CNC (swing) buy drops out of positions() once it settles into a holding --
                # commonly the next trading day or two -- even though nothing was ever sold.
                # Without also checking holdings(), every swing position would eventually show
                # as "Not found at broker" here purely from settlement, a few days after entry.
                for holding in client.client.holdings():
                    total_quantity = int(holding.get("quantity", 0) or 0) + int(holding.get("t1_quantity", 0) or 0)
                    if total_quantity > 0:
                        broker_open_tradingsymbols.add(str(holding.get("tradingsymbol", "")).strip().upper())
            except Exception as error:
                st.warning(f"Live broker data unavailable right now: {error}")
            else:
                for record in records:
                    if record.instrument_token is None:
                        continue
                    try:
                        interval = "15minute" if record.position_type == "INTRADAY" else "day"
                        lookback_days = 5 if record.position_type == "INTRADAY" else SWING_LOOKBACK_DAYS
                        candles = load_live_candles_from_client(client.client, record.instrument_token, interval, lookback_days)
                        if len(candles) > 14:
                            value = float(compute_atr(candles, 14).iloc[-1])
                            if pd.notna(value) and value > 0:
                                atr_values[record.symbol] = value
                    except Exception:
                        logger.exception("ATR lookup failed for %s in the live monitor", record.symbol)

        def tradingsymbol(symbol: str) -> str:
            return symbol.split(":", 1)[1].strip().upper() if ":" in symbol else symbol.strip().upper()

        def build_rows(position_type: str) -> list[dict]:
            rows = []
            for record in records:
                if record.position_type != position_type:
                    continue
                key = record.symbol if ":" in record.symbol else f"NSE:{record.symbol}"
                live_quote = quotes.get(key) or {}
                last_price = float(live_quote.get("last_price", 0) or 0) or record.entry_price
                direction = 1 if record.side == "BUY" else -1
                pnl = (last_price - record.entry_price) * record.quantity * direction
                invested = record.entry_price * record.quantity
                pnl_pct = (pnl / invested * 100) if invested else 0.0
                stop_distance_pct = abs(last_price - record.stop_loss) / last_price * 100 if last_price else 0.0
                if broker_open_tradingsymbols is None:
                    broker_status = "Unknown"
                elif tradingsymbol(record.symbol) in broker_open_tradingsymbols:
                    broker_status = "Matches broker"
                else:
                    broker_status = "Not found at broker"
                rows.append(
                    {
                        "Symbol": record.symbol,
                        "Strategy": record.strategy_name or "-",
                        "Side": record.side,
                        "Qty": record.quantity,
                        "Entry": record.entry_price,
                        "Last": last_price,
                        "Stop": record.stop_loss,
                        "Stop distance %": stop_distance_pct,
                        "ATR": atr_values.get(record.symbol),
                        "ATR mult.": record.atr_multiplier,
                        "P&L": pnl,
                        "P&L %": pnl_pct,
                        "Target 1": record.target_1,
                        "Target 1 hit": "Yes" if record.target_1_hit else "No",
                        "Stop updates": int(stop_trail_counts.get(record.symbol, 0)),
                        "Last stop move": latest_stop_move.get(record.symbol, "-"),
                        "Protective order": record.protective_order_id or "Unavailable",
                        "Broker status": broker_status,
                        "Entered": record.entry_time,
                    }
                )
            return rows

        column_config = {
            "Entry": st.column_config.NumberColumn(format="₹%.2f"),
            "Last": st.column_config.NumberColumn(format="₹%.2f"),
            "Stop": st.column_config.NumberColumn(format="₹%.2f", help="The current SL-M trigger price resting at the broker."),
            "Stop distance %": st.column_config.NumberColumn(format="%.2f%%", help="How far the last traded price is from the stop, as a percentage of the last price: |Last - Stop| / Last x 100."),
            "ATR": st.column_config.NumberColumn(format="₹%.2f", help="The current ATR14 value in rupees -- the average true range over the last 14 completed candles (15-minute for intraday, daily for swing). Blank if there isn't enough candle history or live broker data yet."),
            "ATR mult.": st.column_config.NumberColumn(format="%.2f", help="The trailing-stop agent's ATR multiplier for this position. Its candidate stop is Last price minus (ATR14 x this multiplier) for a BUY, calculated on 15-minute candles for intraday and daily candles for swing -- and it only ever moves in your favor, never back toward the entry."),
            "P&L": st.column_config.NumberColumn(format="₹%.2f"),
            "P&L %": st.column_config.NumberColumn(format="%.2f%%", help="P&L as a percentage of entry price x quantity: (Last - Entry) / Entry x 100, signed for the trade's side."),
            "Target 1": st.column_config.NumberColumn(format="₹%.2f", help="The price that triggers the target-1 action (move stop to breakeven, or close the position, depending on strategy). Blank means the strategy didn't set one."),
            "Stop updates": st.column_config.NumberColumn(
                help="How many times the trailing-stop agent has moved or re-armed this position's SL-M since entry. Select the row to filter 'Recent stop-loss updates' below to just this symbol."
            ),
            "Last stop move": st.column_config.TextColumn(help="The most recent trailing-stop move for this position (old price → new price)."),
            "Entered": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
        }

        def render_table(title: str, position_type: str, table_key: str) -> str:
            st.subheader(title)
            rows = build_rows(position_type)
            if not rows:
                st.markdown('<div class="empty">No open positions.</div>', unsafe_allow_html=True)
                return ""
            frame = pd.DataFrame(rows).sort_values("Entered", ascending=False).reset_index(drop=True)
            # A LinkColumn ("?stop_symbol=...") used to drive this instead -- but Streamlit
            # renders that as a real <a href>, so clicking it forced a full browser navigation
            # to the bare query string. That tore down the whole app (new Streamlit session,
            # sidebar navigation reset to its default page) instead of just re-running this
            # fragment, which is why it flickered and dropped back to the very first workspace
            # page. Row selection re-runs in place -- no navigation, no lost session state.
            event = st.dataframe(
                frame,
                width="stretch",
                hide_index=True,
                column_config=column_config,
                on_select="rerun",
                selection_mode="single-row",
                key=table_key,
            )
            selected_rows = event.selection.rows if event and event.selection else []
            if selected_rows:
                return str(frame.iloc[selected_rows[0]]["Symbol"])
            return ""

        st.caption(
            "Stop distance % = |Last − Stop| ÷ Last × 100. Stop trailing = (ATR × ATR mult.) below the last price for a "
            "BUY (above it for a SELL) on 15-minute candles for intraday / daily candles for swing, moved only in your "
            "favor -- never back toward entry. ATR is the current ATR14 value in rupees, live from the same candles. "
            "Target 1 is the price that triggers that strategy's target-1 action (move stop to breakeven, or close "
            "outright); hover any column header for its exact definition. Select a row to filter its stop-loss "
            "history below."
        )
        selected_in_intraday = render_table("Intraday positions", "INTRADAY", "live_monitor_intraday_table")
        selected_in_swing = render_table("Swing positions", "SWING", "live_monitor_swing_table")
        selected_stop_symbol = selected_in_intraday or selected_in_swing

        if not stop_trail_events.empty:
            tracked_symbols = {record.symbol for record in records}
            recent_updates = stop_trail_events.loc[stop_trail_events["symbol"].isin(tracked_symbols)].sort_values("timestamp", ascending=False).head(30)
            if selected_stop_symbol:
                selected_updates = recent_updates.loc[recent_updates["symbol"] == selected_stop_symbol]
                updates_title = f"SL-M stop updates: {selected_stop_symbol} ({len(selected_updates)})"
            else:
                selected_updates = recent_updates
                updates_title = f"Recent stop-loss updates ({len(recent_updates)})"
            with st.expander(updates_title, expanded=bool(selected_stop_symbol)):
                st.caption(
                    "Every SL-M change the trailing-stop agent has made at the broker, most recent first -- including a "
                    "same-price re-arm after Zerodha's overnight day-order expiry, not just an actual trailing move. From "
                    "and To are the broker trigger prices (equal for a re-arm, since the level itself didn't change); "
                    "Calculation details is the exact trailing-stop math for a move, or the re-arm's reason otherwise."
                )
                detail_rows = selected_updates.copy()
                # Rows logged before the calculation-detail upgrade have no previous_stop and
                # phrase "reason" as "trailing stop moved from X to Y" instead of a formula --
                # fall back to parsing that legacy text so "From" isn't blank for older history.
                legacy_from = detail_rows["reason"].str.extract(r"from ([\d.]+)", expand=False).astype(float)
                detail_rows["From"] = detail_rows["previous_stop"].fillna(legacy_from)
                detail_rows["To"] = detail_rows["stop_loss"]
                detail_rows["Calculation details"] = detail_rows["reason"]
                st.dataframe(
                    detail_rows.rename(columns={"timestamp": "Time", "symbol": "Symbol"})[
                        ["Time", "Symbol", "From", "To", "Calculation details"]
                    ],
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
                        "From": st.column_config.NumberColumn(format="₹%.2f"),
                        "To": st.column_config.NumberColumn(format="₹%.2f"),
                        "Calculation details": st.column_config.TextColumn(width="large"),
                    },
                )

        if broker_open_tradingsymbols is not None:
            tracked_tradingsymbols = {tradingsymbol(record.symbol) for record in records}
            untracked = sorted(broker_open_tradingsymbols - tracked_tradingsymbols)
            if untracked:
                st.warning(
                    "Zerodha shows open position(s) this app isn't tracking at all -- "
                    "possibly a manual trade or a dropped record: " + ", ".join(untracked)
                )

        intraday_count = sum(1 for record in records if record.position_type == "INTRADAY")
        swing_count = len(records) - intraday_count
        st.caption(f"{intraday_count} intraday · {swing_count} swing position(s) · refreshes every 10 seconds.")

    render_live_monitor_content()


def render_pnl_statement(st, settings) -> None:
    @st.fragment(run_every=10)
    def render_pnl_statement_content() -> None:
        st.markdown('<div class="eyebrow">Trade record</div>', unsafe_allow_html=True)
        st.title("P&L statement")
        st.caption("Every open position (live) and every closed trade (realized), in one statement.")

        repository = get_dashboard_repository(st)
        open_records = repository.load_positions()
        closed_records = repository.load_trades()
        if not open_records and not closed_records:
            st.markdown('<div class="empty">No trades yet.</div>', unsafe_allow_html=True)
            return

        access_token = runtime_access_token(st, settings)
        quotes: dict = {}
        if open_records and broker_credentials_configured(settings) and access_token:
            try:
                client = connect_kite(settings, access_token)
                keys = [record.symbol if ":" in record.symbol else f"NSE:{record.symbol}" for record in open_records]
                quotes = client.client.ltp(keys)
            except Exception as error:
                st.warning(f"Live quotes unavailable right now: {error}")

        def _pnl_pct(pnl: float, entry_price: float, quantity: int) -> float:
            invested = entry_price * quantity
            return (pnl / invested * 100) if invested else 0.0

        # Built once (independent of the Show filter below) so the totals summarized at the
        # top of the page always reflect every position/trade, not just the ones the radio
        # currently displays -- switching the filter to "Open" or "Closed" shouldn't make the
        # portfolio's total P&L appear to change.
        open_rows = []
        for record in open_records:
            key = record.symbol if ":" in record.symbol else f"NSE:{record.symbol}"
            quote = quotes.get(key) or {}
            ltp = float(quote.get("last_price", 0) or 0) or record.entry_price
            direction = 1 if record.side == "BUY" else -1
            pnl = (ltp - record.entry_price) * record.quantity * direction
            open_rows.append(
                {
                    "Stock Name": record.symbol,
                    "Current Position": "OPEN",
                    "Entry Price": record.entry_price,
                    "Exit Price": None,
                    "LTP": ltp,
                    "P&L": pnl,
                    "P&L %": _pnl_pct(pnl, record.entry_price, record.quantity),
                    "SL Current Price": record.stop_loss,
                    "Used Strategy Name": record.strategy_name or "-",
                    "Entered": record.entry_time,
                    "Exited": None,
                    "Activity time": record.entry_time,
                    "_correlation_id": f"{record.symbol}:{record.entry_time.isoformat()}",
                }
            )
        closed_rows = []
        for trade in closed_records:
            closed_rows.append(
                {
                    "Stock Name": trade.symbol,
                    "Current Position": "CLOSED",
                    "Entry Price": trade.entry_price,
                    "Exit Price": trade.exit_price,
                    "LTP": None,
                    "P&L": trade.pnl,
                    "P&L %": _pnl_pct(trade.pnl, trade.entry_price, trade.quantity),
                    "SL Current Price": None,
                    "Used Strategy Name": trade.strategy_name or "-",
                    "Entered": trade.entry_time,
                    "Exited": trade.exit_time,
                    "Activity time": trade.exit_time,
                    "_correlation_id": f"{trade.symbol}:{trade.entry_time.isoformat()}",
                }
            )

        realized_pnl = sum(trade.pnl for trade in closed_records)
        unrealized_pnl = sum(row["P&L"] for row in open_rows)
        total_invested = sum(record.entry_price * record.quantity for record in open_records) + sum(
            trade.entry_price * trade.quantity for trade in closed_records
        )
        total_pnl = realized_pnl + unrealized_pnl
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested else 0.0

        def _ist_date(value):
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("Asia/Kolkata")
            return timestamp.date()

        today = pd.Timestamp.now(tz="Asia/Kolkata").date()
        today_closed_records = [trade for trade in closed_records if _ist_date(trade.exit_time) == today]
        # Today's realized leg is trades exited today; unrealized is folded in as-is (any open
        # position's P&L is inherently "as of today" regardless of which day it was entered).
        today_realized_pnl = sum(trade.pnl for trade in today_closed_records)
        today_pnl = today_realized_pnl + unrealized_pnl
        today_invested = sum(record.entry_price * record.quantity for record in open_records) + sum(
            trade.entry_price * trade.quantity for trade in today_closed_records
        )
        today_pnl_pct = (today_pnl / today_invested * 100) if today_invested else 0.0

        summary_columns = st.columns(5)
        # The delta arg on st.metric renders as an extra pill line below the value, which would
        # make only this one card taller than the others -- folding the % into the value string
        # instead keeps every card the same single-line height.
        summary_columns[0].metric("Total P&L", f"₹{total_pnl:,.2f} ({total_pnl_pct:+.2f}%)")
        summary_columns[1].metric(
            "Today P&L",
            f"₹{today_pnl:,.2f} ({today_pnl_pct:+.2f}%)",
            help="Realized P&L from trades exited today, plus unrealized P&L on every still-open position.",
        )
        summary_columns[2].metric("Total Realized P&L", f"₹{realized_pnl:,.2f}")
        summary_columns[3].metric("Total Unrealized P&L", f"₹{unrealized_pnl:,.2f}")
        summary_columns[4].metric("Positions", f"{len(open_records)} open · {len(closed_records)} closed")

        show_label_column, show_radio_column = st.columns([1, 11], vertical_alignment="center")
        show_label_column.markdown("**Show**")
        status_filter = show_radio_column.radio(
            "Show",
            ["All", "Open", "Closed"],
            horizontal=True,
            key="pnl_statement_filter",
            label_visibility="collapsed",
        )

        rows = []
        if status_filter in ("All", "Open"):
            rows.extend(open_rows)
        if status_filter in ("All", "Closed"):
            rows.extend(closed_rows)

        if not rows:
            st.markdown('<div class="empty">No trades match this filter.</div>', unsafe_allow_html=True)
            return

        def _normalize_activity_time(value):
            if value is None:
                return pd.Timestamp.min
            timestamp = pd.Timestamp(value)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.tz_convert("Asia/Kolkata").tz_localize(None)
            return timestamp

        frame = pd.DataFrame(rows)
        frame["Activity time"] = frame["Activity time"].apply(_normalize_activity_time)
        frame = frame.sort_values("Activity time", ascending=False).drop(columns="Activity time").reset_index(drop=True)
        # Row selection instead of a per-row link/button: a link column would force a full
        # browser navigation (see the Live monitor page's own "Stop updates" column history --
        # that's exactly what caused it to flicker and drop back to the wrong dashboard page).
        # Selecting a row re-runs only this fragment.
        event = st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            on_select="rerun",
            selection_mode="single-row",
            key="pnl_statement_table",
            column_config={
                "Entry Price": st.column_config.NumberColumn(format="₹%.2f"),
                "Exit Price": st.column_config.NumberColumn(format="₹%.2f"),
                "LTP": st.column_config.NumberColumn(format="₹%.2f"),
                "P&L": st.column_config.NumberColumn(format="₹%.2f"),
                "P&L %": st.column_config.NumberColumn(format="%.2f%%"),
                "SL Current Price": st.column_config.NumberColumn(format="₹%.2f"),
                "Entered": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
                "Exited": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
                "_correlation_id": None,
            },
        )
        st.caption("Select a row to see that trade's full activity below: entry, every SL-M update (and whether it succeeded), and why it closed.")

        selected_rows = event.selection.rows if event and event.selection else []
        if selected_rows:
            selected = frame.iloc[selected_rows[0]]
            decisions = repository.load_decisions(correlation_id=selected["_correlation_id"], limit=200)
            decisions.sort(key=lambda decision: decision.timestamp.isoformat())
            with st.expander(f"Trade activity: {selected['Stock Name']} ({len(decisions)})", expanded=True):
                if not decisions:
                    st.markdown(
                        '<div class="empty">No recorded activity for this trade yet -- it may predate the activity log, '
                        "or its entry wasn't made through the standalone trailing-stop agent.</div>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption(
                        "Details shows each stop trail's actual ATR calculation (e.g. \"ATR14 ₹15.35 × mult 1.50 = ₹23.03; "
                        "Last ₹2162.43 − ₹23.03 = ₹2139.40\") for anything trailed since this calculation-detail logging "
                        "was added -- a small, uneven move (a handful of paise) is normal: it's whatever ATR14 × the "
                        "multiplier came out to on that candle, not a rounded step. Older rows only have a plain "
                        "\"moved from X to Y\" line, since the calculation itself wasn't being saved yet when they happened."
                    )
                    # Every row for a closed trade carries the same final `outcome` (it's
                    # backfilled onto the whole correlation_id chain once the exit is known, so
                    # a future LLM pass can see what each earlier decision led to) -- repeating
                    # that identical number on all 20+ intermediate stop-trail rows reads as
                    # noise here, so it's only shown once, on the row that actually closed the
                    # trade. "Details" is where each stop trail's own math lives instead.
                    activity_rows = [
                        {
                            "Time": decision.timestamp,
                            "Event": decision.event_type,
                            "Result": decision.decision,
                            "Details": decision.rationale,
                            "Final P&L": (
                                f"₹{decision.outcome['pnl']:,.2f}"
                                if decision.event_type in {"exit"} and decision.outcome and "pnl" in decision.outcome
                                else "-"
                            ),
                        }
                        for decision in decisions
                    ]
                    st.dataframe(
                        pd.DataFrame(activity_rows),
                        width="stretch",
                        hide_index=True,
                        column_config={
                            "Time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm:ss"),
                            "Details": st.column_config.TextColumn(width="large"),
                        },
                    )

    render_pnl_statement_content()


def render_trailing_agent_auto_start_toggle(st, repository, user_id: str) -> bool:
    current_value = repository.get_auto_start_trailing_agent(user_id)
    enabled = st.checkbox(
        "Automatically start the trailing-stop agent whenever I log in",
        value=current_value,
        key="auto_start_trailing_agent_toggle",
        help=(
            "The standalone trailing-stop agent (scripts/run_trailing_stop_agent.py) will be launched "
            "as a detached background process each time a fresh access token becomes available here -- "
            "no manual .env editing needed. It keeps running even if this dashboard is closed."
        ),
    )
    if enabled != current_value:
        repository.set_auto_start_trailing_agent(user_id, enabled)
    return enabled


def render_kite_authentication(st, settings) -> None:
    st.markdown('<div class="eyebrow">Broker connection</div>', unsafe_allow_html=True)
    st.title("Kite authentication")
    if notice := st.session_state.pop("kite_auth_notice", ""):
        st.success(notice, icon=":material/check_circle:")
    if st.session_state.pop("kite_session_expired_notice", False):
        st.warning(
            "Your Kite session expired -- Zerodha invalidates access tokens daily with no refresh, "
            "so a fresh sign-in is required. Please log in again below.",
            icon=":material/schedule:",
        )

    if not broker_credentials_configured(settings):
        st.info("Add KITE_API_KEY and KITE_API_SECRET to .env before starting Kite login.", icon=":material/key:")
        return

    runtime_token = verified_kite_access_token(st, settings)
    repository = get_dashboard_repository(st)
    user_id = dashboard_user_id(settings)
    auto_start_enabled = render_trailing_agent_auto_start_toggle(st, repository, user_id)

    if runtime_token:
        st.success("Kite authenticated. Workspace unlocked.", icon=":material/check_circle:")
        agent_running = agent_heartbeat_is_fresh(repository)
        agent_heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
        status_column, action_column = st.columns([3, 1])
        with status_column:
            if agent_running:
                st.caption("Trailing-stop agent is active. See the Live monitor page for details.")
            elif agent_heartbeat and agent_heartbeat.last_error:
                st.error(f"Trailing-stop agent failed to start: {agent_heartbeat.last_error[:300]}", icon=":material/error:")
            else:
                st.caption("Trailing-stop agent is not currently running.")
        with action_column:
            if agent_running:
                if st.button("Stop agent", icon=":material/stop:", width="stretch"):
                    stopped = stop_trailing_stop_agent(repository)
                    st.session_state.kite_auth_notice = (
                        "Trailing-stop agent stopped. Open positions will not be protected or trailed until it's started again."
                        if stopped
                        else "No running trailing-stop agent process was found to stop; its status has been cleared."
                    )
                    st.rerun()
            else:
                if st.button("Start agent now", icon=":material/play_arrow:", width="stretch"):
                    launch_trailing_stop_agent(
                        settings.kite_api_key,
                        settings.kite_api_secret.get_secret_value(),
                        runtime_token,
                        repository,
                        extra_env=trailing_agent_env_from_settings(settings),
                    )
                    st.rerun()
        if st.button("Log out", icon=":material/logout:"):
            log_out_of_kite(st, repository, user_id)
            st.rerun()
        return

    request_token_from_url = st.query_params.get("request_token", "")
    if request_token_from_url:
        st.session_state.kite_request_token = request_token_from_url
        st.session_state.pop("kite_logged_out", None)
        st.query_params.clear()
    stored_request_token = st.session_state.get("kite_request_token", "")

    # Kite hands back a one-time request_token via the redirect (or a manual paste below); as
    # soon as one shows up, exchange it for an access token immediately instead of making the
    # user click a second "Generate access token" button -- one action (sign in) is all this
    # should take. `kite_request_token_attempted` stops a failed/expired token from being
    # retried on every rerun of this page.
    if stored_request_token and stored_request_token != st.session_state.get("kite_request_token_attempted", ""):
        st.session_state.kite_request_token_attempted = stored_request_token
        with st.spinner("Connecting to Kite..."):
            try:
                access_token = exchange_request_token(
                    settings.kite_api_key,
                    settings.kite_api_secret.get_secret_value(),
                    stored_request_token,
                )
            except AuthenticationError as error:
                st.session_state.kite_auth_error = str(error)
                st.session_state.pop("kite_request_token", None)
            else:
                st.session_state.kite_access_token = access_token.value
                repository.save_kite_access_token(user_id, access_token.value)
                st.session_state.kite_auth_notice = "Kite connected. Workspace navigation is now enabled."
                st.session_state.pop("kite_auth_error", None)
                st.session_state.pop("kite_request_token", None)
                st.session_state.pop("kite_request_token_attempted", None)
                st.session_state.pop("kite_logged_out", None)
                if auto_start_enabled:
                    maybe_autostart_trailing_agent(
                        repository,
                        settings.kite_api_key,
                        settings.kite_api_secret.get_secret_value(),
                        access_token.value,
                        extra_env=trailing_agent_env_from_settings(settings),
                    )
                st.rerun()

    with st.container(border=True):
        st.markdown(
            '<div class="auth-step-head">'
            '<div class="auth-step-number">1</div>'
            '<div class="auth-step-title">Connect to Kite</div>'
            "</div>",
            unsafe_allow_html=True,
        )
        login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={quote(settings.kite_api_key)}"
        col_button, col_status = st.columns([1, 2], vertical_alignment="center")
        with col_button:
            st.link_button("Sign in with Kite", login_url, type="primary", icon=":material/login:")
        with col_status:
            if error := st.session_state.pop("kite_auth_error", ""):
                st.error(error, icon=":material/error:")
            else:
                st.caption("Sign in to Zerodha Kite -- you'll be connected automatically when it redirects back here.")

    with st.expander("Paste tokens manually (optional)"):
        access_token_input = st.text_input(
            "Kite access token",
            type="password",
            key="kite_access_token_input",
            help="Paste an access token you already generated, skipping the steps above.",
        )
        if access_token_input.strip():
            st.session_state.kite_access_token = access_token_input.strip()
            repository.save_kite_access_token(user_id, access_token_input.strip())
            st.session_state.pop("kite_logged_out", None)
            if auto_start_enabled:
                maybe_autostart_trailing_agent(
                    repository,
                    settings.kite_api_key,
                    settings.kite_api_secret.get_secret_value(),
                    access_token_input.strip(),
                    extra_env=trailing_agent_env_from_settings(settings),
                )
        manual_request_token = st.text_input(
            "Kite request token",
            type="password",
            help="If the Kite redirect landed in a different tab, paste its request_token here.",
        )
        if manual_request_token.strip():
            st.session_state.kite_request_token = manual_request_token.strip()
            st.session_state.pop("kite_logged_out", None)


def dashboard_user_id(settings) -> str:
    return str(getattr(settings, "user_id", "default"))


def selected_watchlist_symbols(repository: Repository, user_id: str) -> dict[str, int]:
    symbols: dict[str, int] = {}
    tokens: set[int] = set()
    for watchlist in repository.load_watchlists(user_id, selected_only=True):
        for symbol, token in watchlist.symbols.items():
            if token not in tokens:
                symbols[symbol] = token
                tokens.add(token)
    dynamic_watchlist = repository.load_dynamic_watchlist(user_id)
    if dynamic_watchlist and dynamic_watchlist.selected:
        for symbol, token in dynamic_watchlist.symbols.items():
            if token not in tokens:
                symbols[symbol] = token
                tokens.add(token)
    return symbols


def render_dynamic_watchlist(st, settings, repository: Repository, user_id: str, source_watchlists: list[WatchlistRecord]) -> None:
    @st.fragment(run_every=60)
    def render_dynamic_section() -> None:
        st.divider()
        st.subheader(DYNAMIC_WATCHLIST_NAME)
        st.caption(
            "The scan starts at the first tick of the 09:15 candle and keeps symbols where LOW >= OPEN * 0.999 "
            "and CURRENT_PRICE > OPEN."
        )
        dynamic_watchlist = repository.load_dynamic_watchlist(user_id)
        source_names = [watchlist.name for watchlist in source_watchlists]
        if not source_names:
            st.info("Create a source watchlist before configuring the dynamic list.", icon=":material/info:")
            return

        default_source = dynamic_watchlist.source_name if dynamic_watchlist and dynamic_watchlist.source_name in source_names else source_names[0]
        source_index = source_names.index(default_source)
        today = pd.Timestamp.now(tz="Asia/Kolkata").date()
        with st.form("dynamic_watchlist_source_form"):
            source_name = st.selectbox("Source watchlist", source_names, index=source_index, key="dynamic_watchlist_source")
            evaluation_date = st.date_input("Evaluation date", value=today, max_value=today, key="dynamic_watchlist_date")
            apply_selection = st.form_submit_button("Apply selection", type="primary", icon=":material/check:")
        if apply_selection:
            current = repository.load_dynamic_watchlist(user_id)
            same_selection = current and current.source_name == source_name and current.session_date == evaluation_date.isoformat()
            repository.save_dynamic_watchlist(
                DynamicWatchlistRecord(
                    user_id=user_id,
                    source_name=source_name,
                    symbols=current.symbols if same_selection else {},
                    selected=current.selected if current else True,
                    require_breakout=current.require_breakout if current else False,
                    refreshed_at=current.refreshed_at if same_selection else None,
                    session_date=evaluation_date.isoformat(),
                )
            )
            st.session_state.pop("dynamic_watchlist_rows", None)
            dynamic_watchlist = repository.load_dynamic_watchlist(user_id)

        if dynamic_watchlist is None:
            st.info("Apply a source watchlist to initialize the dynamic list.", icon=":material/tune:")
            return

        selected_source = next(item for item in source_watchlists if item.name == dynamic_watchlist.source_name)
        if dynamic_watchlist.source_name == "Nifty 500" and len(selected_source.symbols) < 500:
            st.warning(
                f"This Nifty 500 source has only {len(selected_source.symbols)} symbols and may be stale. "
                "Sync the current constituents before comparing with Zerodha."
            )
            if st.button("Sync Nifty 500 constituents", icon=":material/sync:", key="dynamic_watchlist_sync_nifty500"):
                if not broker_credentials_configured(settings) or not runtime_access_token(st, settings):
                    st.error("Connect Kite before syncing Nifty 500 constituents.")
                else:
                    try:
                        with st.spinner("Loading current Nifty 500 constituents..."):
                            sync_client = connect_kite(settings, runtime_access_token(st, settings))
                            universe = load_nifty_index_universe_from_api(sync_client.client, "NIFTY 500")
                        synced_symbols = {
                            f"NSE:{row.symbol}": int(row.instrument_token)
                            for row in universe.itertuples()
                        }
                        repository.update_watchlist_symbols(user_id, selected_source.name, synced_symbols)
                        st.session_state.pop("dynamic_watchlist_rows", None)
                        st.session_state.dynamic_watchlist_refresh_requested = True
                        st.success(f"Synced {len(synced_symbols)} current Nifty 500 constituents.")
                        st.rerun()
                    except (OSError, RuntimeError, ValueError) as error:
                        st.error(f"Nifty 500 sync failed: {error}")

        selected = st.checkbox(
            "Use for Signals",
            value=dynamic_watchlist.selected,
            key="dynamic_watchlist_selected",
            help="Include the current filtered symbols in the backend signal engine.",
        )
        if selected != dynamic_watchlist.selected:
            repository.set_dynamic_watchlist_selection(user_id, selected)
            dynamic_watchlist = repository.load_dynamic_watchlist(user_id)

        now = pd.Timestamp.now(tz="Asia/Kolkata")
        selected_date = evaluation_date
        current_slot = auto_refresh_slot(now, settings.market_open) if selected_date == today else None
        stored_slot = dynamic_watchlist.refresh_slot if dynamic_watchlist.session_date == selected_date.isoformat() else None
        auto_refresh_due = current_slot is not None and current_slot != stored_slot
        refresh_column, status_column = st.columns([1, 3])
        with refresh_column:
            manual_refresh = st.button("Refresh list", icon=":material/refresh:", key="dynamic_watchlist_refresh", width="stretch")
        with status_column:
            if selected_date != today:
                st.caption("Historical date selected. Use Refresh list to load its completed candles.")
            elif current_slot is not None:
                st.caption(f"Auto-refresh active from the first tick; evaluating candle window {current_slot + 1} of {DYNAMIC_AUTO_REFRESH_CANDLES}.")
            else:
                st.caption("Automatic scanning runs during the first three 5-minute candle windows from 09:15 IST.")

        rows = st.session_state.get("dynamic_watchlist_rows")
        rows_source = st.session_state.get("dynamic_watchlist_rows_source")
        refresh_requested = st.session_state.pop("dynamic_watchlist_refresh_requested", False)
        if manual_refresh or auto_refresh_due or refresh_requested:
            if not broker_credentials_configured(settings) or not runtime_access_token(st, settings):
                if manual_refresh:
                    st.warning("Connect Kite before refreshing the dynamic list.", icon=":material/key:")
            else:
                try:
                    source_watchlist = next(item for item in source_watchlists if item.name == dynamic_watchlist.source_name)
                    progress_bar = st.progress(
                        0.0,
                        text=f"Refreshing dynamic watchlist: 0/{len(source_watchlist.symbols)} complete · {len(source_watchlist.symbols)} remaining",
                    )

                    def update_refresh_progress(completed: int, total: int, symbol: str) -> None:
                        remaining = total - completed
                        progress_bar.progress(
                            completed / total if total else 1.0,
                            text=f"Refreshing {split_watchlist_symbol(symbol)[1]}: {completed}/{total} complete · {remaining} remaining",
                        )

                    with st.spinner("Refreshing dynamic watchlist..."):
                        rows, errors = load_dynamic_watchlist_rows(
                            settings,
                            runtime_access_token(st, settings),
                            source_watchlist,
                            selected_date,
                            progress_callback=update_refresh_progress,
                        )
                    progress_bar.progress(
                        1.0,
                        text=f"Dynamic watchlist refresh complete: {len(source_watchlist.symbols)}/{len(source_watchlist.symbols)} complete · 0 remaining",
                    )
                    refreshed_at = datetime.now()
                    repository.save_dynamic_watchlist(
                        DynamicWatchlistRecord(
                            user_id=user_id,
                            source_name=dynamic_watchlist.source_name,
                            symbols={row["symbol"]: int(row["instrument_token"]) for row in rows.to_dict("records")},
                            selected=dynamic_watchlist.selected,
                            require_breakout=dynamic_watchlist.require_breakout,
                            updated_at=refreshed_at,
                            refreshed_at=refreshed_at,
                            session_date=selected_date.isoformat(),
                            refresh_slot=current_slot,
                        )
                    )
                    dynamic_watchlist = repository.load_dynamic_watchlist(user_id)
                    st.session_state.dynamic_watchlist_rows = rows
                    st.session_state.dynamic_watchlist_rows_source = dynamic_watchlist.source_name
                    rows_source = dynamic_watchlist.source_name
                    if errors:
                        st.warning(f"Some source instruments could not be loaded ({len(errors)}).")
                except (OSError, RuntimeError, ValueError) as error:
                    st.error(f"Dynamic watchlist refresh failed: {error}")

        if rows is None or rows_source != dynamic_watchlist.source_name:
            rows = pd.DataFrame()
        if dynamic_watchlist.symbols and rows.empty:
            rows = pd.DataFrame(
                [{"symbol": symbol, "instrument_token": token} for symbol, token in dynamic_watchlist.symbols.items()]
            )
        if dynamic_watchlist.refreshed_at:
            st.caption(f"Source: {dynamic_watchlist.source_name} · Date: {dynamic_watchlist.session_date} · Last refresh: {dynamic_watchlist.refreshed_at:%d %b %Y, %I:%M:%S %p}")
        metric_column, _ = st.columns([1, 4])
        metric_column.metric("Matching stocks", len(rows))
        if rows.empty:
            st.info("No source instruments currently meet all first-candle filters.", icon=":material/search_off:")
            return

        display = rows.copy()
        display["Symbol"] = display["symbol"].map(lambda value: split_watchlist_symbol(value)[1])
        display["TradingView"] = display["symbol"].map(tradingview_chart_url)
        display = display.rename(
            columns={
                "candle_time": "Candle time",
                "open": "Open",
                "low": "Low",
                "close": "Close",
                "volume": "Volume",
                "avg_volume_20": "Avg volume 20",
                "vwap": "VWAP",
                "first_candle_high": "First candle high",
                "current_price": "Current price",
            }
        )
        st.dataframe(
            display.reindex(
                columns=[
                    "Symbol",
                    "TradingView",
                    "Candle time",
                    "Open",
                    "Low",
                    "Close",
                    "Volume",
                    "Avg volume 20",
                    "VWAP",
                    "First candle high",
                    "Current price",
                ]
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                "Open": st.column_config.NumberColumn(format="₹%.2f"),
                "Low": st.column_config.NumberColumn(format="₹%.2f"),
                "Close": st.column_config.NumberColumn(format="₹%.2f"),
                "Volume": st.column_config.NumberColumn(format="%.0f"),
                "Avg volume 20": st.column_config.NumberColumn(format="%.0f"),
                "VWAP": st.column_config.NumberColumn(format="₹%.2f"),
                "First candle high": st.column_config.NumberColumn(format="₹%.2f"),
                "Current price": st.column_config.NumberColumn(format="₹%.2f"),
            },
        )

    render_dynamic_section()


def render_watchlist_stock_view(st, settings, repository: Repository, user_id: str, watchlist: WatchlistRecord, instruments: pd.DataFrame) -> None:
    st.divider()
    header, close = st.columns([5, 1])
    with header:
        st.subheader(f"{watchlist.name} stocks")
        st.caption(f"{len(watchlist.symbols)} instruments in this watchlist")
    with close:
        if st.button("Close", key=f"watchlist_close_{watchlist.name}", width="stretch"):
            st.session_state.pop("open_watchlist", None)
            st.rerun()

    search_filter = st.text_input("Filter stocks", placeholder="Filter by symbol or company", key=f"watchlist_filter_{watchlist.name}")
    rows = []
    for stored_symbol, token in sorted(watchlist.symbols.items()):
        exchange, tradingsymbol = split_watchlist_symbol(stored_symbol)
        match = instruments.loc[
            (instruments["exchange"] == exchange) & (instruments["tradingsymbol"] == tradingsymbol)
        ]
        company = str(match.iloc[0]["name"]) if not match.empty else ""
        if search_filter.strip() and search_filter.strip().upper() not in f"{tradingsymbol} {company}".upper():
            continue
        rows.append({"stored_symbol": stored_symbol, "token": int(token), "Symbol": tradingsymbol, "Company": company, "Exchange": exchange})

    page_size = 50
    page_count = max(1, (len(rows) + page_size - 1) // page_size)
    page = st.selectbox("Page", range(1, page_count + 1), format_func=lambda value: f"Page {value} of {page_count}", key=f"watchlist_page_{watchlist.name}")
    visible_rows = rows[(page - 1) * page_size : page * page_size]
    visible_frame = pd.DataFrame(visible_rows)
    prices = {}
    if not visible_frame.empty:
        try:
            quote_rows = visible_frame.rename(columns={"Symbol": "tradingsymbol", "Exchange": "exchange", "token": "instrument_token"})
            prices = load_watchlist_prices(settings, runtime_access_token(st, settings), quote_rows)
        except Exception:
            st.caption("Live prices are unavailable. The instrument list is still editable.")
    if visible_rows:
        display = pd.DataFrame(
            [
                {
                    "Symbol": row["Symbol"],
                    "Company": row["Company"],
                    "Exchange": row["Exchange"],
                    "Current Price": prices.get(row["token"]),
                    "Added On": (watchlist.updated_at or datetime.now()).strftime("%d %b %Y"),
                }
                for row in visible_rows
            ]
        )
        st.dataframe(
            display,
            width="stretch",
            hide_index=True,
            column_config={"Current Price": st.column_config.NumberColumn(format="₹%.2f")},
        )
    else:
        st.markdown('<div class="empty">No instruments match this filter.</div>', unsafe_allow_html=True)

    actions, add = st.columns([3, 1])
    with actions:
        if rows:
            remove_options = [row["stored_symbol"] for row in rows]
            remove_symbols = st.multiselect(
                "Remove stocks",
                remove_options,
                format_func=lambda value: f"{split_watchlist_symbol(value)[1]} · {split_watchlist_symbol(value)[0]}",
                key=f"watchlist_remove_select_{watchlist.name}",
            )
            if st.button("Remove selected", key=f"watchlist_remove_{watchlist.name}", icon=":material/delete:", disabled=not remove_symbols):
                updated_symbols = dict(watchlist.symbols)
                for symbol in remove_symbols:
                    updated_symbols.pop(symbol, None)
                repository.update_watchlist_symbols(user_id, watchlist.name, updated_symbols)
                st.rerun()
    with add:
        if st.button("Add Stock", key=f"watchlist_detail_add_{watchlist.name}", type="primary", icon=":material/add:"):
            st.session_state.add_target_watchlist = watchlist.name
            st.session_state.watchlist_batch_destinations = [watchlist.name]
            st.rerun()


def render_watchlists(st, settings) -> None:
    st.markdown('<div class="eyebrow">Backend signal scope</div>', unsafe_allow_html=True)
    header, create = st.columns([5, 1])
    with header:
        st.title("Watchlists")
        st.caption("Manage the stocks you want the signal engine to monitor.")
    with create:
        if st.button("Create Watchlist", type="primary", icon=":material/add:", width="stretch"):
            st.session_state.create_watchlist_open = True

    repository = get_dashboard_repository(st)
    user_id = dashboard_user_id(settings)
    watchlists = repository.load_watchlists(user_id)
    selected_watchlists = [watchlist for watchlist in watchlists if watchlist.selected]
    selected_symbols = selected_watchlist_symbols(repository, user_id)
    engine_status = repository.load_signal_engine_status(user_id)
    now = datetime.now()
    heartbeat_age = (now - engine_status.last_run_at).total_seconds() if engine_status else None
    engine_running = heartbeat_age is not None and heartbeat_age <= max(120, settings.signal_poll_seconds * 3)
    engine_label = "Running" if engine_running else "Ready to start from Scanner & signals"
    last_scan = engine_status.last_run_at.strftime("%I:%M:%S %p") if engine_status else "No scan recorded"
    status_color = "#20844b" if engine_running else "#b7791f"
    status_text = f"<strong style='color:{status_color}'>{engine_label}</strong>"
    st.markdown(
        f"""
        <div class="engine-panel">
            <div class="eyebrow">Signal engine</div>
            <div style="font-size:1.1rem; margin:5px 0 9px"><span style="color:{status_color}">●</span> {status_text}</div>
            <div style="color:#53645a; font-size:.9rem">Monitoring: {len(selected_watchlists)} watchlists · {len(selected_symbols)} stocks &nbsp;|&nbsp; Last scan: {last_scan}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    total_memberships = sum(len(item.symbols) for item in selected_watchlists)
    overlap_count = max(0, total_memberships - len(selected_symbols))
    metric_one, metric_two, metric_three, metric_four = st.columns(4)
    metric_one.metric("Watchlists", len(watchlists))
    metric_two.metric("Active lists", len(selected_watchlists))
    metric_three.metric("Unique monitored", len(selected_symbols))
    metric_four.metric("Overlapping entries", overlap_count)
    if overlap_count:
        memberships: dict[int, list[str]] = {}
        labels: dict[int, str] = {}
        for watchlist in selected_watchlists:
            for symbol, token in watchlist.symbols.items():
                memberships.setdefault(token, []).append(watchlist.name)
                labels[token] = split_watchlist_symbol(symbol)[1]
        with st.expander(f"View {overlap_count} overlapping instrument{'s' if overlap_count != 1 else ''}"):
            st.dataframe(
                pd.DataFrame(
                    [{"Symbol": labels[token], "Selected watchlists": ", ".join(names)} for token, names in memberships.items() if len(names) > 1]
                ),
                width="stretch",
                hide_index=True,
            )

    if engine_status and engine_status.last_error:
        st.warning(f"The backend reported an error on its last cycle: {engine_status.last_error}")

    if st.session_state.get("create_watchlist_open", False):
        with st.form("create_watchlist_form"):
            name = st.text_input("Watchlist name", placeholder="e.g. Momentum Stocks")
            form_actions = st.columns([1, 1, 4])
            with form_actions[0]:
                submitted = st.form_submit_button("Create watchlist", type="primary", icon=":material/check:")
            with form_actions[1]:
                cancelled = st.form_submit_button("Cancel", icon=":material/close:")
        if cancelled:
            st.session_state.create_watchlist_open = False
            st.rerun()
        if submitted:
            normalized_name = " ".join(name.strip().split())
            if not normalized_name:
                st.error("Watchlist name is required.")
            elif any(item.name.casefold() == normalized_name.casefold() for item in watchlists):
                st.error("A watchlist with that name already exists.")
            else:
                repository.save_watchlist(WatchlistRecord(user_id, normalized_name, {}, selected=False))
                st.session_state.create_watchlist_open = False
                st.session_state.add_target_watchlist = normalized_name
                st.session_state.watchlist_batch_destinations = [normalized_name]
                st.session_state.watchlist_created_notice = f"Watchlist '{normalized_name}' created. Search for a stock below to add it."
                st.rerun()

    if notice := st.session_state.pop("watchlist_created_notice", ""):
        st.success(notice)

    access_token = runtime_access_token(st, settings)
    instruments = st.session_state.get("watchlist_instruments")
    st.divider()
    st.subheader("Add stocks to your watchlists")
    st.caption("Search the full NSE and BSE instrument catalog by company name or trading symbol.")
    if not broker_credentials_configured(settings) or not access_token:
        st.info("Connect Kite to search market instruments. You can create empty watchlists before connecting.", icon=":material/lock:")
    else:
        if instruments is None:
            try:
                with st.spinner("Loading market instruments..."):
                    instruments = load_watchlist_instruments(settings, access_token)
                st.session_state.watchlist_instruments = instruments
            except Exception as error:
                st.error(f"Market instrument search is unavailable: {error}")
                instruments = pd.DataFrame()
        elif "instrument_type" not in instruments.columns or "instrument_key" not in instruments.columns:
            st.session_state.pop("watchlist_instruments", None)
            st.rerun()
        search_column, exchange_column, type_column = st.columns([3, 1, 1])
        with search_column:
            query = st.text_input("Search stocks", placeholder="Search company name or symbol...", key="watchlist_search")
        with exchange_column:
            exchange_filter = st.selectbox("Exchange", ["All exchanges", "NSE", "BSE"], key="watchlist_search_exchange")
        with type_column:
            type_options = ["All types", *sorted(instruments["instrument_type"].unique())] if instruments is not None and not instruments.empty else ["All types"]
            instrument_type_filter = st.selectbox("Instrument type", type_options, key="watchlist_search_type")
        selected_keys = set(st.session_state.get("watchlist_selected_instruments", []))
        if instruments is not None and not instruments.empty and len(query.strip()) >= 2:
            normalized_query = query.strip().upper()
            results = instruments.loc[
                instruments["tradingsymbol"].str.contains(normalized_query, na=False, regex=False)
                | instruments["name"].str.upper().str.contains(normalized_query, na=False, regex=False)
            ].copy()
            if exchange_filter != "All exchanges":
                results = results.loc[results["exchange"] == exchange_filter]
            if instrument_type_filter != "All types":
                results = results.loc[results["instrument_type"] == instrument_type_filter]
            results["rank"] = results["tradingsymbol"].str.startswith(normalized_query).map({True: 0, False: 1})
            results = results.sort_values(["rank", "tradingsymbol", "exchange"]).head(25)
            if results.empty:
                st.markdown('<div class="empty">No market instruments matched that search.</div>', unsafe_allow_html=True)
            else:
                st.caption(f"Showing {len(results)} of the matching market instruments")
                result_rows = [
                    {
                        "instrument_key": row.instrument_key,
                        "symbol": row.tradingsymbol,
                        "exchange": row.exchange,
                        "name": row.name,
                        "instrument_type": row.instrument_type,
                        "instrument_token": int(row.instrument_token),
                    }
                    for row in results.itertuples()
                ]
                visible_keys = {row["instrument_key"] for row in result_rows}
                select_all_column, clear_visible_column = st.columns([1, 1])
                with select_all_column:
                    if st.button("Select all visible", key="watchlist_select_all_visible", icon=":material/done_all:"):
                        st.session_state.watchlist_selected_instruments = sorted(selected_keys | visible_keys)
                        st.rerun()
                with clear_visible_column:
                    if st.button("Clear visible", key="watchlist_clear_visible", icon=":material/remove_done:", disabled=not selected_keys.intersection(visible_keys)):
                        st.session_state.watchlist_selected_instruments = sorted(selected_keys - visible_keys)
                        st.rerun()
                for row in result_rows:
                    pick_column, info_column = st.columns([0.35, 5])
                    with pick_column:
                        checked = st.checkbox(
                            "Select",
                            value=row["instrument_key"] in selected_keys,
                            key=f"watchlist_pick_{row['instrument_key']}",
                            label_visibility="collapsed",
                        )
                    if checked:
                        selected_keys.add(row["instrument_key"])
                    else:
                        selected_keys.discard(row["instrument_key"])
                    with info_column:
                        st.markdown(
                            f'<div class="search-result"><span class="search-symbol">{row["symbol"]}</span> <span class="search-name">{row["name"] or "Market instrument"} · {row["exchange"]}</span><div class="result-meta">{row["instrument_type"]} · Instrument token {row["instrument_token"]}</div></div>',
                            unsafe_allow_html=True,
                        )
                st.session_state.watchlist_selected_instruments = sorted(selected_keys)
                toolbar_left, toolbar_right = st.columns([4, 1])
                with toolbar_left:
                    st.caption(f"{len(selected_keys)} instrument{'s' if len(selected_keys) != 1 else ''} selected")
                with toolbar_right:
                    if st.button("Clear", key="watchlist_clear_selection", icon=":material/close:", disabled=not selected_keys):
                        st.session_state.watchlist_selected_instruments = []
                        st.rerun()

        selected_rows = [
            {
                "instrument_key": row.instrument_key,
                "symbol": row.tradingsymbol,
                "exchange": row.exchange,
                "name": row.name,
                "instrument_token": int(row.instrument_token),
            }
            for row in instruments.loc[instruments["instrument_key"].isin(selected_keys)].itertuples()
        ] if instruments is not None and not instruments.empty else []
        if selected_rows:
            destination_names = [item.name for item in watchlists]
            default_destination = st.session_state.get("add_target_watchlist")
            if "watchlist_batch_destinations" not in st.session_state:
                st.session_state.watchlist_batch_destinations = [default_destination] if default_destination in destination_names else []
            with st.container(border=True):
                st.markdown(f'<div class="selection-tray"><strong>{len(selected_rows)} instrument{"s" if len(selected_rows) != 1 else ""} ready to add</strong><br><span class="result-meta">{", ".join(row["symbol"] for row in selected_rows[:6])}{" · …" if len(selected_rows) > 6 else ""}</span></div>', unsafe_allow_html=True)
                destinations = st.multiselect(
                    "Add selected instruments to",
                    destination_names,
                    key="watchlist_batch_destinations",
                    disabled=not destination_names,
                )
                if st.button("Add selected to watchlists", type="primary", icon=":material/library_add:", key="watchlist_batch_add", disabled=not destinations):
                    additions = add_instruments_to_watchlists(repository, user_id, watchlists, selected_rows, destinations)
                    st.session_state.watchlist_selected_instruments = []
                    st.session_state.pop("watchlist_batch_destinations", None)
                    st.session_state.pop("add_target_watchlist", None)
                    st.success(f"Added {additions} new instrument{'s' if additions != 1 else ''} to {len(destinations)} watchlist{'s' if len(destinations) != 1 else ''}.")
                    st.rerun()

    with st.expander("Bulk add stocks", expanded=False):
        st.caption("Paste symbols separated by commas, spaces, or new lines. Only equity instruments from the selected exchange are added.")
        if st.session_state.pop("watchlist_bulk_clear", False):
            st.session_state.watchlist_bulk_symbols = ""
        bulk_text = st.text_area(
            "Stock symbols",
            placeholder="HFCL, INDUSTOWER, SAGILITY\nAFFLE, COFORGE, CYIENT",
            height=120,
            key="watchlist_bulk_symbols",
        )
        bulk_exchange_column, bulk_destination_column = st.columns(2)
        with bulk_exchange_column:
            bulk_exchange = st.selectbox("Exchange", ["NSE", "BSE"], key="watchlist_bulk_exchange")
        with bulk_destination_column:
            bulk_destinations = [item.name for item in watchlists]
            bulk_default = st.session_state.get("add_target_watchlist")
            bulk_default_index = bulk_destinations.index(bulk_default) if bulk_default in bulk_destinations else 0
            bulk_destination = st.selectbox(
                "Add to watchlist",
                bulk_destinations or ["Create a watchlist first"],
                index=bulk_default_index if bulk_destinations else 0,
                disabled=not bulk_destinations,
                key="watchlist_bulk_destination",
            )
        parsed_bulk_symbols = parse_bulk_symbols(bulk_text)
        bulk_rows, missing_bulk_symbols = resolve_bulk_stock_instruments(
            instruments if instruments is not None else pd.DataFrame(),
            bulk_text,
            bulk_exchange,
        )
        if parsed_bulk_symbols:
            found_count = len(bulk_rows)
            st.caption(f"{len(parsed_bulk_symbols)} unique symbols pasted · {found_count} ready to add")
        if bulk_rows:
            st.dataframe(
                pd.DataFrame(
                    [{"Symbol": row["symbol"], "Company": row["name"] or "Market instrument", "Exchange": row["exchange"]} for row in bulk_rows]
                ),
                width="stretch",
                hide_index=True,
                height=min(280, 38 * len(bulk_rows) + 38),
            )
        if missing_bulk_symbols:
            with st.expander(f"Not found on {bulk_exchange} ({len(missing_bulk_symbols)})"):
                st.caption(", ".join(missing_bulk_symbols))
        if not parsed_bulk_symbols:
            st.info("Paste stock symbols above to preview the instruments before adding them.")
        elif instruments is None or instruments.empty:
            st.warning("Connect Kite to load the market instrument catalog before using bulk add.")
        elif not bulk_rows:
            st.warning(f"None of the pasted symbols matched equity instruments on {bulk_exchange}.")
        if st.button(
            f"Add {len(bulk_rows)} stock{'s' if len(bulk_rows) != 1 else ''} to watchlist",
            type="primary",
            icon=":material/library_add:",
            key="watchlist_bulk_add",
            disabled=not bulk_rows or not bulk_destinations,
        ):
            additions = add_instruments_to_watchlists(repository, user_id, watchlists, bulk_rows, [bulk_destination])
            st.session_state.watchlist_bulk_clear = True
            st.session_state.add_target_watchlist = bulk_destination
            st.success(f"Added {additions} new stock{'s' if additions != 1 else ''} to {bulk_destination}.")
            st.rerun()

    with st.expander("Optional: bulk import an index", expanded=False):
        st.caption("Useful for starting a list quickly. This is an import shortcut, not the signal universe.")
        import_index = st.selectbox("Index", SUPPORTED_INDEXES, index=0, key="watchlist_import_index")
        if st.button("Load constituents", icon=":material/download:", key="watchlist_import_load"):
            if not broker_credentials_configured(settings) or not access_token:
                st.error("Connect Kite before importing index constituents.")
            else:
                try:
                    client = connect_kite(settings, access_token)
                    st.session_state.watchlist_import_universe = load_nifty_index_universe_from_api(client.client, import_index)
                except (OSError, RuntimeError, ValueError) as error:
                    st.error(f"Index import could not be loaded: {error}")
        import_universe = st.session_state.get("watchlist_import_universe")
        if import_universe is not None:
            destination_options = [item.name for item in watchlists] + ["Create new watchlist"]
            destination = st.selectbox("Add imported stocks to", destination_options, key="watchlist_import_destination")
            if st.button("Add imported stocks", type="primary", icon=":material/add:", key="watchlist_import_add"):
                if destination == "Create new watchlist":
                    st.warning("Create a named watchlist above first, then import stocks into it.")
                else:
                    existing = next(item for item in watchlists if item.name == destination)
                    imported_symbols = {f"NSE:{row.symbol}": int(row.instrument_token) for row in import_universe.itertuples()}
                    repository.update_watchlist_symbols(user_id, destination, {**existing.symbols, **imported_symbols})
                    st.success(f"Added {len(imported_symbols)} index constituents to {destination}.")
                    st.rerun()

    st.divider()
    st.subheader("My watchlists")
    if not watchlists:
        st.markdown('<div class="empty">No watchlists yet. Create one above, then add stocks from market search.</div>', unsafe_allow_html=True)
    else:
        filter_column, view_column = st.columns([3, 2])
        with filter_column:
            watchlist_filter = st.text_input("Find a watchlist", placeholder="Search by list name", key="watchlist_card_filter")
        with view_column:
            watchlist_view = st.segmented_control("Show", ["All", "Active", "Empty"], default="All", key="watchlist_card_view")
        watchlist_view = watchlist_view or "All"
        visible_watchlists = [
            item
            for item in watchlists
            if (not watchlist_filter.strip() or watchlist_filter.strip().casefold() in item.name.casefold())
            and (watchlist_view == "All" or (watchlist_view == "Active" and item.selected) or (watchlist_view == "Empty" and not item.symbols))
        ]
        st.caption(f"Showing {len(visible_watchlists)} of {len(watchlists)} watchlists")
        card_columns = st.columns(2)
        for index, watchlist in enumerate(visible_watchlists):
            with card_columns[index % 2]:
                with st.container(border=True):
                    top, menu = st.columns([5, 1])
                    with top:
                        st.markdown(f"### {watchlist.name}")
                        st.markdown(f'<div class="watchlist-count">{len(watchlist.symbols)} stocks</div>', unsafe_allow_html=True)
                    with menu:
                        with st.popover("", icon=":material/more_horiz:", use_container_width=True):
                            rename_to = st.text_input("Rename", value=watchlist.name, key=f"watchlist_rename_input_{watchlist.name}")
                            if st.button("Rename", key=f"watchlist_rename_{watchlist.name}", icon=":material/edit:"):
                                normalized_name = " ".join(rename_to.strip().split())
                                if not normalized_name:
                                    st.error("Watchlist name is required.")
                                elif normalized_name != watchlist.name and any(item.name.casefold() == normalized_name.casefold() for item in watchlists):
                                    st.error("A watchlist with that name already exists.")
                                else:
                                    repository.rename_watchlist(user_id, watchlist.name, normalized_name)
                                    if st.session_state.get("open_watchlist") == watchlist.name:
                                        st.session_state.open_watchlist = normalized_name
                                    st.rerun()
                            confirm_delete = st.checkbox("Confirm delete", key=f"watchlist_delete_confirm_{watchlist.name}")
                            if st.button("Delete", key=f"watchlist_delete_{watchlist.name}", icon=":material/delete:", disabled=not confirm_delete):
                                repository.delete_watchlist(user_id, watchlist.name)
                                if st.session_state.get("open_watchlist") == watchlist.name:
                                    st.session_state.pop("open_watchlist", None)
                                st.rerun()
                    preview = [split_watchlist_symbol(symbol)[1] for symbol in list(watchlist.symbols)[:5]]
                    preview_text = " · ".join(preview) if preview else "No stocks added yet"
                    if len(watchlist.symbols) > 5:
                        preview_text += f" · +{len(watchlist.symbols) - 5} more"
                    st.markdown(f'<div class="symbol-preview">{preview_text}</div>', unsafe_allow_html=True)
                    selected = st.checkbox("Use for Signals", value=watchlist.selected, key=f"watchlist_selected_{watchlist.name}")
                    if selected != watchlist.selected:
                        repository.set_watchlist_selection(user_id, watchlist.name, selected)
                        st.rerun()
                    view, add_stock = st.columns(2)
                    with view:
                        if st.button("View Stocks", key=f"watchlist_view_{watchlist.name}", width="stretch", icon=":material/list:"):
                            st.session_state.open_watchlist = watchlist.name
                            st.rerun()
                    with add_stock:
                        if st.button("Add Stock", key=f"watchlist_add_{watchlist.name}", width="stretch", icon=":material/add:"):
                            st.session_state.add_target_watchlist = watchlist.name
                            st.session_state.watchlist_batch_destinations = [watchlist.name]
                            st.rerun()
        if not visible_watchlists:
            st.markdown('<div class="empty">No watchlists match the current filter.</div>', unsafe_allow_html=True)

    render_dynamic_watchlist(st, settings, repository, user_id, watchlists)

    st.divider()
    st.subheader("Signal monitoring")
    if selected_watchlists:
        selected_names = " · ".join(item.name for item in selected_watchlists)
        st.markdown(
            f"<div class='engine-panel'><strong>● Signal engine {engine_label.lower()}</strong><br><span style='color:#53645a'>Selected watchlists: {selected_names}<br>Total unique stocks monitored: <b>{len(selected_symbols)}</b></span></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown('<div class="empty">Select a watchlist with “Use for Signals” to define the backend signal universe.</div>', unsafe_allow_html=True)

    open_name = st.session_state.get("open_watchlist")
    if open_name:
        open_watchlist = next((item for item in repository.load_watchlists(user_id) if item.name == open_name), None)
        if open_watchlist is not None:
            render_watchlist_stock_view(st, settings, repository, user_id, open_watchlist, instruments if instruments is not None else pd.DataFrame(columns=["exchange", "tradingsymbol", "name"]))


def load_persisted_signal_frame(repository: Repository, user_id: str, limit: int = 100) -> pd.DataFrame:
    signals = repository.load_signals(user_id, limit)
    if not signals:
        return pd.DataFrame(columns=["Symbol", "Side", "Price", "VWAP", "EMA20", "Stop loss", "Candle time", "Reason"])
    return pd.DataFrame(
        [
            {
                "Symbol": signal.symbol,
                "TradingView": tradingview_chart_url(signal.symbol),
                "Side": signal.side,
                "Price": signal.price,
                "VWAP": signal.vwap,
                "EMA20": signal.ema20,
                "Stop loss": signal.stop_loss,
                "Candle time": signal.signal_timestamp,
                "Reason": signal.reason,
            }
            for signal in signals
        ]
    )


def tradingview_chart_url(symbol: str) -> str:
    exchange, tradingsymbol = split_watchlist_symbol(symbol)
    return f"https://in.tradingview.com/chart/?symbol={quote(f'{exchange}:{tradingsymbol}', safe='')}"


def start_dashboard_signal_engine(
    st,
    settings,
    access_token: str,
    user_id: str,
    selected_strategy: str = EMA_PROGRESSIVE_LIVE_LABEL,
    pre_spike_config: PreSpikeMomentumConfig | None = None,
):
    engine = st.session_state.get("dashboard_signal_engine")
    if engine is not None and engine.running:
        return engine
    if engine is not None:
        stale_database = st.session_state.pop("dashboard_signal_database", None)
        if stale_database is not None:
            stale_database.close()
        st.session_state.pop("dashboard_signal_engine", None)
    strategy = build_live_signal_strategy(selected_strategy, settings.signal_timeframe, pre_spike_config)
    engine, database = build_signal_engine(
        settings,
        access_token,
        user_id=user_id,
        strategy=strategy,
        legacy_signals_enabled=False,
        enable_progressive_strategy=selected_strategy == EMA_PROGRESSIVE_LIVE_LABEL,
    )
    engine.start()
    st.session_state.dashboard_signal_engine = engine
    st.session_state.dashboard_signal_database = database
    st.session_state.dashboard_signal_strategy = selected_strategy
    st.session_state.dashboard_pre_spike_config = pre_spike_config
    return engine


def stop_dashboard_signal_engine(st) -> None:
    engine = st.session_state.pop("dashboard_signal_engine", None)
    database = st.session_state.pop("dashboard_signal_database", None)
    stopped = True
    if engine is not None:
        stopped = engine.stop()
    if database is not None and stopped:
        database.close()
    elif database is not None:
        # Keep the connection alive until the worker finishes its current broker call.
        st.session_state.dashboard_signal_database = database
        st.session_state.dashboard_signal_engine = engine


def render_pre_spike_signal_table(st, repository: Repository, user_id: str) -> None:
    events = repository.load_pre_spike_events(user_id, PreSpikeMomentumStrategy.name, active_only=False, limit=100)
    st.markdown('<div class="eyebrow">Pre-Spike Momentum</div>', unsafe_allow_html=True)
    st.caption("Completed 5-minute candles only. Signals require price expansion or breakout plus a demand condition.")
    if not events:
        st.caption("No PRE_SPIKE_MOMENTUM events are available for the selected watchlists.")
        return
    rows = []
    for event in events:
        metrics = event.latest_metadata
        rows.append(
            {
                "Stock": event.symbol,
                "TradingView": tradingview_chart_url(event.symbol),
                "Signal": metrics.get("signal_type", "EARLY_MOMENTUM"),
                "Status": event.status,
                "Event ID": event.event_id,
                "Score": event.latest_score,
                "Highest score": event.highest_score,
                "Current price": event.latest_price,
                "Entry": event.entry_price,
                "RVOL": metrics.get("rvol"),
                "5m change %": metrics.get("price_change_pct"),
                "VWAP": metrics.get("vwap"),
                "EMA9": metrics.get("ema9"),
                "EMA20": metrics.get("ema20"),
                "EMA50": metrics.get("ema50"),
                "EMA200": metrics.get("ema200"),
                "PDH breakout": "Yes" if metrics.get("previous_day_breakout") else "No",
                "20D high breakout": "Yes" if metrics.get("twenty_day_breakout") else "No",
                "Volume buildup": metrics.get("volume_buildup_ratio"),
                "Compression": "Yes" if metrics.get("range_compression") else "No",
                "Close location": metrics.get("close_location"),
                "Trigger time": event.trigger_time,
                "Latest time": event.latest_time,
                "Reason": event.reason,
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        height=520,
        hide_index=True,
        column_config={
            "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
            "Status": st.column_config.TextColumn(),
            "Score": st.column_config.NumberColumn(format="%d/100"),
            "Highest score": st.column_config.NumberColumn(format="%d/100"),
            "Current price": st.column_config.NumberColumn(format="₹%.2f"),
            "Entry": st.column_config.NumberColumn(format="₹%.2f"),
            "RVOL": st.column_config.NumberColumn(format="%.2fx"),
            "5m change %": st.column_config.NumberColumn(format="%.2f%%"),
            "Volume buildup": st.column_config.NumberColumn(format="%.2fx"),
            "Close location": st.column_config.NumberColumn(format="%.0%%"),
            "VWAP": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA50": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
        },
    )


def render_previous_day_high_signal_table(st, repository: Repository, user_id: str) -> None:
    signals = [
        signal
        for signal in repository.load_signals(user_id, 100)
        if signal.strategy == PreviousDayHighBreakoutStrategy.name
    ]
    st.markdown('<div class="eyebrow">Previous-day high breakout</div>', unsafe_allow_html=True)
    st.caption("BUY signals trigger when a completed 5-minute candle crosses above the previous trading day's high.")
    if not signals:
        st.caption("No previous-day high breakout signals are available for the selected watchlists.")
        return
    table = pd.DataFrame(
        [
            {
                "Stock": signal.symbol,
                "TradingView": tradingview_chart_url(signal.symbol),
                "Current price": signal.price,
                "Previous day high": signal.metadata.get("previous_day_high"),
                "Stop loss": signal.stop_loss,
                "Trigger time": signal.signal_timestamp,
                "Reason": signal.reason,
            }
            for signal in signals
        ]
    )
    st.dataframe(
        table,
        width="stretch",
        hide_index=True,
        column_config={
            "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
            "Current price": st.column_config.NumberColumn(format="₹%.2f"),
            "Previous day high": st.column_config.NumberColumn(format="₹%.2f"),
            "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
            "Trigger time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
        },
    )


def render_ema200_close_signal_table(st, repository: Repository, user_id: str) -> None:
    signals = repository.load_signals(user_id, 100, strategy=Ema200CloseStrategy.name)
    st.markdown('<div class="eyebrow">EMA 200 close-above signals</div>', unsafe_allow_html=True)
    st.caption("BUY signals trigger on every newly completed candle whose close is above EMA 200.")
    if not signals:
        st.caption("No EMA 200 close-above signals are available for the selected watchlists.")
        return
    rows = []
    for signal in signals:
        ema200 = signal.metadata.get("ema200")
        distance = ((signal.price / float(ema200)) - 1) * 100 if ema200 else None
        rows.append(
            {
                "Stock": signal.symbol,
                "TradingView": tradingview_chart_url(signal.symbol),
                "Signal": signal.side,
                "Current price": signal.price,
                "EMA200": ema200,
                "Distance above EMA200": distance,
                "Candle open": signal.metadata.get("candle_open"),
                "Candle high": signal.metadata.get("candle_high"),
                "Candle low": signal.metadata.get("candle_low"),
                "Trigger time": signal.signal_timestamp,
                "Reason": signal.reason,
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        height=520,
        hide_index=True,
        column_config={
            "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
            "Current price": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
            "Distance above EMA200": st.column_config.NumberColumn(format="%.2f%%"),
            "Candle open": st.column_config.NumberColumn(format="₹%.2f"),
            "Candle high": st.column_config.NumberColumn(format="₹%.2f"),
            "Candle low": st.column_config.NumberColumn(format="₹%.2f"),
            "Trigger time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
        },
    )


def render_high_conviction_signal_table(st, repository: Repository, user_id: str) -> None:
    signals = repository.load_signals(user_id, 100, strategy=HighConvictionLongStrategy.name)
    st.markdown('<div class="eyebrow">High-conviction long signals</div>', unsafe_allow_html=True)
    st.caption("Signals require a completed 5-minute candle with breakout, volume expansion, trend alignment, VWAP, and bullish candle confirmation.")
    if not signals:
        st.caption("No high-conviction long signals are available for the selected watchlists.")
        return
    rows = []
    for signal in signals:
        metadata = signal.metadata
        rows.append(
            {
                "Stock": signal.symbol,
                "TradingView": tradingview_chart_url(signal.symbol),
                "Score": signal.score,
                "Entry": signal.price,
                "Stop loss": signal.stop_loss,
                "Target 1": signal.target_1,
                "Target 2": signal.target_2,
                "RVOL": metadata.get("relative_volume"),
                "VWAP": metadata.get("vwap"),
                "EMA9": metadata.get("ema9"),
                "EMA20": metadata.get("ema20"),
                "EMA50": metadata.get("ema50"),
                "EMA200": metadata.get("ema200"),
                "5m change %": metadata.get("price_change_percent"),
                "Candle strength": metadata.get("candle_strength"),
                "Trigger time": signal.signal_timestamp,
                "Reason": signal.reason,
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        height=520,
        hide_index=True,
        column_config={
            "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
            "Score": st.column_config.NumberColumn(format="%d/100"),
            "Entry": st.column_config.NumberColumn(format="₹%.2f"),
            "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
            "Target 1": st.column_config.NumberColumn(format="₹%.2f"),
            "Target 2": st.column_config.NumberColumn(format="₹%.2f"),
            "RVOL": st.column_config.NumberColumn(format="%.2fx"),
            "VWAP": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA50": st.column_config.NumberColumn(format="₹%.2f"),
            "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
            "5m change %": st.column_config.NumberColumn(format="%.2f%%"),
            "Candle strength": st.column_config.NumberColumn(format="%.0%%"),
            "Trigger time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
        },
    )


def create_swing_auto_trader(client, mode, trailing_multiplier: float, strategy_name: str, repository=None):
    try:
        return SwingAutoTrader(client, mode, trailing_multiplier, strategy_name, repository=repository)
    except TypeError as error:
        message = str(error)
        stale_signature = "SwingAutoTrader.__init__" in message and (
            "positional arguments" in message or "unexpected keyword argument" in message
        )
        if not stale_signature:
            raise
        # Streamlit can retain the imported class while source files hot-reload.
        import importlib
        import app.execution.swing_auto_trader as swing_auto_trader_module

        refreshed_module = importlib.reload(swing_auto_trader_module)
        return refreshed_module.SwingAutoTrader(client, mode, trailing_multiplier, strategy_name, repository=repository)


SWING_EVENT_KINDS = {
    "submitted": "swing_entry_submitted",
    "entry_pending": "swing_entry_pending",
    "flattened_after_protective_stop_failure": "swing_entry_flattened",
    "critical_unprotected": "swing_entry_critical_unprotected",
    "rejected": "swing_entry_rejected",
    "skipped": "swing_entry_skipped",
}

INTRADAY_ACTIVITY_EVENT_KINDS = {
    "entry_submitted",
    "entry_rejected",
    "entry_skipped",
    "exit_submitted",
    "exit_rejected",
    "broker_exit_detected",
    "signal_skipped",
    "signal_held",
    "breakeven_activated",
    "breakeven_rejected",
    "critical_unprotected",
}


def swing_activity_record(outcome: SwingOrderResult, mode: str) -> ActivityRecord:
    return ActivityRecord(
        event_kind=SWING_EVENT_KINDS.get(outcome.status, f"swing_entry_{outcome.status}"),
        symbol=outcome.symbol,
        timestamp=datetime.now(),
        mode=mode,
        order_id=outcome.order_id,
        side="BUY",
        quantity=outcome.quantity or None,
        reason=outcome.reason,
    )


def render_swing_auto_trading(st, settings) -> None:
    # Swing signals come from completed daily candles, which only change once a trading day --
    # unlike the intraday pages, there's nothing to gain from a periodically-rerunning fragment
    # here, so this renders once per page visit/interaction (see should_scan below for the
    # once-a-day scan gate).
    def render_swing_content() -> None:
        st.title("Swing auto trading")
        st.caption("Scans completed daily candles for a fresh EMA 9 cross above EMA 200 and manages a CNC position with a Zerodha-side trailing SL-M order.")
        repository = get_dashboard_repository(st)
        user_id = dashboard_user_id(settings)
        selected_symbols = selected_watchlist_symbols(repository, user_id)
        selected_watchlists = repository.load_watchlists(user_id, selected_only=True)
        amount_limit = float(settings.swing_capital_limit)
        quantity_limit = int(settings.swing_quantity_limit)
        trailing_multiplier = float(settings.swing_trailing_atr_multiplier)
        max_open_swing_positions = int(settings.swing_max_open_positions)

        enabled = bool(st.session_state.get("swing_auto_enabled", False))
        kill_switch_active = bool(st.session_state.get("swing_kill_switch", False))
        live_confirmed = st.checkbox(
            "I understand this can place real CNC orders and modify Zerodha-side stop orders.",
            key="swing_live_confirmation",
            disabled=settings.trading_mode != TradingMode.LIVE,
        )
        label_column, strategy_column, toggle_column, scan_column = st.columns([0.14, 0.36, 0.25, 0.25])
        with label_column:
            st.markdown('<div style="padding-top:0.55rem; font-weight:600;">Strategy</div>', unsafe_allow_html=True)
        with strategy_column:
            selected_strategy_label = st.selectbox(
                "Strategy",
                list(SWING_STRATEGIES),
                key="swing_strategy",
                label_visibility="collapsed",
            )
        selected_strategy_name = SWING_STRATEGIES[selected_strategy_label]
        warmup_period = 200 if selected_strategy_name == "EMA 9/200 swing" else SwingTrendBreakoutStrategy().warmup_period
        with toggle_column:
            if enabled:
                if st.button(
                    "Trading Live End",
                    icon=":material/power_settings_new:",
                    type="secondary",
                    width="stretch",
                    help="Kill switch: immediately stop automatic scans and new swing orders. Existing broker-side protective stops remain active.",
                ):
                    st.session_state.swing_auto_enabled = False
                    st.session_state.swing_kill_switch = True
                    st.session_state.swing_scan_result = None
                    st.rerun()
            else:
                if st.button(
                    "Trading Live Start",
                    type="primary",
                    icon=":material/play_arrow:",
                    width="stretch",
                    disabled=settings.trading_mode != TradingMode.LIVE or not live_confirmed,
                    help="Start swing auto trading",
                ):
                    st.session_state.swing_auto_enabled = True
                    st.session_state.swing_kill_switch = False
                    st.session_state.swing_last_scan_day = None
                    st.rerun()
        with scan_column:
            manual_scan = st.button("Scan", icon=":material/search:", width="stretch", help="Run scan now")
        st.caption(
            f"₹{amount_limit:,.0f} cap · qty {quantity_limit} · ATR×{trailing_multiplier:.2f} · max {max_open_swing_positions} open "
            "(edit on Risk & settings page)  \n"
            f"Universe: {len(selected_symbols)} stocks from {len(selected_watchlists)} selected watchlists · Timeframe: daily · Warm-up: {warmup_period} completed candles"
        )
        if not selected_symbols:
            st.info("Select at least one watchlist on the Watchlists page before scanning.", icon=":material/list_alt:")
            return

        access_token = runtime_access_token(st, settings)
        if not broker_credentials_configured(settings) or not access_token:
            st.info("Authenticate with Kite before using the swing scanner.", icon=":material/key:")
            return
        try:
            client = connect_kite(settings, access_token)
        except (OSError, RuntimeError, ValueError) as error:
            st.error(f"Kite connection could not be established: {error}")
            return

        trader = st.session_state.get("swing_auto_trader")
        previous_strategy_name = st.session_state.get("swing_strategy_name")
        if previous_strategy_name != selected_strategy_name:
            st.session_state.swing_strategy_name = selected_strategy_name
            st.session_state.swing_scan_result = None
        if trader is None:
            trader = create_swing_auto_trader(client.client, settings.trading_mode, float(trailing_multiplier), selected_strategy_name, repository=repository)
            st.session_state.swing_auto_trader = trader
        elif getattr(trader, "strategy_name", "EMA 9/200 swing") != selected_strategy_name:
            if hasattr(trader, "set_strategy"):
                trader.set_strategy(selected_strategy_name)
            else:
                # Migrate an instance created before selectable swing strategies existed.
                legacy_positions = getattr(trader, "active_positions", {})
                legacy_signal_keys = getattr(trader, "submitted_signal_keys", set())
                legacy_open_symbols = getattr(trader, "broker_open_symbols", set())
                legacy_pending_entries = getattr(trader, "pending_entries", {})
                trader = create_swing_auto_trader(client.client, settings.trading_mode, float(trailing_multiplier), selected_strategy_name, repository=repository)
                trader.active_positions.update(legacy_positions)
                trader.submitted_signal_keys.update(legacy_signal_keys)
                trader.broker_open_symbols.update(legacy_open_symbols)
                trader.pending_entries.update(legacy_pending_entries)
                st.session_state.swing_auto_trader = trader
        trader.mode = settings.trading_mode
        trader.orders.mode = settings.trading_mode
        trader.trailing_atr_multiplier = float(trailing_multiplier)
        if hasattr(trader.strategy, "trailing_atr_multiplier"):
            trader.strategy.trailing_atr_multiplier = float(trailing_multiplier)
        scan_day = pd.Timestamp.now(tz="Asia/Kolkata").date().isoformat()
        trader.tick_sizes = {}
        try:
            trader.tick_sizes = load_swing_tick_sizes_cached(client.client, access_token)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
            st.error(f"Instrument tick-size metadata unavailable; live entries will be blocked until it can be verified: {error}")

        if settings.trading_mode != TradingMode.LIVE:
            st.info("PAPER mode is active. Swing auto execution is disabled; switch to LIVE mode only after validating the scanner.", icon=":material/science:")

        try:
            trader.sync_broker_positions()
        except Exception as error:
            st.warning(f"Broker position sync paused: {error}")

        # Trailing-stop math and broker-side SL-M modification are owned exclusively by the
        # standalone scripts/run_trailing_stop_agent.py process; this view only reads the stop
        # it last persisted, so it stays accurate even when the dashboard isn't open.
        swing_stops = {
            record.symbol: record.stop_loss
            for record in repository.load_positions()
            if record.position_type == "SWING"
        }
        if swing_stops:
            st.caption(
                "Current stop (maintained by the trailing stop agent): "
                + " · ".join(f"{symbol} ₹{stop:,.2f}" for symbol, stop in swing_stops.items())
            )

        open_swing_positions = len(trader.active_positions)
        position_limit_reached = open_swing_positions >= int(max_open_swing_positions)
        auto_enabled = bool(st.session_state.get("swing_auto_enabled", False)) and not bool(st.session_state.get("swing_kill_switch", False))
        # Today's daily candle doesn't change again once it's formed, so an automatic scan only
        # needs to run once per trading day -- after it completes and submits any qualifying
        # orders, auto mode stops scanning until the next day. The "Scan" button can still force
        # a rescan at any time regardless of whether today's auto scan already ran.
        already_scanned_today = st.session_state.get("swing_last_scan_day") == scan_day
        should_scan = (manual_scan or (auto_enabled and not already_scanned_today)) and not position_limit_reached
        if manual_scan and position_limit_reached:
            st.warning(
                f"Scan skipped: {open_swing_positions}/{int(max_open_swing_positions)} swing positions are already open. "
                "Close a position or raise the limit above to resume scanning."
            )
        if should_scan:
            # Candle fetches run in parallel (bounded by SCAN_MAX_WORKERS, out of respect for
            # Kite's historical-data rate limit) and a qualifying candidate is submitted the
            # moment it's found, instead of after every selected stock has been scanned --
            # sequentially scanning hundreds of stocks before placing the first order could
            # mean the breakout has already moved by the time the order goes in.
            total_symbols = len(selected_symbols)
            progress_bar = st.progress(0.0, text=f"Scanning {selected_strategy_label}: 0/{total_symbols} complete")
            live_log: list[str] = []
            log_box = st.empty()

            def log_line(message: str) -> None:
                live_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {message}")
                log_box.code("\n".join(live_log[-300:]), language=None)

            def fetch_symbol_candles(instrument_token: int) -> pd.DataFrame:
                return load_swing_daily_candles_cached(client.client, access_token, instrument_token, scan_day)

            submit_live = auto_enabled and settings.trading_mode == TradingMode.LIVE and live_confirmed
            candidates: list = []
            pending_candidates: list = []
            scan_errors: list[str] = []
            insufficient_history: list[str] = []
            no_signal: list[tuple[str, str]] = []
            submit_outcomes: list[SwingOrderResult] = []
            completed = 0
            scan_exception: Exception | None = None

            worker_count = min(SCAN_MAX_WORKERS, total_symbols) if total_symbols else 1
            try:
                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="swing-scan") as executor:
                    futures = {
                        executor.submit(fetch_symbol_candles, instrument_token): (symbol, instrument_token)
                        for symbol, instrument_token in selected_symbols.items()
                    }
                    for future in as_completed(futures):
                        symbol, instrument_token = futures[future]
                        completed += 1
                        progress_bar.progress(completed / total_symbols if total_symbols else 1.0, text=f"Scanning {selected_strategy_label}: {completed}/{total_symbols} complete")
                        try:
                            candles = future.result()
                        except Exception as error:
                            scan_errors.append(f"{symbol}: {error}")
                            continue
                        evaluation = trader.evaluate_symbol(symbol, instrument_token, lambda _token, _candles=candles: _candles)
                        if evaluation.error is not None:
                            scan_errors.append(f"{symbol}: {evaluation.error}")
                            continue
                        if evaluation.insufficient_history:
                            insufficient_history.append(symbol)
                            continue
                        for reason in evaluation.no_signal_reasons:
                            no_signal.append((symbol, reason))
                        if evaluation.pending_candidate is not None:
                            pending_candidates.append(evaluation.pending_candidate)
                        if evaluation.candidate is None:
                            continue
                        candidates.append(evaluation.candidate)
                        log_line(f"{symbol}: candidate found (cross {evaluation.candidate.signal.timestamp:%d %b %Y})")
                        if not submit_live:
                            continue
                        open_now = len(trader.active_positions)
                        if open_now >= int(max_open_swing_positions):
                            log_line(f"{symbol}: not submitted -- {open_now}/{int(max_open_swing_positions)} swing positions already open")
                            continue
                        try:
                            outcome = trader.submit_candidate(evaluation.candidate, float(amount_limit), int(quantity_limit))
                        except Exception as error:
                            outcome = SwingOrderResult(symbol, "rejected", f"submit_candidate raised an unexpected error: {error}")
                        submit_outcomes.append(outcome)
                        repository.save_activity(swing_activity_record(outcome, settings.trading_mode.value))
                        log_line(f"{symbol}: {outcome.status} -- {outcome.reason}")
            except Exception as error:
                scan_exception = error
            progress_bar.empty()
            st.session_state.swing_last_scan_at = datetime.now()
            if scan_exception is not None:
                st.session_state.swing_last_error = str(scan_exception)
                st.error(f"Swing scan could not be completed: {scan_exception}")
                result = None
            else:
                st.session_state.swing_last_error = None
                candidates.sort(key=lambda item: item.signal.timestamp, reverse=True)
                result = SwingScanResult(
                    tuple(candidates),
                    tuple(scan_errors),
                    total_symbols,
                    tuple(insufficient_history),
                    tuple(pending_candidates),
                    trader.strategy_name,
                    tuple(no_signal),
                )
                st.session_state.swing_scan_result = result
                st.session_state.swing_last_scan_day = scan_day
            for outcome in submit_outcomes:
                if outcome.status == "submitted":
                    st.success(f"{outcome.symbol}: {outcome.reason} · quantity {outcome.quantity}", icon=":material/check_circle:")
                elif outcome.status == "submitted_unprotected":
                    st.error(f"{outcome.symbol}: {outcome.reason}")
                elif outcome.status == "entry_pending":
                    st.info(f"{outcome.symbol}: {outcome.reason}")
                elif outcome.status == "flattened_after_protective_stop_failure":
                    st.error(f"{outcome.symbol}: {outcome.reason}")
                elif outcome.status == "critical_unprotected":
                    st.error(f"CRITICAL {outcome.symbol}: {outcome.reason}")
                elif outcome.status == "rejected":
                    st.error(f"{outcome.symbol}: {outcome.reason}")
            if result is not None:
                entered = sum(1 for outcome in submit_outcomes if outcome.status == "submitted")
                rejected = sum(
                    1
                    for outcome in submit_outcomes
                    if outcome.status in {"rejected", "critical_unprotected", "flattened_after_protective_stop_failure"}
                )
                cycle_summary = (
                    f"{datetime.now().strftime('%I:%M:%S %p')} — scanned {result.scanned} · "
                    f"{len(result.candidates)} candidates · {entered} entered · {rejected} rejected · {len(result.errors)} errors"
                )
                cycle_history = st.session_state.get("swing_cycle_log", [])
                st.session_state.swing_cycle_log = (cycle_history + [cycle_summary])[-12:]

        swing_last_error = st.session_state.get("swing_last_error")
        last_scan_at = st.session_state.get("swing_last_scan_at")
        last_scan_label = last_scan_at.strftime("%I:%M:%S %p") if last_scan_at else "No scan yet"
        already_scanned_today = st.session_state.get("swing_last_scan_day") == scan_day
        if position_limit_reached:
            swing_engine_state = "limit_reached"
        elif kill_switch_active or not enabled:
            swing_engine_state = "stopped"
        elif swing_last_error:
            swing_engine_state = "error"
        elif already_scanned_today:
            # Today's daily candle is fixed once formed, so the auto scan already ran and
            # submitted anything qualifying for today -- there is nothing left for it to do
            # until tomorrow's candle closes. Keep showing "armed / LIVE" here would read as
            # if a scan were still in progress or about to happen again today, which is the
            # same confusion intraday avoids by dropping out of "armed" the moment its cycle
            # halts on a filled position or a risk-limit hit instead of staying "LIVE" forever.
            swing_engine_state = "scanned_today"
        else:
            swing_engine_state = "running"
        state_copy = {
            "running": (
                "Swing scanner armed",
                f"{selected_strategy_label} · monitoring {len(selected_symbols)} stocks · "
                "scans once when this page is next opened today · "
                f"{open_swing_positions}/{int(max_open_swing_positions)} positions open · last scan {last_scan_label}",
            ),
            "scanned_today": (
                "Swing scan complete for today",
                f"{selected_strategy_label} · today's daily scan already ran at {last_scan_label} · "
                f"{open_swing_positions}/{int(max_open_swing_positions)} positions open · "
                "next scan when tomorrow's candle closes (use Scan to force a rescan now)",
            ),
            "stopped": (
                "Swing scanner stopped",
                f"{selected_strategy_label} · {open_swing_positions}/{int(max_open_swing_positions)} positions open · last scan {last_scan_label}",
            ),
            "error": ("Swing scanner needs attention", escape((swing_last_error or "The latest scan cycle reported an error")[:240])),
            "limit_reached": (
                "Swing scanner paused — position limit reached",
                f"{open_swing_positions}/{int(max_open_swing_positions)} swing positions open · close a position to resume scanning",
            ),
        }[swing_engine_state]
        swing_badge = {
            "running": "LIVE",
            "scanned_today": "DONE FOR TODAY",
            "stopped": "STOPPED",
            "limit_reached": "LIMIT REACHED",
            "error": "NEEDS ATTENTION",
        }[swing_engine_state]
        render_scan_activity_banner(
            st,
            "stalled" if swing_engine_state in {"limit_reached", "stopped", "scanned_today"} else swing_engine_state,
            *state_copy,
            badge=swing_badge,
        )
        swing_cycle_log = st.session_state.get("swing_cycle_log", [])
        if swing_cycle_log:
            with st.expander("Recent scan cycles", expanded=False):
                st.caption("  \n".join(reversed(swing_cycle_log[-5:])))
        swing_activity = load_activity()[2]
        swing_activity = swing_activity[swing_activity["event_kind"].astype(str).str.startswith("swing_")]
        if not swing_activity.empty:
            with st.expander(f"Recent swing activity ({len(swing_activity)})", expanded=False):
                st.dataframe(
                    swing_activity[["timestamp", "symbol", "event_kind", "reason"]].head(20),
                    width="stretch",
                    hide_index=True,
                )

        result = st.session_state.get("swing_scan_result")
        result_strategy_name = getattr(result, "strategy_name", "EMA 9/200 swing") if result is not None else None
        if result is not None and result_strategy_name == selected_strategy_name:
            st.subheader(selected_strategy_label)
            if result.candidates:
                candidate_rows = []
                stale_candidate_count = 0
                for candidate in result.candidates:
                    signal = candidate.signal
                    if signal is None:
                        continue
                    candidate_quantity = min(int(quantity_limit), int(amount_limit // signal.price))
                    if selected_strategy_name == "SWING_TREND_BREAKOUT":
                        evaluation = candidate.evaluation
                        if not all(
                            hasattr(evaluation, attribute)
                            for attribute in (
                                "classification",
                                "score",
                                "breakout_level",
                                "swing_low",
                                "volume_ratio",
                                "rsi14",
                                "adx14",
                            )
                        ):
                            stale_candidate_count += 1
                            continue
                        candidate_rows.append(
                            {
                                "Stock": candidate.symbol,
                                "TradingView": tradingview_chart_url(candidate.symbol),
                                "State": "CONFIRMED_BUY",
                                "Classification": evaluation.classification,
                                "Score": evaluation.score,
                                "Close": signal.price,
                                "Breakout high": evaluation.breakout_level,
                                "Swing low": evaluation.swing_low,
                                "Volume ratio": evaluation.volume_ratio,
                                "RSI14": evaluation.rsi14,
                                "ADX14": evaluation.adx14,
                                "Initial stop": signal.stop_loss,
                                "Target 1 (2R)": signal.target_1,
                                "Quantity": candidate_quantity,
                                "Cross candle": signal.timestamp,
                            }
                        )
                        continue
                    candidate_rows.append(
                        {
                            "Stock": candidate.symbol,
                            "TradingView": tradingview_chart_url(candidate.symbol),
                            "Close": signal.price,
                            "EMA9": candidate.evaluation.ema9,
                            "EMA200": candidate.evaluation.ema200,
                            "ATR14": candidate.evaluation.atr,
                            "Initial stop": signal.stop_loss,
                            "Quantity": candidate_quantity,
                            "Capital": candidate_quantity * signal.price,
                            "Cross candle": signal.timestamp,
                        }
                    )
                if stale_candidate_count:
                    st.session_state.swing_scan_result = None
                    st.warning("A stale scan result from the previous swing strategy was discarded. Run the scan again.")
                st.dataframe(
                    pd.DataFrame(candidate_rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                        "Close": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
                        "ATR14": st.column_config.NumberColumn(format="₹%.2f"),
                        "Initial stop": st.column_config.NumberColumn(format="₹%.2f"),
                        "Capital": st.column_config.NumberColumn(format="₹%.2f"),
                        "Cross candle": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
                    },
                )
            else:
                st.caption("No executable candidate was found in the selected watchlists.")
            pending_candidates = getattr(result, "pending_candidates", ())
            if pending_candidates and selected_strategy_name == "SWING_TREND_BREAKOUT":
                valid_pending_candidates = [
                    candidate
                    for candidate in pending_candidates
                    if all(
                        hasattr(candidate.evaluation, attribute)
                        for attribute in (
                            "classification",
                            "score",
                            "breakout_level",
                            "swing_low",
                            "volume_ratio",
                            "rsi14",
                            "adx14",
                        )
                    )
                ]
                if len(valid_pending_candidates) != len(pending_candidates):
                    st.session_state.swing_scan_result = None
                    st.warning("A stale pending breakout result was discarded. Run the scan again.")
                pending_candidates = valid_pending_candidates
            if pending_candidates and selected_strategy_name == "SWING_TREND_BREAKOUT":
                st.subheader("Breakout candidates awaiting next-session confirmation")
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "Stock": candidate.symbol,
                                "TradingView": tradingview_chart_url(candidate.symbol),
                                "State": "SWING_CANDIDATE",
                                "Classification": candidate.evaluation.classification,
                                "Score": candidate.evaluation.score,
                                "Breakout high": candidate.evaluation.breakout_level,
                                "Swing low": candidate.evaluation.swing_low,
                                "Volume ratio": candidate.evaluation.volume_ratio,
                                "RSI14": candidate.evaluation.rsi14,
                                "ADX14": candidate.evaluation.adx14,
                                "Candidate candle": candidate.evaluation.timestamp,
                            }
                            for candidate in pending_candidates
                        ]
                    ),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                        "Breakout high": st.column_config.NumberColumn(format="₹%.2f"),
                        "Swing low": st.column_config.NumberColumn(format="₹%.2f"),
                        "Volume ratio": st.column_config.NumberColumn(format="%.2fx"),
                        "RSI14": st.column_config.NumberColumn(format="%.2f"),
                        "ADX14": st.column_config.NumberColumn(format="%.2f"),
                        "Candidate candle": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
                    },
                )
            if result.errors:
                st.warning(" · ".join(result.errors[:5]))

        if trader.active_positions:
            st.subheader("Open swing positions")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Stock": position.symbol,
                            "Quantity": position.quantity,
                            "Entry": position.entry_price,
                            "Trailing stop": position.stop_loss,
                            "Target 1": getattr(position, "target_1", None),
                            "Partial booked": getattr(position, "partial_profit_booked", False),
                            "Zerodha entry order": position.order_id,
                            "Zerodha stop order": position.protective_order_id,
                            "Entry candle": position.entry_timestamp,
                        }
                        for position in trader.active_positions.values()
                    ]
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                    "Trailing stop": st.column_config.NumberColumn(format="₹%.2f"),
                    "Target 1": st.column_config.NumberColumn(format="₹%.2f"),
                    "Entry candle": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
                },
            )
            st.caption("Stopping the scanner does not cancel these Zerodha-side protective orders. Manage or close swing positions from Zerodha; daily stop ratchets run while this page session is active.")

    render_swing_content()


def render_backtesting(st, settings) -> None:
    st.title("Backtesting")
    st.caption("Replay Kite historical candles against the same research strategies used by Scanner & signals, Intratrading, and Swing auto trading.")
    st.warning("Backtest results are historical simulations, not guarantees of future performance. This page never submits orders or changes live positions.", icon=":material/science:")

    access_token = runtime_access_token(st, settings)
    if not broker_credentials_configured(settings) or not access_token:
        st.info("Authenticate with Kite before running a backtest.", icon=":material/key:")
        return

    repository = get_dashboard_repository(st)
    selected_symbols = selected_watchlist_symbols(repository, dashboard_user_id(settings))
    if not selected_symbols:
        st.info("Select at least one watchlist on the Watchlists page before running a backtest.", icon=":material/list_alt:")
        return

    scope = st.radio("Run scope", ["One symbol", "Selected watchlists"], horizontal=True, key="backtest_scope")
    if scope == "One symbol":
        symbol = st.selectbox("Symbol", list(selected_symbols), key="backtest_symbol")
        run_symbols = {symbol: selected_symbols[symbol]}
    else:
        run_symbols = selected_symbols
        st.caption(f"{len(run_symbols)} unique instruments from the selected watchlists")

    strategy_label = st.selectbox("Strategy", BACKTEST_STRATEGY_LABELS, key="backtest_strategy")
    daily_default = strategy_label in {"EMA 9/200 swing", BACKTEST_SWING_TREND_LABEL}
    timeframe = st.selectbox(
        "Timeframe",
        ["Daily", "5-minute"],
        index=0 if daily_default else 1,
        key="backtest_timeframe",
    )
    interval = "day" if timeframe == "Daily" else "5minute"

    date_column, capital_column, sizing_column = st.columns(3)
    with date_column:
        default_start = date.today() - timedelta(days=90 if interval == "5minute" else 730)
        selected_dates = st.date_input(
            "Historical date range",
            value=(default_start, date.today()),
            key="backtest_dates",
        )
    with capital_column:
        initial_capital = st.number_input("Initial capital", min_value=1.0, value=100_000.0, step=10_000.0, key="backtest_initial_capital")
    with sizing_column:
        sizing_mode = st.selectbox("Sizing", ["Fixed quantity", "Capital per position"], key="backtest_sizing_mode")
    quantity_column, position_column, fee_column, slippage_column = st.columns(4)
    with quantity_column:
        fixed_quantity = st.number_input("Quantity", min_value=1, value=1, step=1, key="backtest_quantity")
    with position_column:
        capital_per_position = st.number_input("Capital per position", min_value=1.0, value=10_000.0, step=1_000.0, key="backtest_capital_position")
    with fee_column:
        fee_rate = st.number_input("Fee rate", min_value=0.0, value=0.0003, step=0.0001, format="%.4f", key="backtest_fee_rate")
    with slippage_column:
        slippage_rate = st.number_input("Slippage rate", min_value=0.0, value=0.0005, step=0.0001, format="%.4f", key="backtest_slippage_rate")

    if not isinstance(selected_dates, (tuple, list)) or len(selected_dates) != 2:
        st.info("Select both a start and end date.")
        return
    start_date, end_date = selected_dates
    if start_date > end_date:
        st.error("The historical start date must be on or before the end date.")
        return

    try:
        strategy = build_backtest_strategy(strategy_label, interval)
    except ValueError as error:
        st.error(str(error))
        return

    st.caption(f"Kite interval: {interval} · warm-up: {getattr(strategy, 'warmup_period', 'stateful')} completed candles · one position per symbol")
    if strategy_label == BACKTEST_PROGRESSIVE_LABEL:
        st.info("Progressive replay enters only on STRONG alignment. LIGHT events are lifecycle context; EMA9 <= EMA200 invalidates an open cycle.", icon=":material/timeline:")

    if st.button("Run backtest", type="primary", icon=":material/play_arrow:", width="stretch"):
        try:
            client = connect_kite(settings, access_token)
            market_data = MarketData(client.client)
            loader = KiteHistoricalDataLoader(market_data)
            st.session_state.backtest_loader = loader
            start_timestamp = datetime.combine(start_date, datetime_time.min)
            end_timestamp = datetime.combine(end_date, datetime_time.max)
            progress = st.progress(0.0, text=f"Fetching 0/{len(run_symbols)} symbols")
            all_trades = []
            errors: list[str] = []
            symbol_metrics: dict[str, dict] = {}
            for completed, (run_symbol, instrument_token) in enumerate(run_symbols.items(), start=1):
                try:
                    candles = loader.load(int(instrument_token), start_timestamp, end_timestamp, interval)
                    if candles.empty:
                        raise ValueError("Kite returned no historical candles for the selected range")
                    if sizing_mode == "Fixed quantity":
                        run_quantity = int(fixed_quantity)
                    else:
                        run_quantity = max(1, int(float(capital_per_position) // float(candles["close"].iloc[0])))
                    symbol_trades = run_strategy_backtest(
                        run_symbol,
                        candles,
                        strategy,
                        quantity=run_quantity,
                        fee_rate=float(fee_rate),
                        slippage_rate=float(slippage_rate),
                    )
                    all_trades.extend(symbol_trades)
                    symbol_metrics[run_symbol] = calculate_metrics(symbol_trades)
                except Exception as error:
                    errors.append(f"{run_symbol}: {error}")
                progress.progress(completed / len(run_symbols), text=f"Processed {completed}/{len(run_symbols)} symbols")
            progress.empty()
            all_trades.sort(key=lambda trade: (pd.Timestamp(trade.exit_time), trade.symbol))
            st.session_state.backtest_result = {
                "trades": all_trades,
                "metrics": calculate_metrics(all_trades),
                "symbol_metrics": symbol_metrics,
                "errors": errors,
                "parameters": {
                    "strategy": strategy_label,
                    "interval": interval,
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                    "initial_capital": float(initial_capital),
                    "fee_rate": float(fee_rate),
                    "slippage_rate": float(slippage_rate),
                },
            }
        except (OSError, RuntimeError, ValueError) as error:
            st.error(f"Backtest could not start: {error}")

    result = st.session_state.get("backtest_result")
    if not result:
        st.caption("Run a Kite-backed historical replay to see results here.")
        return
    metrics = result["metrics"]
    metric_columns = st.columns(5)
    metric_columns[0].metric("Trades", int(metrics["trades"]))
    metric_columns[1].metric("Net P&L", f"₹{metrics['net_pnl']:,.2f}")
    metric_columns[2].metric("Win rate", f"{metrics['win_rate']:.1%}")
    metric_columns[3].metric("Profit factor", "∞" if metrics["profit_factor"] == float("inf") else f"{metrics['profit_factor']:.2f}")
    metric_columns[4].metric("Max drawdown", f"₹{metrics['max_drawdown']:,.2f}")
    st.caption(" · ".join(f"{key}: {value}" for key, value in result["parameters"].items()))

    if result["errors"]:
        st.warning("Some symbols could not be backtested: " + " · ".join(result["errors"][:10]))
    if result["symbol_metrics"]:
        symbol_table = pd.DataFrame.from_dict(result["symbol_metrics"], orient="index").reset_index(names="Symbol")
        st.subheader("Per-symbol results")
        st.dataframe(symbol_table, width="stretch", hide_index=True)

    trades = result["trades"]
    if not trades:
        st.info("No trades were generated for the selected strategy and historical range.")
        return
    trade_table = pd.DataFrame(
        [
            {
                "Symbol": trade.symbol,
                "Entry time": trade.entry_time,
                "Exit time": trade.exit_time,
                "Side": trade.side.value,
                "Quantity": trade.quantity,
                "Entry": trade.entry_price,
                "Exit": trade.exit_price,
                "P&L": trade.pnl,
                "Exit reason": trade.exit_reason,
                "Target 1 hit": trade.target_1_hit,
            }
            for trade in trades
        ]
    )
    equity = trade_table["P&L"].cumsum() + float(result["parameters"]["initial_capital"])
    st.subheader("Equity curve")
    st.line_chart(pd.DataFrame({"Equity": equity.to_numpy()}))
    st.subheader("Trades")
    st.dataframe(trade_table, width="stretch", hide_index=True)
    st.download_button(
        "Download trades",
        data=trade_table.to_csv(index=False).encode("utf-8"),
        file_name="kite-backtest-trades.csv",
        mime="text/csv",
        icon=":material/download:",
    )


def render_historical_day_scan_section(st, settings) -> None:
    st.caption("Choose a watchlist, strategy, and completed date to evaluate every stock using only daily candles available through that date.")
    repository = get_dashboard_repository(st)
    user_id = dashboard_user_id(settings)
    watchlists = repository.load_watchlists(user_id)
    if not watchlists:
        st.info("Create a watchlist on the Watchlists page before running a historical scan.", icon=":material/list_alt:")
        return

    watchlist_names = [watchlist.name for watchlist in watchlists]
    selected_watchlist_name = st.selectbox("Watchlist", watchlist_names, key="historical_scan_watchlist")
    selected_watchlist = next(item for item in watchlists if item.name == selected_watchlist_name)
    strategy_label = st.selectbox("Daily strategy", list(DAILY_SCAN_STRATEGIES), key="historical_scan_strategy")
    selected_date = st.date_input(
        "Completed trading date",
        value=pd.Timestamp.now(tz="Asia/Kolkata").date() - timedelta(days=1),
        max_value=pd.Timestamp.now(tz="Asia/Kolkata").date() - timedelta(days=1),
        key="historical_scan_date",
    )
    st.caption(f"{len(selected_watchlist.symbols)} stocks · {strategy_label} · {selected_date.isoformat()} · 600 calendar days of daily history loaded per stock")
    if not selected_watchlist.symbols:
        st.info("The selected watchlist has no stocks.", icon=":material/inventory_2:")
        return

    access_token = runtime_access_token(st, settings)
    if not broker_credentials_configured(settings) or not access_token:
        st.info("Authenticate with Kite before loading historical daily candles.", icon=":material/key:")
        return
    context = (selected_watchlist_name, strategy_label, selected_date.isoformat())
    results_placeholder = st.empty()
    scan_requested = st.button("Scan selected date", type="primary", icon=":material/search:", width="stretch")
    if scan_requested:
        results_placeholder.empty()
        try:
            client = connect_kite(settings, access_token)
            strategy = DAILY_SCAN_STRATEGIES[strategy_label]()
            frames: dict[int, pd.DataFrame] = {}
            frame_errors: dict[int, Exception] = {}

            def load_symbol(token: int) -> tuple[int, pd.DataFrame | None, Exception | None]:
                try:
                    return token, load_historical_daily_candles_from_client(client.client, token, selected_date), None
                except Exception as error:
                    return token, None, error

            with st.spinner("Loading daily history and evaluating every stock..."):
                worker_count = min(DYNAMIC_MAX_WORKERS, len(selected_watchlist.symbols))
                total_symbols = len(selected_watchlist.symbols)
                token_symbols = {int(token): symbol for symbol, token in selected_watchlist.symbols.items()}
                progress_bar = st.progress(
                    0.0,
                    text=f"Loading daily history: 0/{total_symbols} complete · {total_symbols} remaining",
                )
                loading_list = st.empty()
                stock_states = {symbol: "Queued" for symbol in selected_watchlist.symbols}
                completed_symbols: list[str] = []

                def update_loading_list(current_symbol: str | None = None) -> None:
                    pending = [symbol for symbol, state in stock_states.items() if state in {"Queued", "Loading"}]
                    recent = completed_symbols[-20:]
                    lines = []
                    if current_symbol:
                        lines.append(f"Latest completed: {current_symbol}")
                    if pending:
                        lines.append(f"Pending ({len(pending)}): {', '.join(pending[:20])}")
                    if recent:
                        lines.append("Completed recently: " + ", ".join(recent))
                    loading_list.info("\n\n".join(lines) if lines else "All stocks loaded.", icon=":material/downloading:")

                with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="historical-day-scan") as executor:
                    futures = [executor.submit(load_symbol, int(token)) for token in selected_watchlist.symbols.values()]
                    update_loading_list()
                    completed_count = 0
                    for future in as_completed(futures):
                        token, frame, error = future.result()
                        symbol = token_symbols[token]
                        completed_count += 1
                        if frame is not None:
                            frames[token] = frame
                            stock_states[symbol] = "Loaded"
                            completed_symbols.append(symbol)
                        if error is not None:
                            frame_errors[token] = error
                            stock_states[symbol] = "Error"
                            completed_symbols.append(f"{symbol} (error)")
                        progress_bar.progress(
                            completed_count / total_symbols if total_symbols else 1.0,
                            text=f"Loading {symbol}: {completed_count}/{total_symbols} complete · {total_symbols - completed_count} remaining",
                        )
                        update_loading_list()
                progress_bar.progress(1.0, text=f"Daily history loaded: {total_symbols}/{total_symbols} complete · 0 remaining")
                loading_list.success("All stocks loaded. Evaluating strategy conditions...", icon=":material/check_circle:")

                def candle_loader(token: int) -> pd.DataFrame:
                    if token in frame_errors:
                        raise frame_errors[token]
                    return frames.get(token, pd.DataFrame())

                result = scan_historical_watchlist(selected_date, selected_watchlist.symbols, strategy, candle_loader)
            loading_errors = tuple(
                f"{symbol}: {frame_errors[token]}"
                for symbol, token in selected_watchlist.symbols.items()
                if token in frame_errors
            )
            result = HistoricalScanResult(result.matches, loading_errors + result.errors, result.scanned)
            st.session_state.historical_day_scan_result = result
            st.session_state.historical_day_scan_context = context
        except (OSError, RuntimeError, ValueError) as error:
            st.error(f"Historical scan could not be completed: {error}")
            return

    with results_placeholder.container():
        result = st.session_state.get("historical_day_scan_result")
        if st.session_state.get("historical_day_scan_context") != context or result is None:
            st.info("Run the scan to evaluate every stock in the selected watchlist.", icon=":material/play_arrow:")
            return

        st.metric("Matching stocks", len(result.matches), f"of {result.scanned} scanned")
        if result.matches:
            rows = []
            for match in result.matches:
                metadata = match["metadata"]
                rows.append(
                    {
                        "Stock": match["symbol"],
                        "TradingView": tradingview_chart_url(match["symbol"]),
                        "Signal": match["side"],
                        "Close": match["price"],
                        "EMA9": match["ema9"],
                        "EMA20": match["ema20"],
                        "EMA200": match["ema200"],
                        "ATR14": match["atr"],
                        "Stop loss": match["stop_loss"],
                        "Previous day high": metadata.get("previous_day_high"),
                        "Trigger candle": match["timestamp"],
                        "Reason": match["reason"],
                    }
                )
            table = pd.DataFrame(rows)
            st.dataframe(
                table,
                width="stretch",
                height=600,
                hide_index=True,
                column_config={
                    "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                    "Close": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
                    "ATR14": st.column_config.NumberColumn(format="₹%.2f"),
                    "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
                    "Previous day high": st.column_config.NumberColumn(format="₹%.2f"),
                    "Trigger candle": st.column_config.DatetimeColumn(format="DD MMM YYYY"),
                },
            )
            st.download_button(
                "Download scan results",
                data=table.to_csv(index=False).encode("utf-8"),
                file_name=f"historical-day-scan-{selected_watchlist_name}-{selected_date.isoformat()}.csv",
                mime="text/csv",
                icon=":material/download:",
            )
        else:
            st.info("No stocks met the selected strategy condition on this date.", icon=":material/search_off:")
        if result.errors:
            st.warning("Some stocks could not be evaluated: " + " · ".join(result.errors[:10]))


def render_scan_activity_banner(st, state: str, title: str, detail: str, badge: str | None = None) -> None:
    animation_markup = '<div class="scan-activity-beam"><span></span><span></span><span></span><span></span></div>' if state == "running" else ""
    badge_text = badge if badge is not None else state.upper()
    html = (
        f'<div class="scan-activity {state}" role="status" aria-live="polite">'
        f'<div class="scan-activity-mark"><span class="scan-activity-dot"></span></div>'
        f'<div class="scan-activity-copy"><strong>{title}</strong><span>{detail}</span>{animation_markup}</div>'
        f'<div class="scan-activity-live">{badge_text}</div>'
        "</div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def render_signal_feed(st, settings) -> None:
    @st.fragment(run_every=5)
    def render_live_signal_feed() -> None:
        render_signal_feed_content(st, settings)

    render_live_signal_feed()


def render_signal_feed_content(st, settings) -> None:
    st.markdown('<div class="eyebrow">Backend signal feed</div>', unsafe_allow_html=True)
    st.title("Scanner & signals")
    st.caption("Start the signal engine here. It continuously scans your selected watchlists and saves BUY/SELL signals while this dashboard is running.")
    repository = get_dashboard_repository(st)
    user_id = dashboard_user_id(settings)
    selected = selected_watchlist_symbols(repository, user_id)
    selected_watchlists = repository.load_watchlists(user_id, selected_only=True)
    selected_live_strategy = st.segmented_control(
        "Live strategy",
        [EMA_PROGRESSIVE_LIVE_LABEL, PRE_SPIKE_LIVE_LABEL, PREVIOUS_DAY_HIGH_LABEL, EMA_200_CLOSE_LIVE_LABEL, HIGH_CONVICTION_LIVE_LABEL],
        default=EMA_PROGRESSIVE_LIVE_LABEL,
        key="live_signal_strategy_selection",
    ) or EMA_PROGRESSIVE_LIVE_LABEL
    pre_spike_config = render_pre_spike_config(st, "live_pre_spike") if selected_live_strategy == PRE_SPIKE_LIVE_LABEL else None
    active_live_strategy = st.session_state.get("dashboard_signal_strategy")
    active_pre_spike_config = st.session_state.get("dashboard_pre_spike_config")
    running_engine = st.session_state.get("dashboard_signal_engine")
    if running_engine is not None and not running_engine.running and getattr(running_engine, "stop_requested", False):
        stopped_database = st.session_state.pop("dashboard_signal_database", None)
        if stopped_database is not None:
            stopped_database.close()
        st.session_state.pop("dashboard_signal_engine", None)
        running_engine = None
    configuration_changed = (
        active_live_strategy != selected_live_strategy
        or (selected_live_strategy == PRE_SPIKE_LIVE_LABEL and active_pre_spike_config != pre_spike_config)
    )
    if configuration_changed and running_engine is not None and running_engine.running:
        stop_dashboard_signal_engine(st)
    st.session_state.dashboard_signal_strategy = selected_live_strategy
    st.session_state.dashboard_pre_spike_config = pre_spike_config
    engine = st.session_state.get("dashboard_signal_engine")
    is_running = engine is not None and engine.running
    stop_requested = engine is not None and getattr(engine, "stop_requested", False)
    status = repository.load_signal_engine_status(user_id)
    last_scan = status.last_run_at.strftime("%I:%M:%S %p") if status else "No scan yet"
    heartbeat_age = None
    heartbeat_at = getattr(status, "heartbeat_at", None) if status else None
    if status and heartbeat_at is None:
        heartbeat_at = status.last_run_at
    if heartbeat_at:
        current_time = datetime.now(heartbeat_at.tzinfo) if heartbeat_at.tzinfo else datetime.now()
        heartbeat_age = max(0.0, (current_time - heartbeat_at).total_seconds())
    heartbeat_timeout = max(120, settings.signal_poll_seconds * 3)
    if stop_requested and is_running:
        engine_state = "stopping"
        status_label = "Stopping"
        status_color = "#b7791f"
    elif status and status.last_error:
        engine_state = "error"
        status_label = "Error"
        status_color = "#b42318"
    elif is_running and (heartbeat_age is None or heartbeat_age <= heartbeat_timeout):
        engine_state = "running"
        status_label = "Running"
        status_color = "#20844b"
    elif is_running:
        engine_state = "stalled"
        status_label = "Stalled"
        status_color = "#b7791f"
    else:
        engine_state = "stopped"
        status_label = "Stopped"
        status_color = "#b7791f"
    if engine_state in {"running", "stalled", "stopping", "error"}:
        state_copy = {
            "running": ("Scanning in progress", f"Monitoring {len(selected)} selected stocks for the next completed-candle signal"),
            "stalled": ("Scan heartbeat is stale", "The worker is still present but has not reported a recent cycle"),
            "stopping": ("Stopping scan engine", "The current stock evaluation will finish, then the worker will exit"),
            "error": ("Scan engine needs attention", escape(status.last_error[:240] if status else "The latest scan cycle reported an error")),
        }[engine_state]
        render_scan_activity_banner(st, engine_state, *state_copy)
    st.markdown(
        f"<div class='engine-panel'><strong style='color:{status_color}'>● Signal engine {status_label.lower()}</strong><br><span style='color:#53645a'>Strategy {selected_live_strategy} · Monitoring {len(selected)} unique stocks from {len(selected_watchlists)} selected watchlist{'s' if len(selected_watchlists) != 1 else ''} · Timeframe {settings.signal_timeframe} · Last scan {last_scan}</span></div>",
        unsafe_allow_html=True,
    )
    start_column, stop_column, refresh_column = st.columns([2, 1, 1])
    with start_column:
        start_disabled = not selected or not broker_credentials_configured(settings) or not runtime_access_token(st, settings)
        if st.button("Start scan engine", type="primary", icon=":material/play_arrow:", width="stretch", disabled=is_running or start_disabled):
            try:
                start_dashboard_signal_engine(
                    st,
                    settings,
                    runtime_access_token(st, settings),
                    user_id,
                    selected_live_strategy,
                    pre_spike_config,
                )
                st.success("Scan engine started. It is now monitoring the selected watchlists.")
                st.rerun()
            except (OSError, RuntimeError, ValueError) as error:
                st.error(f"Scan engine could not start: {error}")
    with stop_column:
        if st.button("Stop engine", icon=":material/stop:", width="stretch", disabled=not is_running or stop_requested):
            stop_dashboard_signal_engine(st)
            st.rerun()
    with refresh_column:
        if st.button("Refresh", icon=":material/refresh:", width="stretch"):
            st.rerun()
    if not selected_watchlists:
        st.info("Select at least one watchlist on the Watchlists page before starting the scan engine.", icon=":material/info:")
    elif not broker_credentials_configured(settings) or not runtime_access_token(st, settings):
        st.info("Connect Kite before starting the scan engine.", icon=":material/key:")
    if status and status.last_error:
        st.warning(f"The last engine cycle reported: {status.last_error}")

    with st.expander("Historical day scan", icon=":material/history:"):
        render_historical_day_scan_section(st, settings)

    if selected_live_strategy == PRE_SPIKE_LIVE_LABEL:
        render_pre_spike_signal_table(st, repository, user_id)
        return
    if selected_live_strategy == PREVIOUS_DAY_HIGH_LABEL:
        render_previous_day_high_signal_table(st, repository, user_id)
        return
    if selected_live_strategy == EMA_200_CLOSE_LIVE_LABEL:
        render_ema200_close_signal_table(st, repository, user_id)
        return
    if selected_live_strategy == HIGH_CONVICTION_LIVE_LABEL:
        render_high_conviction_signal_table(st, repository, user_id)
        return

    progressive_response = repository.load_progressive_signal_response(user_id)
    st.markdown('<div class="eyebrow">EMA 9 to EMA 200 progressive</div>', unsafe_allow_html=True)
    st.caption("The lifecycle below is evaluated from completed candles for every stock in the passed watchlist.")
    bucket_a = pd.DataFrame(progressive_response["bucket_a"])
    bucket_b = pd.DataFrame(progressive_response["bucket_b"])
    if not bucket_a.empty:
        bucket_a["tradingview_url"] = bucket_a["symbol"].map(tradingview_chart_url)
    if not bucket_b.empty:
        bucket_b["tradingview_url"] = bucket_b["symbol"].map(tradingview_chart_url)
    bucket_a_column, bucket_b_column = st.columns(2, gap="large")
    with bucket_a_column:
        st.subheader("LIGHT SIGNALS")
        if bucket_a.empty:
            st.caption("No active or historical Bucket A cycles are available.")
        else:
            bucket_a_display = bucket_a.rename(
                columns={
                    "symbol": "Stock",
                    "tradingview_url": "TradingView",
                    "current_price": "Current price",
                    "ema9": "EMA9",
                    "ema20": "EMA20",
                    "ema50": "EMA50",
                    "ema100": "EMA100",
                    "ema200": "EMA200",
                    "crossover_time": "Crossover time",
                    "status": "Status",
                }
            )
            st.dataframe(
                bucket_a_display.reindex(
                    columns=["Stock", "TradingView", "Current price", "EMA9", "EMA20", "EMA50", "EMA100", "EMA200", "Crossover time", "Status"]
                ),
                width="stretch",
                height=520,
                hide_index=True,
                column_config={
                    "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                    "Current price": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA50": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA100": st.column_config.NumberColumn(format="₹%.2f"),
                    "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
                },
            )
    with bucket_b_column:
        st.subheader("STRONG SIGNALS")
        if bucket_b.empty:
            st.caption("No Bucket B cycles are available.")
        else:
            bucket_b_display = bucket_b.rename(
                columns={
                    "symbol": "Stock",
                    "tradingview_url": "TradingView",
                    "current_price": "Current price",
                    "ema9": "EMA9",
                    "ema20": "EMA20",
                    "ema50": "EMA50",
                    "ema100": "EMA100",
                    "ema200": "EMA200",
                    "initial_crossover_time": "Light signal time",
                    "strong_signal_time": "Strong signal time",
                    "status": "Status",
                }
            )
            with st.container(border=True):
                st.dataframe(
                    bucket_b_display.reindex(
                        columns=["Stock", "TradingView", "Current price", "EMA9", "EMA20", "EMA50", "EMA100", "EMA200", "Light signal time", "Strong signal time", "Status"]
                    ),
                    width="stretch",
                    height=520,
                    hide_index=True,
                    column_config={
                        "TradingView": st.column_config.LinkColumn("TradingView", display_text="Open chart"),
                        "Current price": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA9": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA50": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA100": st.column_config.NumberColumn(format="₹%.2f"),
                        "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
                    },
                )


def render_automatic_feed(st, settings) -> None:
    st.markdown('<div class="eyebrow">Signal execution</div>', unsafe_allow_html=True)
    st.title("Automatic trading")
    st.caption("The backend generates signals continuously. This page consumes persisted signals and applies the existing execution and risk gates.")
    repository = get_dashboard_repository(st)
    user_id = dashboard_user_id(settings)
    signals = repository.load_signals(user_id, 25)
    if not signals:
        st.markdown('<div class="empty">No persisted signals are available for automatic execution.</div>', unsafe_allow_html=True)
        return
    st.dataframe(load_persisted_signal_frame(repository, user_id, 25), width="stretch", hide_index=True)
    if settings.trading_mode == TradingMode.LIVE:
        st.warning("LIVE mode is active. Submitting a signal can place a real MARKET order.", icon=":material/warning:")
    else:
        st.info("PAPER mode is active. Orders stay inside the local paper execution boundary.", icon=":material/science:")
    if not st.button("Submit latest signals through risk gates", type="primary", icon=":material/play_arrow:"):
        return
    access_token = runtime_access_token(st, settings)
    token_to_symbol = {token: symbol for symbol, token in selected_watchlist_symbols(repository, user_id).items()}
    if not token_to_symbol:
        st.error("Select at least one watchlist before submitting signals.")
        return
    try:
        pipeline = get_dashboard_pipeline(st, settings, access_token, token_to_symbol, CrossoverStrategy())
        events = [
            pipeline.submit_strategy_entry(
                Signal(
                    symbol=signal.symbol,
                    action=SignalAction(signal.side),
                    timestamp=signal.signal_timestamp,
                    price=signal.price,
                    stop_loss=signal.stop_loss,
                    reason=signal.reason,
                    score=100,
                    entry_price=signal.price,
                )
            )
            for signal in signals
            if signal.stop_loss is not None
        ]
        record_dashboard_events(st, events)
        st.success(f"Processed {len(events)} persisted signal(s) through the execution gates.")
    except Exception as error:
        logger.exception("Manual submission of persisted signals failed")
        st.error(f"Signals could not be submitted: {error}")


def render_risk_settings(st, settings) -> None:
    st.markdown('<div class="eyebrow">Risk and execution policy</div>', unsafe_allow_html=True)
    st.title("Risk & settings")
    st.caption("Saved changes persist across dashboard restarts and apply to new entries after validation.")
    if st.session_state.pop("risk_settings_saved", False):
        st.success("Risk and execution settings saved and will be restored next time.", icon=":material/check_circle:")

    pipeline = st.session_state.get("dashboard_pipeline")
    if pipeline is not None and pipeline.managed_positions:
        st.warning("Open positions are being monitored with their existing settings. New settings apply after those positions are closed.", icon=":material/info:")

    with st.form("risk_settings_form"):
        st.subheader("Global")
        st.caption("Shared account-level settings that apply to both Intratrading and Swing auto trading.")
        mode_column, capital_column, deployment_column, min_move_column = st.columns(4)
        trading_mode = mode_column.selectbox(
            "Trading mode",
            list(TradingMode),
            index=list(TradingMode).index(settings.trading_mode),
            format_func=lambda mode: mode.value.upper(),
        )
        initial_capital = capital_column.number_input(
            "Initial capital",
            min_value=1.0,
            value=float(settings.initial_capital),
            step=1_000.0,
            format="%.2f",
        )
        max_capital_deployment = deployment_column.number_input(
            "Maximum capital deployment (%)",
            min_value=1.0,
            max_value=100.0,
            value=float(settings.max_capital_deployment * 100),
            step=1.0,
            format="%.1f",
        )
        min_stop_improvement_pct = min_move_column.number_input(
            "Minimum SL-M move to update (%)",
            min_value=0.0,
            max_value=5.0,
            value=float(settings.min_stop_improvement_pct),
            step=0.05,
            format="%.2f",
            help=(
                "The trailing-stop agent skips sending a broker-side SL-M update unless the new stop improves on "
                "the current one by at least this percentage of the last traded price. Zerodha caps modifications "
                "at 25 per order; this cuts down on paisa-level noise updates that burn through that cap for no "
                "real protection benefit. 0 disables the filter (every improvement is sent, as before)."
            ),
        )

        st.subheader("Intratrading")
        st.caption("Risk limits, trading window, and trailing stop used by the Intratrading (intraday) engine.")
        intraday_capital_column, positions_column, atr_column, trades_column = st.columns(4)
        intraday_capital_limit = intraday_capital_column.number_input(
            "Maximum capital per position",
            min_value=1.0,
            max_value=10_000_000.0,
            value=float(settings.intraday_capital_limit),
            step=1_000.0,
            format="%.2f",
            key="risk_settings_intraday_capital",
        )
        max_open_positions = positions_column.number_input(
            "Maximum open positions",
            min_value=1,
            max_value=100,
            value=int(settings.max_open_positions),
            step=1,
        )
        trailing_atr_multiplier = atr_column.number_input(
            "Trailing stop ATR multiplier",
            min_value=0.1,
            max_value=10.0,
            value=float(settings.trailing_atr_multiplier),
            step=0.1,
            format="%.1f",
        )
        max_trades_per_day = trades_column.number_input(
            "Maximum trades per day",
            min_value=1,
            max_value=1_000,
            value=int(settings.max_trades_per_day),
            step=1,
        )
        intraday_leverage_multiplier = st.number_input(
            "Fallback intraday leverage (x)",
            min_value=1.0,
            max_value=50.0,
            value=float(settings.intraday_leverage_multiplier),
            step=0.5,
            format="%.1f",
            help=(
                "LIVE entries first ask Zerodha's margin calculator for the real leverage it's "
                "granting the specific stock right now; this number is only used as a fallback "
                "in PAPER mode or if that live lookup fails. Leave at 1x unless you've confirmed "
                "the margin your account actually gets."
            ),
        )

        market_column, entry_start_column, entry_end_column, force_exit_column = st.columns(4)
        market_open = market_column.time_input("Market open", value=settings.market_open)
        entry_start = entry_start_column.time_input("Entry window start", value=settings.entry_start)
        entry_end = entry_end_column.time_input("Entry window end", value=settings.entry_end)
        force_exit = force_exit_column.time_input("Force exit", value=settings.force_exit)

        st.subheader("Swing auto trading")
        st.caption("Position sizing and safety limits used by the Swing auto trading page's scanner.")
        swing_capital_column, swing_quantity_column, swing_atr_column, swing_limit_column = st.columns(4)
        swing_capital_limit = swing_capital_column.number_input(
            "Maximum capital per position",
            min_value=1.0,
            max_value=10_000_000.0,
            value=float(settings.swing_capital_limit),
            step=1_000.0,
            format="%.2f",
        )
        swing_quantity_limit = swing_quantity_column.number_input(
            "Maximum quantity per position",
            min_value=1,
            max_value=1_000_000,
            value=int(settings.swing_quantity_limit),
            step=1,
        )
        swing_trailing_atr_multiplier = swing_atr_column.number_input(
            "Trailing stop ATR multiplier",
            min_value=0.5,
            max_value=10.0,
            value=float(settings.swing_trailing_atr_multiplier),
            step=0.25,
            key="risk_settings_swing_atr",
        )
        swing_max_open_positions = swing_limit_column.number_input(
            "Maximum open swing positions",
            min_value=1,
            max_value=1_000,
            value=int(settings.swing_max_open_positions),
            step=1,
            help="Once this many swing positions are open, the Swing auto trading page pauses scanning automatically until one closes.",
        )

        save_settings = st.form_submit_button("Save risk & settings", type="primary", width="stretch", icon=":material/save:")

    if not save_settings:
        return

    overrides = {
        "trading_mode": trading_mode,
        "initial_capital": float(initial_capital),
        "max_open_positions": int(max_open_positions),
        "max_trades_per_day": int(max_trades_per_day),
        "max_capital_deployment": float(max_capital_deployment) / 100,
        "market_open": market_open,
        "entry_start": entry_start,
        "entry_end": entry_end,
        "force_exit": force_exit,
        "trailing_atr_multiplier": float(trailing_atr_multiplier),
        "min_stop_improvement_pct": float(min_stop_improvement_pct),
        "swing_capital_limit": float(swing_capital_limit),
        "swing_quantity_limit": int(swing_quantity_limit),
        "swing_trailing_atr_multiplier": float(swing_trailing_atr_multiplier),
        "swing_max_open_positions": int(swing_max_open_positions),
        "intraday_capital_limit": float(intraday_capital_limit),
        "intraday_leverage_multiplier": float(intraday_leverage_multiplier),
    }
    try:
        updated_settings = apply_frontend_settings(settings, overrides)
    except (TypeError, ValueError) as error:
        st.error(f"Settings could not be saved: {error}")
        return

    save_dashboard_settings(
        get_dashboard_repository(st),
        settings.user_id,
        serialize_frontend_settings(settings_values(updated_settings)),
    )
    st.session_state.frontend_settings = settings_values(updated_settings)
    st.session_state.risk_settings_saved = True
    if pipeline is None or not pipeline.managed_positions:
        st.session_state.pop("dashboard_pipeline", None)
        st.session_state.pop("dashboard_kite_client", None)
    st.rerun()


def render_automatic_trading(st, settings) -> None:
    st.title("Intratrading")
    st.caption("Automatic entries use the selected strategy, score threshold, risk gates, and activity ledger as the manual signal page.")
    repository = get_dashboard_repository(st)
    strategy_options = load_dashboard_strategy_options(repository, include_pre_spike=True, include_previous_day_high=True)
    strategy_labels = list(strategy_options)
    automatic_enabled = bool(st.session_state.get("automatic_enabled", False))
    intraday_capital_limit = float(settings.intraday_capital_limit)
    intraday_max_open_positions = int(settings.max_open_positions)
    intraday_trailing_atr_multiplier = float(settings.trailing_atr_multiplier)
    live_confirmed = st.checkbox(
        "I understand this can place real MARKET orders and modify Zerodha-side stop orders.",
        key="automatic_live_confirmation",
        disabled=settings.trading_mode != TradingMode.LIVE,
    )

    label_column, strategy_column, toggle_column, scan_column = st.columns([0.14, 0.36, 0.25, 0.25])
    with label_column:
        st.markdown('<div style="padding-top:0.55rem; font-weight:600;">Strategy</div>', unsafe_allow_html=True)
    with strategy_column:
        selected_strategy = st.selectbox(
            "Strategy",
            strategy_labels,
            index=strategy_labels.index(HIGH_CONVICTION_LIVE_LABEL),
            key="automatic_strategy",
            label_visibility="collapsed",
        )
    with toggle_column:
        if automatic_enabled:
            if st.button(
                "Trading Live End",
                icon=":material/power_settings_new:",
                type="secondary",
                width="stretch",
                help="Stop automatic entries immediately.",
            ):
                st.session_state.automatic_enabled = False
                st.rerun()
        else:
            if st.button(
                "Trading Live Start",
                icon=":material/play_arrow:",
                type="primary",
                width="stretch",
                disabled=settings.trading_mode != TradingMode.LIVE or not live_confirmed,
                help="Start automatic entries",
            ):
                st.session_state.automatic_enabled = True
                st.rerun()
    with scan_column:
        manual_scan_clicked = st.button("Scan only", icon=":material/search:", width="stretch", help="Run a one-off scan without submitting entries")

    pre_spike_config = render_pre_spike_config(st, "automatic_pre_spike") if selected_strategy == PRE_SPIKE_LIVE_LABEL else None
    try:
        strategy = build_dashboard_strategy(repository, selected_strategy, pre_spike_config)
    except ValueError as error:
        st.error(f"Strategy could not be loaded: {error}")
        return
    uses_market_filters = isinstance(strategy, VwapEmaBreakoutStrategy)
    uses_market_confirmation = isinstance(strategy, HighConvictionLongStrategy)

    selected_symbols = selected_watchlist_symbols(repository, dashboard_user_id(settings))
    st.caption(
        f"₹{intraday_capital_limit:,.0f} cap · ATR×{intraday_trailing_atr_multiplier:.2f} · max {intraday_max_open_positions} open "
        "(edit on Risk & settings page)"
    )
    if not selected_symbols:
        st.info("Select at least one watchlist on the Watchlists page before scanning.", icon=":material/list_alt:")
        return
    if settings.trading_mode != TradingMode.LIVE:
        st.info("PAPER mode is active. Orders stay inside the local paper execution boundary.", icon=":material/science:")
    access_token = runtime_access_token(st, settings)
    if not broker_credentials_configured(settings) or not access_token:
        st.info("Authenticate with Kite to run Intratrading on your selected watchlists.", icon=":material/lock:")
        return
    try:
        client = connect_kite(settings, access_token)
        current_instruments = load_watchlist_instruments(settings, access_token)
        resolved_symbols, skipped_symbols = reconcile_selected_watchlist_symbols(selected_symbols, current_instruments)
        if skipped_symbols:
            st.warning(
                "Skipped unavailable or non-equity watchlist instruments: "
                + ", ".join(skipped_symbols)
                + ". Refresh the watchlist before enabling Intratrading."
            )
        if not resolved_symbols:
            st.info("No selected watchlist instruments are currently tradable on Kite.", icon=":material/block:")
            return
        token_to_symbol = {token: symbol for symbol, token in resolved_symbols.items()}
        selected_sector = "All sectors"
    except (OSError, RuntimeError, ValueError) as error:
        st.error(f"Watchlist stock universe could not be loaded: {error}")
        return

    universe_label = "Selected watchlists"
    automatic_scan_context = (
        selected_strategy,
        repr(getattr(strategy, "config", None)),
        universe_label,
        selected_sector,
        tuple(sorted(token_to_symbol.items())),
    )
    st.caption(f"Selected stocks: {len(token_to_symbol)} · Sector: {selected_sector} · Strategy: {selected_strategy}")
    if isinstance(strategy, PreSpikeMomentumStrategy):
        st.caption(
            f"Completed 5-minute candles · 60-day lookback · score >= {strategy.config.minimum_score} · "
            f"RVOL >= {strategy.config.minimum_rvol:.1f}x · 5-minute move >= {strategy.config.minimum_price_change_pct:.2f}%."
        )
    elif isinstance(strategy, PreviousDayHighBreakoutStrategy):
        st.caption("BUY when the latest completed 5-minute candle closes above the previous trading day's high.")
    elif isinstance(strategy, HighConvictionLongStrategy):
        st.caption("Completed 5-minute candle · score >= 85 · previous 20-candle close breakout · RVOL >= 1.5x · 1 ATR stop · targets +1% / +2%.")
    elif not uses_market_filters:
        st.caption(
            f"Candle close rule: Entry EMA {strategy.entry_ema}, Exit EMA {strategy.exit_ema}, "
            f"fresh crossover only, minimum gap {strategy.min_gap_percent:.1f}%, stop {strategy.stop_atr:.1f} x ATR{strategy.atr_period}."
        )

    def record_automatic_cycle(summary: str) -> None:
        cycle_history = st.session_state.get("automatic_cycle_log", [])
        st.session_state.automatic_cycle_log = (cycle_history + [summary])[-12:]

    def run_automatic_cycle(execute_entries: bool) -> None:
        """Scan the universe with candle fetches running in parallel (bounded by
        SCAN_MAX_WORKERS, out of respect for Kite's historical-data rate limit), and submit an
        order for each qualifying BUY the moment it's found -- not after all 1000+ stocks have
        been scanned. A sequential scan-then-submit-top-5 pipeline meant a signal found early
        could sit for minutes before an order went in, by which point the move may have already
        run. The trade-off: entries are no longer limited to the best 5 signals by score: every
        qualifying signal is submitted, in the order its fetch happens to complete, until
        position/capital/daily-trade limits naturally stop further entries. The buy/sell frames
        in the final result are still ranked to the top 5 for the on-screen summary table, but
        that ranking no longer gates what gets submitted.
        """
        token_by_symbol = {symbol: token for token, symbol in token_to_symbol.items()}
        run_started_at = datetime.now()
        status_caption = st.empty()
        st.session_state.automatic_last_run_at = run_started_at
        st.session_state.automatic_next_run_at = run_started_at + timedelta(seconds=300)
        st.session_state.automatic_last_error = None
        live_log: list[str] = []
        log_box = st.empty()

        def log_line(message: str) -> None:
            live_log.append(f"{datetime.now().strftime('%H:%M:%S')}  {message}")
            log_box.code("\n".join(live_log[-300:]), language=None)

        try:
            # Check the position cap first, before any regime lookup, progress bar, or scan
            # log gets created -- if the cap is already hit, no entry could ever be accepted
            # this cycle regardless of what the scan finds, so scanning 600+ stocks (and the
            # API calls that costs) is pure waste. Halting here also means only the top status
            # banner (driven by automatic_last_error) reports the halt, instead of the same
            # message repeating across the log box, a stuck 0%-complete progress bar, and a
            # separate error box. automatic_last_error isn't sticky -- it's reset at the top of
            # every cycle, so the very next scheduled run re-checks automatically and clears
            # itself the moment a position closes or the limit is raised, with no manual
            # restart needed.
            pipeline = get_dashboard_pipeline(st, settings, access_token, token_to_symbol, strategy) if execute_entries else None
            if pipeline is not None and pipeline.managed_positions:
                # The Intraday page no longer shows a live position table (removed on request),
                # but this pipeline's own in-memory managed_positions is still what the position
                # count and capital-deployment limits below are checked against. Without this
                # sync, a position closed at the broker (SL-M fill) keeps counting as "open"
                # here even though the standalone trailing-stop agent already reconciled it out
                # of the database -- silently blocking new entries with "maximum open positions
                # reached" even when real headroom exists. See the Live monitor page for the
                # authoritative, always-reconciled position list.
                try:
                    pipeline.sync_broker_positions()
                except Exception:
                    logger.exception("Silent broker position sync failed before the automatic scan; position/capital limits may use stale counts this cycle")
            if execute_entries and pipeline is not None:
                open_position_count = len(pipeline.managed_positions)
                if open_position_count >= settings.max_open_positions:
                    message = (
                        f"maximum open positions already reached ({open_position_count}/{settings.max_open_positions}); "
                        "scan halted for this cycle -- close a position or raise the limit on the Risk & settings page"
                    )
                    st.session_state.automatic_last_error = message
                    record_automatic_cycle(f"{datetime.now().strftime('%I:%M:%S %p')} — scan halted: {message}")
                    return

            if uses_market_filters or uses_market_confirmation:
                status_caption.caption("Loading NIFTY 50 market regime...")
                regime = load_live_nifty_regime(client.client)
            else:
                regime = None

            def load_symbol_candles(symbol: str) -> pd.DataFrame:
                lookback_days = 60 if isinstance(strategy, PreSpikeMomentumStrategy) else 5
                return load_live_candles_from_client(client.client, token_by_symbol[symbol], "5minute", lookback_days)

            def load_confirmation(symbol: str) -> TimeframeConfirmation:
                candles = load_live_candles_from_client(client.client, token_by_symbol[symbol], "15minute", 20)
                return TimeframeConfirmation.from_candles(candles)

            def fetch_symbol_data(symbol: str) -> tuple[pd.DataFrame, TimeframeConfirmation | None]:
                candles = load_symbol_candles(symbol)
                confirmation = load_confirmation(symbol) if uses_market_filters else None
                return candles, confirmation

            status_caption.empty()
            symbols = list(token_to_symbol.values())
            total_symbols = len(symbols)
            progress_bar = st.progress(0.0, text=f"Scanning {selected_strategy}: 0/{total_symbols} complete")

            scanner = StrategySignalScanner(buy_limit=5, sell_limit=5, minimum_score=int(getattr(strategy, "minimum_score", 80)))
            entry_window_open = settings.entry_start <= datetime.now().time() <= settings.entry_end
            if execute_entries and not entry_window_open:
                log_line("Entry window is closed -- signals will be scanned but not submitted this cycle.")

            buys: list[dict] = []
            sells: list[dict] = []
            scan_errors: list[str] = []
            insufficient_history: list[str] = []
            no_signal: list[tuple[str, str]] = []
            events = []
            capital_skipped = 0
            completed = 0

            worker_count = min(SCAN_MAX_WORKERS, total_symbols) if total_symbols else 1
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="intraday-scan") as executor:
                futures = {executor.submit(fetch_symbol_data, symbol): symbol for symbol in symbols}
                for future in as_completed(futures):
                    symbol = futures[future]
                    completed += 1
                    progress_bar.progress(completed / total_symbols if total_symbols else 1.0, text=f"Scanning {selected_strategy}: {completed}/{total_symbols} complete")
                    try:
                        candles, confirmation = future.result()
                    except Exception as error:
                        scan_errors.append(f"{symbol}: {error}")
                        continue
                    evaluation = scanner.evaluate_symbol(
                        symbol,
                        token_by_symbol.get(symbol),
                        strategy,
                        lambda _symbol, _candles=candles: _candles,
                        regime,
                        (lambda _symbol, _confirmation=confirmation: _confirmation) if uses_market_filters else None,
                    )
                    if evaluation.outcome == "error":
                        scan_errors.append(f"{symbol}: {evaluation.detail}")
                        continue
                    if evaluation.outcome == "insufficient_history":
                        insufficient_history.append(symbol)
                        continue
                    if evaluation.outcome == "no_signal":
                        no_signal.append((symbol, evaluation.detail))
                        continue
                    if evaluation.outcome == "sell":
                        sells.append(evaluation.row)
                        continue
                    row = evaluation.row
                    buys.append(row)
                    signal_time = pd.Timestamp(row["timestamp"])
                    now = pd.Timestamp.now(tz=signal_time.tz) if signal_time.tz is not None else pd.Timestamp.now()
                    fresh = pd.notna(signal_time) and -60 <= (now - signal_time).total_seconds() <= 600
                    valid_stop = row["current_price"] > 0 and row["stop_loss"] > 0 and row["stop_loss"] < row["entry_price"]
                    log_line(f"{symbol}: signal found (score {row['score']}, entry ₹{row['entry_price']:.2f})")
                    if not execute_entries:
                        continue
                    if not fresh:
                        log_line(f"{symbol}: not submitted -- signal candle is stale")
                        continue
                    if not valid_stop:
                        log_line(f"{symbol}: not submitted -- invalid price/stop combination")
                        continue
                    if not entry_window_open:
                        log_line(f"{symbol}: not submitted -- entry window closed")
                        continue
                    entry_price = float(row["entry_price"])
                    # intraday_capital_limit is the cash/margin budget per position; leverage
                    # (looked up live from Zerodha's margin calculator when possible -- see
                    # TradingPipeline.resolve_intraday_leverage) stretches that into more shares.
                    leverage = pipeline.resolve_intraday_leverage(symbol, entry_price)
                    quantity = int((intraday_capital_limit * leverage) // entry_price) if entry_price > 0 else 0
                    if quantity < 1:
                        capital_skipped += 1
                        log_line(f"{symbol}: not submitted -- capital limit smaller than one share at ₹{entry_price:.2f}")
                        continue
                    event = pipeline.submit_strategy_entry(
                        Signal(
                            symbol=symbol,
                            action=SignalAction(str(row["side"])),
                            timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
                            price=entry_price,
                            stop_loss=float(row["stop_loss"]),
                            reason=str(row["signal_reasons"]),
                            score=int(row["score"]),
                            entry_price=entry_price,
                            target_1=float(row["target_1"]) if pd.notna(row["target_1"]) else None,
                            target_2=float(row["target_2"]) if pd.notna(row["target_2"]) else None,
                            metadata={
                                "move_stop_to_breakeven_after_target_1": isinstance(strategy, HighConvictionLongStrategy),
                            },
                        ),
                        quantity=quantity,
                    )
                    events.append(event)
                    record_dashboard_events(st, [event])
                    if event.kind == "entry_submitted":
                        log_line(f"{symbol}: ENTRY SUBMITTED qty={quantity} @ ₹{entry_price:.2f}")
                    else:
                        log_line(f"{symbol}: {event.kind} -- {event.reason}")
            progress_bar.empty()

            result = SignalScanResult(
                buy=StrategySignalScanner._rank(buys, scanner.buy_limit),
                sell=StrategySignalScanner._rank(sells, scanner.sell_limit),
                errors=tuple(scan_errors),
                scanned=total_symbols,
                insufficient_history=tuple(insufficient_history),
                no_signal=tuple(no_signal),
            )
            st.session_state.automatic_signal_result = result
            st.session_state.automatic_signal_context = automatic_scan_context
            st.session_state.automatic_signal_regime = regime
            st.session_state.automatic_signal_time = run_started_at.strftime("%H:%M:%S")
            st.session_state.last_market_data_at = datetime.now()
            if execute_entries:
                record_signal_notifications(
                    st,
                    pd.concat([result.buy, result.sell], ignore_index=True),
                    selected_strategy,
                    universe_label,
                    selected_sector,
                    settings,
                )
            if not execute_entries:
                record_automatic_cycle(
                    f"{datetime.now().strftime('%I:%M:%S %p')} — scanned {result.scanned} · "
                    f"{len(buys)} buy · {len(sells)} sell signals (scan only)"
                )
                return
            if not entry_window_open:
                st.info("Automatic entries are paused outside the configured entry window.")
                record_automatic_cycle(
                    f"{datetime.now().strftime('%I:%M:%S %p')} — scanned {result.scanned} · entry window closed, no submissions"
                )
                return
            submitted = [event for event in events if event.kind == "entry_submitted"]
            rejected = [event for event in events if event.kind == "entry_rejected"]
            skipped = [event for event in events if event.kind == "entry_skipped"]
            if submitted:
                st.success(f"Submitted {len(submitted)} automatic entr{'y' if len(submitted) == 1 else 'ies'}.")
            if rejected:
                st.error("\n".join(f"{event.symbol}: {event.reason}" for event in rejected))
            if capital_skipped:
                st.warning(f"{capital_skipped} signal(s) skipped: maximum capital per position is smaller than one share at the current price.")
            if skipped and not submitted and not rejected:
                st.caption("All qualifying signals were already submitted or have open positions.")
            if not events and not capital_skipped:
                st.info("No strong signals qualified for automatic entry.")
            record_automatic_cycle(
                f"{datetime.now().strftime('%I:%M:%S %p')} — scanned {result.scanned} · "
                f"{len(buys)} qualifying · {len(submitted)} entered · {len(rejected)} rejected · {len(result.errors)} errors"
            )
        except Exception as error:
            logger.exception("Automatic scan/entry cycle failed")
            status_caption.empty()
            st.session_state.automatic_last_error = str(error)
            st.error(f"Automatic scan could not be completed: {error}")

    @st.fragment(run_every="300s")
    def render_automatic_status() -> None:
        live_automatic_enabled = bool(st.session_state.get("automatic_enabled", False))
        automatic_last_error = st.session_state.get("automatic_last_error")
        automatic_last_run_at = st.session_state.get("automatic_last_run_at")
        automatic_next_run_at = st.session_state.get("automatic_next_run_at")
        last_run_label = automatic_last_run_at.strftime("%I:%M:%S %p") if automatic_last_run_at else "No run yet"
        next_run_label = automatic_next_run_at.strftime("%I:%M:%S %p") if automatic_next_run_at else "pending first run"
        stalled = bool(live_automatic_enabled and automatic_next_run_at is not None and datetime.now() > automatic_next_run_at + timedelta(seconds=120))
        if not live_automatic_enabled:
            automatic_engine_state = "stopped"
        elif automatic_last_error:
            automatic_engine_state = "error"
        elif stalled:
            automatic_engine_state = "stalled"
        else:
            automatic_engine_state = "running"
        state_copy = {
            "running": (
                "Auto trade armed",
                f"{selected_strategy} · monitoring {len(token_to_symbol)} stocks from {universe_label} · next scan around {next_run_label} · last run {last_run_label}",
            ),
            "stopped": (
                "Auto trade stopped",
                f"{selected_strategy} · {universe_label} · {len(token_to_symbol)} stocks · last run {last_run_label}",
            ),
            "stalled": (
                "Scheduler heartbeat is stale",
                "Auto trade is enabled but has not completed a cycle recently",
            ),
            "error": (
                "Intratrading needs attention",
                escape((automatic_last_error or "The latest automatic cycle reported an error")[:240]),
            ),
        }[automatic_engine_state]
        automatic_badge = {
            "running": "LIVE",
            "stopped": "STOPPED",
            "stalled": "STALLED",
            "error": "NEEDS ATTENTION",
        }[automatic_engine_state]
        render_scan_activity_banner(
            st,
            "stalled" if automatic_engine_state in {"stalled", "stopped"} else automatic_engine_state,
            *state_copy,
            badge=automatic_badge,
        )

    render_automatic_status()

    if manual_scan_clicked:
        run_automatic_cycle(False)

    @st.fragment(run_every="300s")
    def automatic_scheduler_fragment():
        if st.session_state.get("automatic_enabled", False):
            # render_automatic_status() above is a *separate* fragment on its own independent
            # 300s timer -- it has no way to know this cycle just changed automatic_last_error
            # (e.g. a halt because the position cap is already reached) until its own timer next
            # fires, which can lag this one by minutes. That's exactly how the top banner could
            # keep showing "Auto trade armed / LIVE" while this fragment's own halted-scan
            # message sits right below it, contradicting it. Forcing a full rerun the moment the
            # error state actually changes keeps them in sync without rerunning on every tick.
            error_before = st.session_state.get("automatic_last_error")
            run_automatic_cycle(True)
            if st.session_state.get("automatic_last_error") != error_before:
                st.rerun()
        else:
            st.caption("Scheduler is idle until auto trade is started.")

    automatic_scheduler_fragment()

    automatic_cycle_log = st.session_state.get("automatic_cycle_log", [])
    if automatic_cycle_log:
        with st.expander("Recent scan cycles", expanded=False):
            st.caption("  \n".join(reversed(automatic_cycle_log[-5:])))
    automatic_activity = load_activity()[2]
    automatic_activity = automatic_activity[automatic_activity["event_kind"].isin(INTRADAY_ACTIVITY_EVENT_KINDS)]
    if not automatic_activity.empty:
        with st.expander(f"Recent Intratrading activity ({len(automatic_activity)})", expanded=False):
            st.dataframe(
                automatic_activity[["timestamp", "symbol", "event_kind", "reason"]].head(20),
                width="stretch",
                hide_index=True,
            )

    result = st.session_state.get("automatic_signal_result")
    if result is not None and st.session_state.get("automatic_signal_context") != automatic_scan_context:
        st.info("The last scan belongs to a different strategy or stock selection. Run Scan only to refresh it.")
        result = None
    if result is None:
        st.markdown(f'<div class="empty">Run a scan to evaluate every stock in the existing selection. Only signals scoring {getattr(strategy, "minimum_score", 80)} or higher are eligible.</div>', unsafe_allow_html=True)
        return
    regime = st.session_state.get("automatic_signal_regime")
    if regime is not None:
        st.info(f"NIFTY 50 regime: {regime.label} · close ₹{regime.close:,.2f} · VWAP ₹{regime.vwap:,.2f}")
    st.caption(f"Last run {st.session_state.get('automatic_signal_time', 'unknown')} · scanned {result.scanned} selected stocks · threshold score >= {getattr(strategy, 'minimum_score', 80)}")
    signals = pd.concat([result.buy, result.sell], ignore_index=True)
    if signals.empty:
        st.info("No strong BUY or SELL opportunities are available right now.")
    elif isinstance(strategy, PreviousDayHighBreakoutStrategy):
        st.subheader("Previous-day high breakout signals")
        breakout_table = signals.loc[:, ["symbol", "current_price", "previous_day_high", "entry_price", "stop_loss", "timestamp", "signal_reasons"]].rename(
            columns={
                "symbol": "Stock",
                "current_price": "Current price",
                "previous_day_high": "Previous day high",
                "entry_price": "Breakout price",
                "stop_loss": "Stop loss",
                "timestamp": "Trigger time",
                "signal_reasons": "Reason",
            }
        )
        st.dataframe(
            breakout_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Current price": st.column_config.NumberColumn(format="₹%.2f"),
                "Previous day high": st.column_config.NumberColumn(format="₹%.2f"),
                "Breakout price": st.column_config.NumberColumn(format="₹%.2f"),
                "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
                "Trigger time": st.column_config.DatetimeColumn(format="DD MMM YYYY, HH:mm"),
            },
        )
    elif isinstance(strategy, HighConvictionLongStrategy):
        st.subheader("High-conviction long signals")
        high_conviction_table = signals.loc[:, ["symbol", "score", "current_price", "entry_price", "stop_loss", "target_1", "target_2", "relative_volume", "vwap", "ema20", "ema50", "ema200", "signal_reasons"]].rename(
            columns={
                "symbol": "Symbol", "score": "Score", "current_price": "Current price", "entry_price": "Entry",
                "stop_loss": "Stop loss", "target_1": "Target 1", "target_2": "Target 2", "relative_volume": "RVOL",
                "vwap": "VWAP", "ema20": "EMA20", "ema50": "EMA50", "ema200": "EMA200", "signal_reasons": "Reasons",
            }
        )
        st.dataframe(
            high_conviction_table,
            width="stretch",
            hide_index=True,
            column_config={
                "Score": st.column_config.NumberColumn(format="%d/100"),
                "Current price": st.column_config.NumberColumn(format="₹%.2f"),
                "Entry": st.column_config.NumberColumn(format="₹%.2f"),
                "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
                "Target 1": st.column_config.NumberColumn(format="₹%.2f"),
                "Target 2": st.column_config.NumberColumn(format="₹%.2f"),
                "RVOL": st.column_config.NumberColumn(format="%.2fx"),
                "VWAP": st.column_config.NumberColumn(format="₹%.2f"),
                "EMA20": st.column_config.NumberColumn(format="₹%.2f"),
                "EMA50": st.column_config.NumberColumn(format="₹%.2f"),
                "EMA200": st.column_config.NumberColumn(format="₹%.2f"),
            },
        )
    else:
        st.dataframe(
            signals.loc[:, ["symbol", "side", "score", "current_price", "stop_loss", "target_2", "expected_value", "risk_reward", "signal_reasons"]].rename(
                columns={
                    "symbol": "Symbol", "side": "Side", "score": "Score", "current_price": "Current price",
                    "stop_loss": "Stop loss", "target_2": "Target 2", "expected_value": "Expected value",
                    "risk_reward": "Risk / reward", "signal_reasons": "Reasons",
                }
            ),
            width="stretch",
            hide_index=True,
            column_config={
                "Score": st.column_config.NumberColumn(format="%d"),
                "Current price": st.column_config.NumberColumn(format="₹%.2f"),
                "Stop loss": st.column_config.NumberColumn(format="₹%.2f"),
                "Target 2": st.column_config.NumberColumn(format="₹%.2f"),
                "Expected value": st.column_config.NumberColumn(format="₹%.2f"),
                "Risk / reward": st.column_config.NumberColumn(format="%.2f"),
            },
        )
    if result.errors:
        with st.expander(f"Skipped selected stocks ({len(result.errors)})"):
            st.write("\n".join(result.errors))
def render_overview(st, settings, orders: pd.DataFrame, trades: pd.DataFrame, activity: pd.DataFrame) -> None:
    today = date.today().isoformat()
    if not activity.empty:
        activity = activity.copy()
        activity["timestamp"] = pd.to_datetime(activity["timestamp"], errors="coerce")
        activity["event_kind"] = activity["event_kind"].astype(str)
        activity["pnl"] = pd.to_numeric(activity["pnl"], errors="coerce")
        today_activity = activity[activity["timestamp"].dt.strftime("%Y-%m-%d") == today]
        closed_activity = activity[activity["event_kind"].isin(["exit_submitted", "broker_exit_detected"])]
        net_pnl = float(activity["pnl"].sum())
        day_pnl = float(today_activity["pnl"].sum())
        submitted_count = int(activity["event_kind"].isin(["entry_submitted", "exit_submitted"]).sum())
        rejected_count = int(activity["event_kind"].str.endswith("_rejected").sum())
        closed_count = int(len(closed_activity))
    else:
        today_trades = trades[trades["exit_time"].astype(str).str.startswith(today)] if not trades.empty else trades
        net_pnl = float(trades["pnl"].sum()) if not trades.empty else 0.0
        day_pnl = float(today_trades["pnl"].sum()) if not today_trades.empty else 0.0
        submitted_count = int(len(orders))
        rejected_count = 0
        closed_count = int(len(trades))
    mode_label = "LIVE TRADING" if settings.trading_mode == TradingMode.LIVE else "PAPER TRADING"
    st.markdown('<div class="eyebrow">Session control room</div>', unsafe_allow_html=True)
    st.title("Intraday desk")
    st.markdown(f'<span class="status"><span class="status-dot"></span>{mode_label}</span>', unsafe_allow_html=True)
    st.divider()
    first, second, third, fourth = st.columns(4)
    first.metric("Simulated equity", f"₹{settings.initial_capital + net_pnl:,.0f}", f"₹{net_pnl:,.0f} total")
    second.metric("Today's P&L", f"₹{day_pnl:,.0f}")
    third.metric("Orders submitted", submitted_count)
    fourth.metric("Rejected", rejected_count)
    first, second, third, fourth = st.columns(4)
    first.metric("Closed trades", closed_count)
    if closed_count:
        wins = int((closed_activity["pnl"] > 0).sum()) if not activity.empty else int((trades["pnl"] > 0).sum())
        overall_win_rate = wins / closed_count
        second.metric("Win rate", f"{overall_win_rate:.1%}")
    else:
        overall_win_rate = 0.0
        second.metric("Win rate", "0.0%")
    third.metric("Capital deployment cap", f"{settings.max_capital_deployment:.0%}")
    fourth.metric("Ledger events", int(len(activity)) if not activity.empty else int(len(orders) + len(trades)))

    card_col, table_col = st.columns([1, 3])
    card_col.metric("Overall success ratio", f"{overall_win_rate:.1%}", f"{closed_count} closed trades")
    with table_col:
        st.markdown("**Strategy vs success ratio**")
        strategy_trades = trades.copy() if not trades.empty else pd.DataFrame(columns=["strategy_name", "pnl"])
        if strategy_trades.empty:
            st.markdown('<div class="empty">No closed trades with strategy tracking yet.</div>', unsafe_allow_html=True)
        else:
            strategy_trades["strategy_name"] = strategy_trades["strategy_name"].fillna("").replace("", "Unlabeled")
            strategy_trades["pnl"] = pd.to_numeric(strategy_trades["pnl"], errors="coerce").fillna(0.0)
            by_strategy = strategy_trades.groupby("strategy_name").agg(
                Trades=("pnl", "size"),
                Wins=("pnl", lambda values: int((values > 0).sum())),
                Net_PnL=("pnl", "sum"),
            ).reset_index()
            by_strategy["Success ratio"] = (by_strategy["Wins"] / by_strategy["Trades"]).map(lambda value: f"{value:.1%}")
            by_strategy = by_strategy.rename(columns={"strategy_name": "Strategy"}).sort_values("Net_PnL", ascending=False)
            st.dataframe(
                by_strategy[["Strategy", "Trades", "Wins", "Success ratio", "Net_PnL"]],
                width="stretch",
                hide_index=True,
                column_config={"Net_PnL": st.column_config.NumberColumn("Net P&L", format="₹%.2f")},
            )
    render_control_center(st, settings, activity)
    st.subheader("Equity path")
    equity_source = activity[activity["event_kind"].isin(["exit_submitted", "broker_exit_detected"])] if not activity.empty else trades
    if equity_source.empty:
        st.markdown('<div class="empty">No closed trades recorded yet.</div>', unsafe_allow_html=True)
    else:
        chart = equity_source.copy()
        time_column = "timestamp" if not activity.empty else "exit_time"
        chart[time_column] = pd.to_datetime(chart[time_column])
        chart = chart.sort_values(time_column).set_index(time_column)
        chart["equity"] = settings.initial_capital + chart["pnl"].cumsum()
        st.line_chart(chart["equity"], height=260)
    if not activity.empty:
        st.subheader("Daily ledger")
        daily = activity.assign(day=activity["timestamp"].dt.strftime("%Y-%m-%d"))
        daily = daily.groupby("day", as_index=False).agg(
            Events=("event_kind", "size"),
            Submitted=("event_kind", lambda values: values.isin(["entry_submitted", "exit_submitted"]).sum()),
            Rejected=("event_kind", lambda values: values.astype(str).str.endswith("_rejected").sum()),
            Closed=("event_kind", lambda values: values.isin(["exit_submitted", "broker_exit_detected"]).sum()),
            PnL=("pnl", "sum"),
        ).sort_values("day", ascending=False)
        st.dataframe(daily, width="stretch", hide_index=True, column_config={"PnL": st.column_config.NumberColumn(format="₹%.2f")})
        rejection_rows = activity[activity["event_kind"].str.endswith("_rejected")]
        if not rejection_rows.empty:
            st.subheader("Rejection reasons")
            rejection_summary = rejection_rows.groupby("reason", dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False)
            st.dataframe(rejection_summary, width="stretch", hide_index=True)
        if not closed_activity.empty:
            closed_activity = closed_activity.copy()
            closed_activity["pnl"] = pd.to_numeric(closed_activity["pnl"], errors="coerce").fillna(0.0)
            gross_profit = float(closed_activity.loc[closed_activity["pnl"] > 0, "pnl"].sum())
            gross_loss = abs(float(closed_activity.loc[closed_activity["pnl"] < 0, "pnl"].sum()))
            profit_factor = gross_profit / gross_loss if gross_loss else float("inf") if gross_profit else 0.0
            equity_curve = settings.initial_capital + closed_activity.sort_values("timestamp")["pnl"].cumsum()
            drawdown = equity_curve - equity_curve.cummax()
            metric_one, metric_two, metric_three, metric_four = st.columns(4)
            metric_one.metric("Average win", f"₹{closed_activity.loc[closed_activity['pnl'] > 0, 'pnl'].mean():,.2f}" if (closed_activity["pnl"] > 0).any() else "₹0.00")
            metric_two.metric("Average loss", f"₹{closed_activity.loc[closed_activity['pnl'] < 0, 'pnl'].mean():,.2f}" if (closed_activity["pnl"] < 0).any() else "₹0.00")
            metric_three.metric("Profit factor", "∞" if profit_factor == float("inf") else f"{profit_factor:.2f}")
            metric_four.metric("Max drawdown", f"₹{drawdown.min():,.2f}")
            by_side = closed_activity.groupby("side", dropna=False).agg(Trades=("pnl", "size"), Net_PnL=("pnl", "sum"), Average_PnL=("pnl", "mean")).reset_index()
            by_side = by_side.rename(columns={"side": "Exit side"})
            by_hour = closed_activity.assign(Hour=closed_activity["timestamp"].dt.strftime("%H:00")).groupby("Hour", as_index=False).agg(Trades=("pnl", "size"), Net_PnL=("pnl", "sum"))
            st.dataframe(by_side, width="stretch", hide_index=True, column_config={"Net_PnL": st.column_config.NumberColumn(format="₹%.2f"), "Average_PnL": st.column_config.NumberColumn(format="₹%.2f")})
            st.dataframe(by_hour, width="stretch", hide_index=True, column_config={"Net_PnL": st.column_config.NumberColumn(format="₹%.2f")})
    st.subheader("Current orders")
    render_position_monitor(st, settings, runtime_access_token(st, settings))
    pipeline = st.session_state.get("dashboard_pipeline")
    if pipeline is not None and pipeline.managed_positions:
        with st.form("close_all_positions_form"):
            close_confirmed = st.checkbox(
                "Confirm that every monitored position should be closed at the current available price.",
                key="close_all_positions_confirmation",
            )
            close_submitted = st.form_submit_button("Close all positions", type="primary", icon=":material/close:")
        if close_submitted:
            if not close_confirmed:
                st.warning("Confirm the close-all action before submitting it.")
            else:
                events = pipeline.close_all_positions()
                record_dashboard_events(st, events)
                st.success(f"Processed {len(events)} close request(s).")
                st.rerun()


def main() -> None:
    st.set_page_config(page_title="Garuda Trading", page_icon=":material/candlestick_chart:", layout="wide")
    inject_styles(st)
    base_settings = get_settings()
    settings = get_frontend_settings(st, base_settings)
    orders, trades, activity = load_activity()

    authenticated = broker_credentials_configured(settings) and bool(verified_kite_access_token(st, settings))
    pages = WORKSPACE_PAGES if authenticated else ["Kite authentication"]
    st.session_state.setdefault("active_page", "Kite authentication")
    if st.session_state.active_page not in pages:
        st.session_state.active_page = pages[0]
    if not authenticated or st.session_state.get("workspace_navigation") not in pages:
        st.session_state.workspace_navigation = st.session_state.active_page

    with st.sidebar:
        st.session_state.setdefault("sidebar_compact", False)
        compact = st.session_state.sidebar_compact
        if compact:
            st.markdown(
                '<style>[data-testid="stSidebar"] { width: 96px !important; min-width: 96px !important; }</style>',
                unsafe_allow_html=True,
            )
        toggle_column, title_column = st.columns([1, 5]) if not compact else (st.container(), None)
        with toggle_column:
            if st.button(
                " ",
                key="sidebar_logo_toggle",
                help="Expand sidebar" if compact else "Collapse to icons",
            ):
                st.session_state.sidebar_compact = not compact
                st.rerun()
        if not compact:
            with title_column:
                st.markdown('<div class="sidebar-brand-title">Garuda Trading</div>', unsafe_allow_html=True)
        page = st.radio(
            "Workspace",
            pages,
            index=None,
            key="workspace_navigation",
            width="stretch",
            label_visibility="collapsed",
            format_func=(
                (lambda name: f":material/{SIDEBAR_PAGE_ICONS.get(name, 'circle')}:")
                if compact
                else (lambda name: f":material/{SIDEBAR_PAGE_ICONS.get(name, 'circle')}: {name}")
            ),
        )
        previous_page = st.session_state.get("previous_workspace_page")
        st.session_state.active_page = page
        st.session_state.previous_workspace_page = page
        if page != previous_page:
            # Auto trading must never keep running silently once its control page is left --
            # arming it again always requires an explicit, freshly-confirmed Start click.
            # (Unattended execution belongs to the standalone trailing-stop agent process, not
            # to session state that outlives a glance at another dashboard page.)
            if previous_page == "Swing auto trading":
                st.session_state.swing_auto_enabled = False
                st.session_state.swing_kill_switch = False
                st.session_state.swing_live_confirmation = False
            if previous_page == "Intratrading":
                st.session_state.automatic_enabled = False
        if not compact and not authenticated:
            st.caption("Complete both Kite authentication steps to unlock the workspace.")
        if not compact:
            st.divider()
            st.caption(f"Session date  {date.today().isoformat()}")
            st.caption(f"Broker mode  {settings.trading_mode.value}")
        if authenticated and not compact:
            st.divider()
            if st.session_state.get("emergency_halt"):
                st.caption("New entries halted")
                if st.button("Resume entries", width="stretch", icon=":material/play_arrow:"):
                    pipeline = st.session_state.get("dashboard_pipeline")
                    if pipeline is not None:
                        pipeline.resume_entries()
                    st.session_state.emergency_halt = False
                    st.rerun()
            else:
                with st.form("sidebar_emergency_halt_form"):
                    halt_confirmed = st.checkbox("Confirm halt", key="sidebar_halt_confirmation")
                    halt_submitted = st.form_submit_button("Emergency stop", width="stretch", icon=":material/stop_circle:")
                if halt_submitted:
                    if not halt_confirmed:
                        st.warning("Confirm the emergency halt before submitting it.")
                    else:
                        pipeline = st.session_state.get("dashboard_pipeline")
                        if pipeline is not None:
                            pipeline.halt_entries()
                        st.session_state.automatic_enabled = False
                        st.session_state.emergency_halt = True
                        st.rerun()

    page_content = st.empty()
    page_content.empty()
    with page_content.container():
        if page == "Kite authentication":
            render_kite_authentication(st, settings)
        elif page == "Overview":
            render_overview(st, settings, orders, trades, activity)
        elif page == "Live monitor":
            render_live_monitor(st, settings)
        elif page == "P&L statement":
            render_pnl_statement(st, settings)
        elif page == "Watchlists":
            render_watchlists(st, settings)
        elif page == "Scanner & signals":
            render_signal_feed(st, settings)
        elif page == "Backtesting":
            render_backtesting(st, settings)
        elif page == "Swing auto trading":
            render_swing_auto_trading(st, settings)
        elif page == "Intratrading":
            render_automatic_trading(st, settings)
        elif page == "Risk & settings":
            render_risk_settings(st, settings)


if __name__ == "__main__":
    main()
