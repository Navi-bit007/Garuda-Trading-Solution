from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from app.database.models import NotificationRecord


class Notifier:
    def __init__(self, enabled: bool = False, bot_token: str = "", chat_id: str = ""):
        self.enabled = enabled
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.logger = logging.getLogger(__name__)

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        if not self.bot_token or not self.chat_id:
            self.logger.warning("notifications enabled but Telegram credentials are incomplete")
            return
        request = Request(
            f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
            data=urlencode({"chat_id": self.chat_id, "text": message}).encode(),
            method="POST",
        )
        try:
            with urlopen(request, timeout=10):
                pass
        except Exception as error:
            self.logger.warning("notification delivery failed: %s", error)


def notifier_from_settings(settings) -> Notifier:
    """Build a `Notifier` from a `Settings` instance's `enable_telegram`/`telegram_bot_token`/
    `telegram_chat_id` fields. Shared by every caller that owns a `Settings` object
    (`TradingPipeline`, `SwingAutoTrader`, ...) so the `SecretStr`-unwrapping logic lives in one
    place instead of being duplicated at each construction site."""
    bot_token = getattr(settings, "telegram_bot_token", "")
    return Notifier(
        bool(getattr(settings, "enable_telegram", False)),
        bot_token.get_secret_value() if hasattr(bot_token, "get_secret_value") else str(bot_token),
        str(getattr(settings, "telegram_chat_id", "")),
    )


def notification_message(notification: NotificationRecord) -> str:
    return (
        f"{notification.side} signal: {notification.symbol} at {notification.signal_timestamp:%Y-%m-%d %H:%M} "
        f"score={notification.score}"
    )


def save_signal_notifications(repository, signals, strategy_label: str, universe_label: str, sector: str, notifier: Notifier | None = None) -> list[NotificationRecord]:
    if signals.empty:
        return []
    created_at = datetime.now()
    new_notifications: list[NotificationRecord] = []
    for _, row in signals.iterrows():
        notification = NotificationRecord(
            strategy=strategy_label,
            universe=universe_label,
            sector=sector,
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            score=int(row["score"]),
            signal_timestamp=pd.Timestamp(row["timestamp"]).to_pydatetime(),
            created_at=created_at,
            message="",
            instrument_token=int(row["instrument_token"]) if pd.notna(row.get("instrument_token")) else None,
        )
        notification = replace(notification, message=notification_message(notification) + f" reasons={row.get('signal_reasons', '')}")
        if repository.save_notification(notification):
            new_notifications.append(notification)
            if notifier is not None:
                notifier.send(notification.message)
    return new_notifications
