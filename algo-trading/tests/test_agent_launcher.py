import os
from datetime import datetime, timedelta

from app.database.database import Database
from app.database.models import AgentHeartbeat
from app.database.repository import Repository
from app.execution.agent_launcher import (
    REPO_ROOT,
    agent_heartbeat_is_fresh,
    launch_trailing_stop_agent,
    maybe_autostart_trailing_agent,
    stop_trailing_stop_agent,
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
        lambda *args: launched.append(args),
    )

    result = maybe_autostart_trailing_agent(repository, "key", "secret", "token")

    assert result is None
    assert launched == []
    database.close()


def test_autostart_launches_when_heartbeat_is_stale_or_missing(tmp_path, monkeypatch):
    database, repository = build_repository(tmp_path)
    launched = []
    monkeypatch.setattr(
        "app.execution.agent_launcher.launch_trailing_stop_agent",
        lambda api_key, api_secret, access_token, repository: launched.append((api_key, api_secret, access_token, repository)) or "process",
    )

    result = maybe_autostart_trailing_agent(repository, "key", "secret", "token")

    assert result == "process"
    assert launched == [("key", "secret", "token", repository)]
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
