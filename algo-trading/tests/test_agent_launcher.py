import os
from datetime import date, datetime, timedelta

from app.database.database import Database
from app.database.models import AgentHeartbeat
from app.database.repository import Repository
from app.execution.agent_launcher import (
    REPO_ROOT,
    agent_heartbeat_is_fresh,
    launch_trailing_stop_agent,
    maybe_autostart_trailing_agent,
    stop_trailing_stop_agent,
    trailing_agent_env_from_settings,
)


def build_repository(tmp_path) -> tuple[Database, Repository]:
    database = Database(str(tmp_path / "trading.sqlite3"))
    database.initialize()
    return database, Repository(database)


def test_heartbeat_is_fresh_within_window(tmp_path):
    database, repository = build_repository(tmp_path)
    now = datetime(2026, 1, 1, 9, 30)
    repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", now, "", now))

    assert agent_heartbeat_is_fresh(repository, now=now + timedelta(seconds=30)) is True
    assert agent_heartbeat_is_fresh(repository, now=now + timedelta(seconds=200)) is False
    database.close()


def test_heartbeat_is_not_fresh_when_missing(tmp_path):
    database, repository = build_repository(tmp_path)

    assert agent_heartbeat_is_fresh(repository) is False
    database.close()


def test_autostart_skips_launch_when_heartbeat_is_fresh(tmp_path, monkeypatch):
    database, repository = build_repository(tmp_path)
    now = datetime.now()
    repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", now, "", now))

    launched = []
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    result = maybe_autostart_trailing_agent(repository, "key", "secret", "token", today=date(2026, 1, 1))

    assert result is None
    assert launched == []
    database.close()


def test_autostart_launches_when_heartbeat_is_stale_or_missing(tmp_path, monkeypatch):
    database, repository = build_repository(tmp_path)
    launched = []
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda api_key, api_secret, access_token, repository, **kwargs: launched.append((api_key, api_secret, access_token, repository, kwargs)) or "process",
    )

    result = maybe_autostart_trailing_agent(repository, "key", "secret", "token", today=date(2026, 1, 1))

    assert result == "process"
    assert launched == [("key", "secret", "token", repository, {"extra_env": None})]
    database.close()


def test_autostart_skips_launch_on_a_weekend_even_with_a_stale_heartbeat(tmp_path, monkeypatch):
    database, repository = build_repository(tmp_path)
    launched = []
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda *args, **kwargs: launched.append((args, kwargs)) or "process",
    )

    result = maybe_autostart_trailing_agent(repository, "key", "secret", "token", today=date(2026, 9, 19))  # Saturday

    assert result is None
    assert launched == []
    database.close()


def test_autostart_skips_launch_on_a_configured_holiday(tmp_path, monkeypatch):
    database, repository = build_repository(tmp_path)
    launched = []
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda *args, **kwargs: launched.append((args, kwargs)) or "process",
    )

    result = maybe_autostart_trailing_agent(
        repository, "key", "secret", "token", market_holidays="2026-01-26", today=date(2026, 1, 26)
    )

    assert result is None
    assert launched == []
    database.close()


def test_autostart_passes_extra_env_through_to_launch(tmp_path, monkeypatch):
    """Risk settings changes (e.g. the minimum stop-improvement threshold) are only ever
    persisted to the per-user dashboard_settings DB row -- the standalone agent's own
    get_settings() call never reads that table, only .env/process environment. Without
    threading extra_env through here too, auto-starting the agent on login would silently keep
    using stale .env-level defaults instead of whatever the user configured."""
    database, repository = build_repository(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda api_key, api_secret, access_token, repository, **kwargs: captured.update(kwargs) or "process",
    )

    maybe_autostart_trailing_agent(
        repository, "key", "secret", "token", extra_env={"MIN_STOP_IMPROVEMENT_PCT": "0.1"}, today=date(2026, 1, 1)
    )

    assert captured == {"extra_env": {"MIN_STOP_IMPROVEMENT_PCT": "0.1"}}
    database.close()


