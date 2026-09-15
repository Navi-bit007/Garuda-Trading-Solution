from __future__ import annotations

import sqlite3
import time
from threading import RLock
from pathlib import Path


class Database:
    def __init__(self, path: str = "data/trading.sqlite3"):
        self.path = path
        self.thread_safe = True
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.connection = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")

    def initialize(self) -> None:
        with self.lock:
            self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_order_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            exit_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            pnl REAL NOT NULL,
            side TEXT NOT NULL DEFAULT 'BUY',
            position_type TEXT NOT NULL DEFAULT 'INTRADAY',
            strategy_name TEXT NOT NULL DEFAULT '',
            exit_reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS activity (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_kind TEXT NOT NULL,
            symbol TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            mode TEXT NOT NULL,
            price REAL,
            order_id TEXT,
            side TEXT,
            quantity INTEGER,
            entry_price REAL,
            stop_loss REAL,
            pnl REAL,
            reason TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL DEFAULT 'default',
            signal_id TEXT NOT NULL DEFAULT '',
            instrument_token INTEGER,
            strategy TEXT NOT NULL,
            universe TEXT NOT NULL,
            sector TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            score INTEGER NOT NULL,
            signal_timestamp TEXT NOT NULL,
            created_at TEXT NOT NULL,
            message TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, signal_id)
        );
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            signal_id TEXT NOT NULL,
            instrument_token INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY', 'SELL')),
            signal_timestamp TEXT NOT NULL,
            price REAL NOT NULL,
            vwap REAL,
            ema20 REAL,
            stop_loss REAL,
            reason TEXT NOT NULL DEFAULT '',
            strategy TEXT NOT NULL DEFAULT '',
            score INTEGER NOT NULL DEFAULT 0,
            entry_price REAL,
            target_1 REAL,
            target_2 REAL,
            metadata TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS pre_spike_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            instrument_token INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            strategy TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            trading_date TEXT NOT NULL,
            event_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('TRIGGERED', 'ACTIVE', 'ENDED', 'COOLDOWN')),
            trigger_time TEXT NOT NULL,
            trigger_price REAL NOT NULL,
            latest_time TEXT NOT NULL,
            latest_price REAL NOT NULL,
            latest_score INTEGER NOT NULL,
            highest_score INTEGER NOT NULL,
            last_valid_time TEXT,
            entry_price REAL NOT NULL,
            cooldown_until TEXT,
            ended_at TEXT,
            invalidation_streak INTEGER NOT NULL DEFAULT 0,
            latest_metadata TEXT NOT NULL DEFAULT '{}',
            reason TEXT NOT NULL DEFAULT '',
            UNIQUE(user_id, event_id)
        );
        CREATE TABLE IF NOT EXISTS ema_progressive_cycles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            instrument_token INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            cycle_id TEXT NOT NULL,
            bucket TEXT NOT NULL CHECK(bucket IN ('A', 'B')),
            signal_type TEXT NOT NULL CHECK(signal_type IN ('LIGHT', 'STRONG')),
            crossover_time TEXT NOT NULL,
            crossover_price REAL NOT NULL,
            strong_signal_time TEXT,
            strong_signal_price REAL,
            ema9 REAL NOT NULL,
            ema20 REAL NOT NULL,
            ema50 REAL NOT NULL,
            ema100 REAL,
            ema200 REAL NOT NULL,
            current_price REAL NOT NULL,
            status TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, cycle_id)
        );
        CREATE TABLE IF NOT EXISTS watchlists (
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            symbols TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(user_id, name)
        );
        CREATE TABLE IF NOT EXISTS dynamic_watchlists (
            user_id TEXT PRIMARY KEY,
            source_name TEXT NOT NULL,
            symbols TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            refreshed_at TEXT,
            session_date TEXT NOT NULL DEFAULT '',
            refresh_slot INTEGER
        );
        CREATE TABLE IF NOT EXISTS signal_engine_status (
            user_id TEXT PRIMARY KEY,
            last_run_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS positions (
            symbol TEXT PRIMARY KEY,
            side TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            stop_loss REAL NOT NULL,
            entry_time TEXT NOT NULL,
            target_1 REAL,
            target_2 REAL,
            protective_order_id TEXT,
            target_1_hit INTEGER NOT NULL DEFAULT 0,
            instrument_token INTEGER,
            position_type TEXT NOT NULL DEFAULT 'INTRADAY',
            atr_multiplier REAL,
            strategy_name TEXT NOT NULL DEFAULT '',
            trading_mode TEXT NOT NULL DEFAULT 'LIVE'
        );
        CREATE TABLE IF NOT EXISTS agent_heartbeat (
            engine_name TEXT PRIMARY KEY,
            last_run_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT '',
            heartbeat_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_preferences (
            user_id TEXT PRIMARY KEY,
            auto_start_trailing_agent INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS strategy_presets (
            name TEXT PRIMARY KEY,
            strategy_type TEXT NOT NULL,
            parameters TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dashboard_settings (
            user_id TEXT PRIMARY KEY,
            settings TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kite_session (
            user_id TEXT PRIMARY KEY,
            access_token TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
            self._run_migration_with_retry(self._migrate_notifications)
            self._migrate_signals()
            self._migrate_signal_engine_status()
            self._migrate_progressive_cycles()
            self._migrate_dynamic_watchlists()
            self._migrate_positions()
            self._migrate_trades()
            self._migrate_agent_heartbeat()
            self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_user_signal ON signals(user_id, signal_id)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS ix_pre_spike_events_lookup ON pre_spike_events(user_id, strategy, symbol, timeframe, trading_date, latest_time)")
            self.connection.commit()

    def _run_migration_with_retry(self, migration, attempts: int = 5) -> None:
        for attempt in range(attempts):
            try:
                migration()
                return
            except sqlite3.OperationalError as error:
                if "database is locked" not in str(error).lower() or attempt == attempts - 1:
                    raise
                self.connection.rollback()
                time.sleep(0.25 * (attempt + 1))

    def _migrate_progressive_cycles(self) -> None:
        columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(ema_progressive_cycles)").fetchall()
        }
        if "ema100" not in columns:
            self.connection.execute("ALTER TABLE ema_progressive_cycles ADD COLUMN ema100 REAL")

    def _migrate_dynamic_watchlists(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(dynamic_watchlists)").fetchall()}
        if "require_breakout" not in columns:
            self.connection.execute("ALTER TABLE dynamic_watchlists ADD COLUMN require_breakout INTEGER NOT NULL DEFAULT 0")

    def _migrate_positions(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(positions)").fetchall()}
        if "target_1_hit" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN target_1_hit INTEGER NOT NULL DEFAULT 0")
        if "instrument_token" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN instrument_token INTEGER")
        if "position_type" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN position_type TEXT NOT NULL DEFAULT 'INTRADAY'")
        if "atr_multiplier" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN atr_multiplier REAL")
        if "strategy_name" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN strategy_name TEXT NOT NULL DEFAULT ''")
        if "trading_mode" not in columns:
            self.connection.execute("ALTER TABLE positions ADD COLUMN trading_mode TEXT NOT NULL DEFAULT 'LIVE'")

    def _migrate_agent_heartbeat(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(agent_heartbeat)").fetchall()}
        if "pid" not in columns:
            self.connection.execute("ALTER TABLE agent_heartbeat ADD COLUMN pid INTEGER")

    def _migrate_trades(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(trades)").fetchall()}
        if "side" not in columns:
            self.connection.execute("ALTER TABLE trades ADD COLUMN side TEXT NOT NULL DEFAULT 'BUY'")
        if "position_type" not in columns:
            self.connection.execute("ALTER TABLE trades ADD COLUMN position_type TEXT NOT NULL DEFAULT 'INTRADAY'")
        if "strategy_name" not in columns:
            self.connection.execute("ALTER TABLE trades ADD COLUMN strategy_name TEXT NOT NULL DEFAULT ''")
        if "exit_reason" not in columns:
            self.connection.execute("ALTER TABLE trades ADD COLUMN exit_reason TEXT NOT NULL DEFAULT ''")

    def _migrate_signals(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(signals)").fetchall()}
        additions = {
            "strategy": "TEXT NOT NULL DEFAULT ''",
            "score": "INTEGER NOT NULL DEFAULT 0",
            "entry_price": "REAL",
            "target_1": "REAL",
            "target_2": "REAL",
            "metadata": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in columns:
                self.connection.execute(f"ALTER TABLE signals ADD COLUMN {name} {definition}")

    def _migrate_notifications(self) -> None:
        table = self.connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'notifications'").fetchone()
        if table and "UNIQUE(strategy" in (table["sql"] or ""):
            self.connection.execute("ALTER TABLE notifications RENAME TO notifications_legacy")
            self.connection.executescript(
                """
                CREATE TABLE notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL DEFAULT 'default',
                    signal_id TEXT NOT NULL,
                    instrument_token INTEGER,
                    strategy TEXT NOT NULL,
                    universe TEXT NOT NULL,
                    sector TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    signal_timestamp TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    read INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(user_id, signal_id)
                );
                INSERT OR IGNORE INTO notifications
                (user_id, signal_id, strategy, universe, sector, symbol, side, score, signal_timestamp, created_at, message, read)
                SELECT 'default', symbol || ':' || side || ':' || signal_timestamp, strategy, universe, sector, symbol, side, score, signal_timestamp, created_at, message, read
                FROM notifications_legacy;
                DROP TABLE notifications_legacy;
                """
            )
            return
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(notifications)").fetchall()}
        if "user_id" not in columns:
            self.connection.execute("ALTER TABLE notifications ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
        if "signal_id" not in columns:
            self.connection.execute("ALTER TABLE notifications ADD COLUMN signal_id TEXT NOT NULL DEFAULT ''")
        if "instrument_token" not in columns:
            self.connection.execute("ALTER TABLE notifications ADD COLUMN instrument_token INTEGER")
        self.connection.execute(
            "UPDATE notifications SET signal_id = COALESCE(instrument_token, symbol) || ':' || side || ':' || signal_timestamp WHERE signal_id = ''"
        )
        self.connection.execute(
            "DELETE FROM notifications WHERE id NOT IN (SELECT MIN(id) FROM notifications GROUP BY user_id, signal_id)"
        )
        self.connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_notifications_signal_id ON notifications(user_id, signal_id)")

    def _migrate_signal_engine_status(self) -> None:
        columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(signal_engine_status)").fetchall()}
        if "heartbeat_at" not in columns:
            self.connection.execute("ALTER TABLE signal_engine_status ADD COLUMN heartbeat_at TEXT")
            self.connection.execute("UPDATE signal_engine_status SET heartbeat_at = last_run_at WHERE heartbeat_at IS NULL")

    def close(self) -> None:
        with self.lock:
            self.connection.close()
