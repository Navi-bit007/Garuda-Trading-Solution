from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


TickHandler = Callable[[list[dict]], None]
ConnectionHandler = Callable[[Any], None]


class KiteTickStream:
    def __init__(self, api_key: str, access_token: str):
        self.api_key = api_key
        self.access_token = access_token
        self._ticker: Any = None

    def start(
        self,
        instrument_tokens: Iterable[int],
        on_ticks: TickHandler,
        on_connect: ConnectionHandler | None = None,
        on_close: ConnectionHandler | None = None,
        on_error: ConnectionHandler | None = None,
        on_reconnect: ConnectionHandler | None = None,
        on_noreconnect: ConnectionHandler | None = None,
    ) -> None:
        tokens = sorted({int(token) for token in instrument_tokens})
        if not tokens:
            raise ValueError("at least one instrument token is required")
        try:
            from kiteconnect import KiteTicker
        except ImportError as exc:
            raise RuntimeError("Install kiteconnect before starting the Kite tick stream") from exc

        self._ticker = KiteTicker(self.api_key, self.access_token)

        def handle_connect(websocket: Any, response: Any) -> None:
            websocket.subscribe(tokens)
            websocket.set_mode(websocket.MODE_FULL, tokens)
            if on_connect:
                on_connect(response)

        def handle_close(websocket: Any, code: Any, reason: Any) -> None:
            if on_close:
                on_close({"code": code, "reason": reason})

        def handle_error(websocket: Any, code: Any, reason: Any) -> None:
            if on_error:
                on_error({"code": code, "reason": reason})

        def handle_reconnect(websocket: Any, attempts: int) -> None:
            if on_reconnect:
                on_reconnect({"attempts": attempts})

        def handle_noreconnect(websocket: Any) -> None:
            if on_noreconnect:
                on_noreconnect({})

        self._ticker.on_connect = handle_connect
        self._ticker.on_ticks = lambda websocket, ticks: on_ticks(ticks)
        self._ticker.on_close = handle_close
        self._ticker.on_error = handle_error
        self._ticker.on_reconnect = handle_reconnect
        self._ticker.on_noreconnect = handle_noreconnect
        self._ticker.connect(threaded=True)

    def stop(self) -> None:
        if self._ticker is not None:
            self._ticker.close()
            self._ticker = None