class FakeProcess:
    def __init__(self, returncode: int | None, pid: int = 4242):
        self.returncode = returncode
        self.pid = pid

    def poll(self) -> int | None:
        return self.returncode


def test_launch_puts_repo_root_on_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setattr("app.execution.agent_launcher.AGENT_LOG_PATH", tmp_path / "agent.log")
    monkeypatch.setattr("app.execution.agent_launcher.STARTUP_GRACE_SECONDS", 0)
    captured = {}

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess(returncode=None)

    monkeypatch.setattr("app.execution.agent_launcher.subprocess.Popen", fake_popen)

    launch_trailing_stop_agent("key", "secret", "token")

    pythonpath_entries = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert str(REPO_ROOT) in pythonpath_entries
    assert captured["env"]["KITE_ACCESS_TOKEN"] == "token"
    assert captured["env"]["TRADING_MODE"] == "LIVE"


def test_launch_forwards_extra_env_into_the_child_process(tmp_path, monkeypatch):
    """A risk setting (e.g. the minimum stop-improvement threshold) changed in the dashboard is
    only ever persisted to the per-user dashboard_settings DB row -- the standalone agent's own
    get_settings() call reads only .env/process environment and has no idea that row exists.
    extra_env is how a freshly (re)launched agent is told what's actually configured, instead of
    silently falling back to a stale .env-level default."""
    monkeypatch.setattr("app.execution.agent_launcher.AGENT_LOG_PATH", tmp_path / "agent.log")
    monkeypatch.setattr("app.execution.agent_launcher.STARTUP_GRACE_SECONDS", 0)
    captured = {}

    def fake_popen(command, **kwargs):
        captured["env"] = kwargs["env"]
        return FakeProcess(returncode=None)

    monkeypatch.setattr("app.execution.agent_launcher.subprocess.Popen", fake_popen)

    launch_trailing_stop_agent("key", "secret", "token", extra_env={"MIN_STOP_IMPROVEMENT_PCT": "0.1", "FORCE_EXIT": "15:20:00"})

    assert captured["env"]["MIN_STOP_IMPROVEMENT_PCT"] == "0.1"
    assert captured["env"]["FORCE_EXIT"] == "15:20:00"
    # Credentials/PYTHONPATH must still be set as before -- extra_env only adds to the child's
    # environment, it never replaces the rest of it.
    assert captured["env"]["KITE_ACCESS_TOKEN"] == "token"


def test_trailing_agent_env_from_settings_mirrors_the_effective_settings():
    from datetime import time
    from types import SimpleNamespace

    settings = SimpleNamespace(
        force_exit=time(15, 20),
        agent_shutdown_time=time(15, 45),
        trailing_atr_multiplier=1.75,
        swing_trailing_atr_multiplier=2.5,
        min_stop_improvement_pct=0.1,
    )

    env = trailing_agent_env_from_settings(settings)

    assert env == {
        "FORCE_EXIT": "15:20:00",
        "AGENT_SHUTDOWN_TIME": "15:45:00",
        "TRAILING_ATR_MULTIPLIER": "1.75",
        "SWING_TRAILING_ATR_MULTIPLIER": "2.5",
        "MIN_STOP_IMPROVEMENT_PCT": "0.1",
    }


def test_trailing_agent_env_from_settings_falls_back_on_missing_attributes():
    env = trailing_agent_env_from_settings(object())

    assert env == {
        "FORCE_EXIT": "15:15:00",
        "AGENT_SHUTDOWN_TIME": "15:40:00",
        "TRAILING_ATR_MULTIPLIER": "1.5",
        "SWING_TRAILING_ATR_MULTIPLIER": "2.0",
        "MIN_STOP_IMPROVEMENT_PCT": "0.25",
    }


