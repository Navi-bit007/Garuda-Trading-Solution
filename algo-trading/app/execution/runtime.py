from __future__ import annotations

import logging
import threading
from typing import Any

from app.broker.authentication import AccessToken, require_credentials
from app.broker.kite_client import KiteClient
from app.broker.tick_stream import KiteTickStream
from app.config.constants import TradingMode
from app.database.database import Database
from app.database.repository import Repository
from app.config.settings import Settings
from app.execution.trading_pipeline import PipelineEvent, TradingPipeline
from app.market.universe import load_nifty_index_token_map_from_api
from app.strategy.vwap_ema_breakout import VwapEmaBreakoutStrategy


logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECONDS = 20.0


class TradingRuntime:
    def __init__(self, pipeline: TradingPipeline, stream: KiteTickStream, reconcile_interval_seconds: float = RECONCILE_INTERVAL_SECONDS):
        self.pipeline = pipeline
        self.stream = stream
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self._reconcile_stop = threading.Event()
        self._reconcile_thread: threading.Thread | None = None

    def start(self) -> None:
        self.stream.start(
            self.pipeline.scanner.token_to_symbol.keys(),
            self._on_ticks,
            on_connect=lambda response: logger.info("Kite tick stream connected"),
            on_close=lambda response: logger.warning("Kite tick stream closed: %s", response),
            on_error=lambda response: logger.error("Kite tick stream error: %s", response),
            on_reconnect=lambda response: logger.warning("Kite tick stream reconnecting: %s", response),
            on_noreconnect=lambda response: logger.error("Kite tick stream stopped reconnecting"),
        )
        self._reconcile_stop.clear()
        self._reconcile_thread = threading.Thread(target=self._reconcile_loop, daemon=True)
        self._reconcile_thread.start()

    def stop(self) -> None:
        self.stream.stop()
        self._reconcile_stop.set()
        if self._reconcile_thread is not None:
            self._reconcile_thread.join(timeout=5.0)
            self._reconcile_thread = None

    def _reconcile_loop(self) -> None:
        # A standalone live/paper run has no dashboard periodically calling
        # sync_broker_positions(); without this, a position closed by the trailing-stop
        # agent's broker-side SL-M fill would never be noticed here, leaving managed_positions
        # stale and blocking new entries for that symbol.
        while not self._reconcile_stop.wait(self.reconcile_interval_seconds):
            try:
                for event in self.pipeline.sync_broker_positions():
                    self._log_event(event)
            except Exception:
                logger.exception("Broker position reconciliation failed")

    def _on_ticks(self, ticks: list[dict]) -> None:
        try:
            for event in self.pipeline.on_ticks(ticks):
                self._log_event(event)
        except Exception:
            logger.exception("Tick batch processing failed")

    @staticmethod
    def _log_event(event: PipelineEvent) -> None:
        message = "%s %s price=%s reason=%s"
        logger.info(message, event.kind, event.symbol, event.price, event.reason)


def build_runtime(settings: Settings) -> TradingRuntime:
    token = settings.kite_access_token.get_secret_value().strip()
    if not settings.kite_api_key.strip() or not token:
        raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN are required for realtime ticks")
    require_credentials(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker = KiteClient(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker.connect(AccessToken(token))
    token_to_symbol = load_nifty_index_token_map_from_api(broker.client, "NIFTY 500")
    broker_client: Any = broker.client if settings.trading_mode == TradingMode.LIVE else None
    database = Database()
    database.initialize()
    pipeline = TradingPipeline(settings, token_to_symbol, VwapEmaBreakoutStrategy(), broker_client, Repository(database))
    return TradingRuntime(pipeline, KiteTickStream(settings.kite_api_key, token))