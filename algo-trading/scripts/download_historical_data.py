from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical OHLCV data")
    parser.add_argument("--output", required=True)
    parser.parse_args()
    raise SystemExit("Connect an authenticated Kite client before downloading historical data")


if __name__ == "__main__":
    main()
