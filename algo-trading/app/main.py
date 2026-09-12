from __future__ import annotations

import logging
from threading import Event

from app.config.settings import get_settings
from app.monitoring.signal_engine import build_signal_engine


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    engine, database = build_signal_engine(settings)
    engine.start()
    logging.info("Always-on signal engine started for %s candles", settings.signal_timeframe)
    if settings.trading_mode.value == "PAPER":
        logging.info("Paper mode is active; signal generation does not submit orders")
    try:
        Event().wait()
    except KeyboardInterrupt:
        logging.info("Stopping always-on signal engine")
    finally:
        if engine.stop():
            database.close()
        else:
            logging.warning("Signal engine did not stop before shutdown; leaving database connection open")


if __name__ == "__main__":
    main()
