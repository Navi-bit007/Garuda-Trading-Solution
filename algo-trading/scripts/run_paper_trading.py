from __future__ import annotations

import argparse
import logging

from app.config.settings import get_settings
from app.execution.runtime import build_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the deterministic Kite tick-to-order runtime")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = build_runtime(get_settings())
    try:
        runtime.start()
        logging.info("Realtime trading runtime started")
        input("Press Enter to stop the runtime.\n")
    finally:
        runtime.stop()


if __name__ == "__main__":
    main()
