from __future__ import annotations

from typing import Any

from app.broker.authentication import AccessToken, require_credentials


class KiteClient:
    def __init__(self, api_key: str, api_secret: str, access_token: AccessToken | None = None):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self._client: Any = None

    def connect(self, access_token: AccessToken) -> None:
        require_credentials(self.api_key, self.api_secret)
        try:
            from kiteconnect import KiteConnect
        except ImportError as exc:
            raise RuntimeError("Install kiteconnect before using the live broker adapter") from exc
        self._client = KiteConnect(api_key=self.api_key)
        self._client.set_access_token(access_token.value)
        self.access_token = access_token

    @property
    def client(self) -> Any:
        if self._client is None:
            raise RuntimeError("Kite client is not connected")
        return self._client
