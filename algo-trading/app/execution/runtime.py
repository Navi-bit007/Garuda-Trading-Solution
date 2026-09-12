from __future__ import annotations

import logging
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


class TradingRuntime:
    def __init__(self, pipeline: TradingPipeline, stream: KiteTickStream):
        self.pipeline = pipeline
        self.stream = stream

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

    def stop(self) -> None:
        self.stream.stop()

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