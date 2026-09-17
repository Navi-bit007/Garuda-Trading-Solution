from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from datetime import time as time_of_day
from pathlib import Path
from typing import Any

from app.database.models import AgentHeartbeat

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_SCRIPT_PATH = REPO_ROOT / "scripts" / "run_trailing_stop_agent.py"
AGENT_LOG_PATH = REPO_ROOT / "data" / "trailing_stop_agent.log"
HEARTBEAT_STALE_SECONDS = 90.0
STARTUP_GRACE_SECONDS = 1.5
LOG_TAIL_LINES = 25
TRAILING_STOP_AGENT_ENGINE_NAME = "trailing_stop_agent"


def trailing_agent_env_from_settings(settings) -> dict[str, str]:
    """Environment overrides mirroring the trailing-stop agent's own settings knobs (force exit
    time, ATR multipliers, minimum stop-improvement threshold), so a freshly (re)launched agent
    picks up whatever a user currently has configured -- including anything only ever persisted
    to the per-user `dashboard_settings` DB row, which the standalone agent's own `get_settings()`
    call never reads (that only sees `.env`/process environment). Falls back to the same
    defaults `TrailingStopAgent.__init__` itself uses if an attribute is missing, so passing an
    incomplete settings-like object never raises.
    """
    return {
        "FORCE_EXIT": getattr(settings, "force_exit", time_of_day(15, 15)).isoformat(),
        "AGENT_SHUTDOWN_TIME": getattr(settings, "agent_shutdown_time", time_of_day(15, 40)).isoformat(),
        "TRAILING_ATR_MULTIPLIER": str(getattr(settings, "trailing_atr_multiplier", 1.5)),
        "SWING_TRAILING_ATR_MULTIPLIER": str(getattr(settings, "swing_trailing_atr_multiplier", 2.0)),
        "MIN_STOP_IMPROVEMENT_PCT": str(getattr(settings, "min_stop_improvement_pct", 0.25)),
    }


def agent_heartbeat_is_fresh(repository, now: datetime | None = None) -> bool:
    """True if the standalone trailing-stop agent reported a heartbeat recently enough
    that it's reasonable to assume it's still running and there's no need to spawn another."""
    heartbeat = repository.load_agent_heartbeat(TRAILING_STOP_AGENT_ENGINE_NAME)
    if heartbeat is None:
        return False
    current_time = now or datetime.now()
    age_seconds = (current_time - heartbeat.heartbeat_at).total_seconds()
    return age_seconds <= HEARTBEAT_STALE_SECONDS


def _tail_log(lines: int = LOG_TAIL_LINES) -> str:
    if not AGENT_LOG_PATH.exists():
        return ""
    with open(AGENT_LOG_PATH, "r", encoding="utf-8", errors="replace") as handle:
        return "".join(handle.readlines()[-lines:]).strip()


def _record_startup_failure_if_any(repository, process: subprocess.Popen) -> None:
    """Detect a crash in the first moment after launch (missing dependency, bad credentials,
    etc.) and write it to the shared heartbeat row.

    Without this, a crash that happens before the agent's first cycle leaves no heartbeat at
    all, and the dashboard has no way to distinguish "failed to start" from "never started".
    """
    time.sleep(STARTUP_GRACE_SECONDS)
    if process.poll() is None:
        return
    now = datetime.now()
    error_message = _tail_log() or f"Agent process exited immediately with code {process.returncode}."
    repository.save_agent_heartbeat(AgentHeartbeat(TRAILING_STOP_AGENT_ENGINE_NAME, now, error_message, now))


def launch_trailing_stop_agent(
    api_key: str, api_secret: str, access_token: str, repository=None, extra_env: dict[str, str] | None = None
) -> subprocess.Popen:
    """Start scripts/run_trailing_stop_agent.py as a fully detached background process.

    Credentials are passed via the child's environment only -- nothing is written to .env --
    so a freshly generated access token can be handed off without any manual file editing.
    The process is detached (on Windows: DETACHED_PROCESS + its own process group; on POSIX: a
    new session) so it keeps running even if the Streamlit server/dashboard is closed, matching
    this agent's standalone design.

    The repo root is put on PYTHONPATH -- running a script directly (rather than via `-m`) only
    ever puts the script's own folder on sys.path, so without this the child process cannot
    `import app` regardless of its working directory.

    `extra_env` carries the dashboard's current effective risk settings (force exit time,
    trailing ATR multiplier, minimum stop-improvement threshold, ...) as environment variable
    overrides -- confirmed live: the standalone agent otherwise only ever reads `.env`/process
    environment via `get_settings()`, with no idea that a user changed something in Risk &
    Settings, since those changes are only ever persisted to the per-user `dashboard_settings`
    DB row, which this subprocess never reads. Without this, restarting the agent after changing
    a risk setting silently keeps using the old `.env` value.

    If `repository` is given, a short startup check catches a crash in the first
    STARTUP_GRACE_SECONDS (e.g. the import failure above, or bad credentials) and records it as
    an error heartbeat so the dashboard can surface it immediately.
    """
    AGENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(AGENT_LOG_PATH, "a", buffering=1)
    env = {
        **os.environ,
        "KITE_API_KEY": api_key,
        "KITE_API_SECRET": api_secret,
        "KITE_ACCESS_TOKEN": access_token,
        "TRADING_MODE": "LIVE",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH", "")])),
        **(extra_env or {}),
    }
    popen_kwargs: dict[str, Any] = dict(
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, str(AGENT_SCRIPT_PATH)], **popen_kwargs)
    if repository is not None:
        repository.save_agent_pid(TRAILING_STOP_AGENT_ENGINE_NAME, process.pid)
        _record_startup_failure_if_any(repository, process)
    return process


def maybe_autostart_trailing_agent(
    repository, api_key: str, api_secret: str, access_token: str, extra_env: dict[str, str] | None = None
) -> subprocess.Popen | None:
    """Launch the agent unless a recent heartbeat shows one is already running.

    Safe to call on every login/token-refresh -- the heartbeat check makes it idempotent, so it
    never spawns a duplicate agent alongside one that's already active.
    """
    if agent_heartbeat_is_fresh(repository):
        return None
    return launch_trailing_stop_agent(api_key, api_secret, access_token, repository, extra_env=extra_env)


def stop_trailing_stop_agent(repository) -> bool:
    """Stop the standalone trailing-stop agent process from the dashboard, so a code/config
    change doesn't require hunting the process down in Task Manager or a terminal.

    The agent is intentionally launched detached (see `launch_trailing_stop_agent`) precisely so
    it survives the dashboard closing -- which also means the dashboard has no live handle to it
    across reruns/sessions, so its PID (recorded at launch, see `save_agent_pid`) is the only way
    back to it. Returns False if there's no recorded PID or it's already gone.
    """
    pid = repository.get_agent_pid(TRAILING_STOP_AGENT_ENGINE_NAME)
    if pid is None:
        return False
    stopped = False
    if os.name == "nt":
        result = subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True)
        stopped = result.returncode == 0
    else:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except ProcessLookupError:
            stopped = False
    repository.clear_agent_heartbeat(TRAILING_STOP_AGENT_ENGINE_NAME)
    return stopped
