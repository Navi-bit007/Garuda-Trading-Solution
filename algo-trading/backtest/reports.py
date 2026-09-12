from __future__ import annotations

import json
from pathlib import Path


def write_report(metrics: dict, path: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(metrics, indent=2, allow_nan=False), encoding="utf-8")
