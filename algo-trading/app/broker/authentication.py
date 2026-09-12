from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AccessToken:
    value: str


class AuthenticationError(RuntimeError):
    pass


def require_credentials(api_key: str, api_secret: str) -> None:
    if not api_key or not api_secret:
        raise AuthenticationError("Kite API credentials are required")


def exchange_request_token(api_key: str, api_secret: str, request_token: str) -> AccessToken:
    require_credentials(api_key, api_secret)
    if not request_token.strip():
        raise AuthenticationError("A Kite request token is required")
    try:
        from kiteconnect import KiteConnect
    except ImportError as exc:
        raise AuthenticationError("Install kiteconnect before generating a live access token") from exc

    try:
        client = KiteConnect(api_key=api_key)
        session = client.generate_session(request_token.strip(), api_secret=api_secret)
    except Exception as exc:
        raise AuthenticationError("Kite token exchange failed. Generate a fresh request token and try again.") from exc

    access_token = session.get("access_token")
    if not access_token:
        raise AuthenticationError("Kite did not return an access token")
    return AccessToken(access_token)
