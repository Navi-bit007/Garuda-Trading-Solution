from datetime import datetime

from app.monitoring import notifications
from app.database.database import Database
from app.database.models import NotificationRecord
from app.database.repository import Repository


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        return False


def test_telegram_notifier_posts_only_when_enabled(monkeypatch):
    requests = []

    def fake_urlopen(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(notifications, "urlopen", fake_urlopen)
    notifier = notifications.Notifier(True, "token", "chat")

    notifier.send("entry_submitted AAA")

    assert requests[0][1] == 10
    assert requests[0][0].full_url == "https://api.telegram.org/bottoken/sendMessage"


def test_disabled_notifier_does_not_make_network_requests(monkeypatch):
    called = False

    def fake_urlopen(request, timeout):
        nonlocal called
        called = True
        return FakeResponse()

    monkeypatch.setattr(notifications, "urlopen", fake_urlopen)
    notifications.Notifier(False, "token", "chat").send("ignored")

    assert not called


def test_signal_notifications_are_persisted_once(tmp_path):
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    repository = Repository(database)
    notification = NotificationRecord(
        strategy="EMA entry/exit (simple)",
        universe="NIFTY 500",
        sector="All sectors",
        symbol="AAA",
        side="BUY",
        score=100,
        signal_timestamp=datetime(2026, 1, 1, 10, 0),
        created_at=datetime(2026, 1, 1, 10, 0, 1),
        message="BUY signal: AAA",
    )

    assert repository.save_notification(notification)
    assert not repository.save_notification(notification)
    loaded = repository.load_notifications()
    assert len(loaded) == 1
    assert loaded[0].message == "BUY signal: AAA"
    assert not loaded[0].read

    database.close()
