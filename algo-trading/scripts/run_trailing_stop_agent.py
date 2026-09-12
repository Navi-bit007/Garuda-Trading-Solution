from __future__ import annotations

import argparse
import logging
import signal
import threading

from app.broker.authentication import AccessToken, require_credentials
from app.broker.kite_client import KiteClient
from app.broker.market_data import MarketData
from app.broker.order_api import OrderAPI
from app.config.settings import Settings, get_settings
from app.database.database import Database
from app.database.repository import Repository
from app.execution.trailing_stop_agent import TrailingStopAgent
from app.monitoring.notifications import Notifier


def build_agent(settings: Settings) -> TrailingStopAgent:
    token = settings.kite_access_token.get_secret_value().strip()
    if not settings.kite_api_key.strip() or not token:
        raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN are required to trail stops")
    require_credentials(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker = KiteClient(settings.kite_api_key, settings.kite_api_secret.get_secret_value())
    broker.connect(AccessToken(token))
    database = Database()
    database.initialize()
    repository = Repository(database)
    orders = OrderAPI(settings.trading_mode, broker.client)
    market_data = MarketData(broker.client)
    notifier = Notifier(settings.enable_telegram, settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id)
    return TrailingStopAgent(settings, repository, orders, market_data, broker_client=broker.client, notifier=notifier)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Standalone broker-synced trailing stop-loss agent. Watches every open intraday and "
            "swing position and walks the resting SL-M order at Zerodha as price moves favorably "
            "-- Kite Connect's GTT API has no trailing trigger, so this replaces it. Runs "
            "independently of the dashboard; keep it running for as long as positions are open."
        )
    )
    parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    agent = build_agent(get_settings())
    stop_event = threading.Event()

    def _request_stop(signum, frame) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    agent.start()
    logging.info("Trailing stop agent started")
    stop_event.wait()
    logging.info("Stopping trailing stop agent")
    agent.stop()


if __name__ == "__main__":
    main()
