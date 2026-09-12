from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description="Download Kite instrument metadata")
    parser.add_argument("--output", required=True)
    parser.parse_args()
    raise SystemExit("Connect an authenticated Kite client before downloading instruments")


if __name__ == "__main__":
    main()