def test_launch_records_error_heartbeat_on_immediate_crash(tmp_path, monkeypatch):
    log_path = tmp_path / "agent.log"
    log_path.write_text("Traceback (most recent call last):\nModuleNotFoundError: No module named 'app'\n")
    monkeypatch.setattr("app.execution.agent_launcher.AGENT_LOG_PATH", log_path)
    monkeypatch.setattr("app.execution.agent_launcher.STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        "app.execution.agent_launcher.subprocess.Popen",
        lambda command, **kwargs: FakeProcess(returncode=1),
    )

    database, repository = build_repository(tmp_path)

    launch_trailing_stop_agent("key", "secret", "token", repository)

    heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
    assert heartbeat is not None
    assert "ModuleNotFoundError" in heartbeat.last_error
    database.close()


def test_launch_does_not_record_an_error_heartbeat_when_process_stays_alive(tmp_path, monkeypatch):
    """A process that's still running after the startup grace period shouldn't have an error
    heartbeat written for it -- a heartbeat row now does exist immediately on launch (it records
    the PID so the "Stop agent" button can find it later, see save_agent_pid), but its last_error
    must stay empty until/unless the agent's own real heartbeat says otherwise."""
    monkeypatch.setattr("app.execution.agent_launcher.AGENT_LOG_PATH", tmp_path / "agent.log")
    monkeypatch.setattr("app.execution.agent_launcher.STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(
        "app.execution.agent_launcher.subprocess.Popen",
        lambda command, **kwargs: FakeProcess(returncode=None),
    )

    database, repository = build_repository(tmp_path)

    launch_trailing_stop_agent("key", "secret", "token", repository)

    heartbeat = repository.load_agent_heartbeat("trailing_stop_agent")
    assert heartbeat is not None
    assert heartbeat.last_error == ""
    assert repository.get_agent_pid("trailing_stop_agent") == 4242
    database.close()


def test_stop_agent_kills_the_recorded_pid_and_clears_the_heartbeat(tmp_path, monkeypatch):
    """The "Stop agent" button on the Kite authentication page needs a way back to a process that
    was deliberately launched detached (see launch_trailing_stop_agent's docstring) -- its PID,
    recorded at launch by save_agent_pid, is the only handle available. After stopping it, the
    heartbeat is cleared immediately so the dashboard shows "not running" right away instead of
    waiting for the old heartbeat to age out."""
    database, repository = build_repository(tmp_path)
    repository.save_agent_pid("trailing_stop_agent", 4242)
    repository.save_agent_heartbeat(AgentHeartbeat("trailing_stop_agent", datetime.now(), "", datetime.now()))

    killed = {}

    class FakeResult:
        returncode = 0

    def fake_run(command, **kwargs):
        killed["command"] = command
        return FakeResult()

    monkeypatch.setattr("app.execution.agent_launcher.os.name", "nt")
    monkeypatch.setattr("app.execution.agent_launcher.subprocess.run", fake_run)

    stopped = stop_trailing_stop_agent(repository)

    assert stopped is True
    assert killed["command"] == ["taskkill", "/PID", "4242", "/F"]
    assert repository.load_agent_heartbeat("trailing_stop_agent") is None
    assert repository.get_agent_pid("trailing_stop_agent") is None
    database.close()


def test_stop_agent_is_a_no_op_when_no_pid_is_recorded(tmp_path):
    database, repository = build_repository(tmp_path)

    assert stop_trailing_stop_agent(repository) is False
    database.close()


def test_auto_start_preference_round_trips(tmp_path):
    database, repository = build_repository(tmp_path)

    assert repository.get_auto_start_trailing_agent("default") is False

    repository.set_auto_start_trailing_agent("default", True)
    assert repository.get_auto_start_trailing_agent("default") is True

    repository.set_auto_start_trailing_agent("default", False)
    assert repository.get_auto_start_trailing_agent("default") is False
    database.close()
