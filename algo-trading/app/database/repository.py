from __future__ import annotations

import json

from app.database.database import Database
from datetime import datetime
from zoneinfo import ZoneInfo

from app.database.models import ActivityRecord, AgentHeartbeat, DynamicWatchlistRecord, NotificationRecord, OrderRecord, PositionRecord, PreSpikeEventRecord, ProgressiveEmaCycleRecord, SignalEngineStatus, SignalRecord, StrategyPresetRecord, TradeRecord, WatchlistRecord

EXPORT_TIMEZONE = ZoneInfo("Asia/Kolkata")
EXPORT_TIMESTAMP_KEYS = {
    "created_at",
    "crossover_time",
    "cooldown_until",
    "ended_at",
    "exported_at",
    "latest_time",
    "last_valid_time",
    "signal_timestamp",
    "strong_signal_time",
    "timestamp",
    "trigger_time",
    "updated_at",
}


def _normalize_export_timestamps(value: object, key: str | None = None) -> object:
    if isinstance(value, dict):
        return {name: _normalize_export_timestamps(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_normalize_export_timestamps(item) for item in value]
    if key not in EXPORT_TIMESTAMP_KEYS or value is None:
        return value
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    else:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EXPORT_TIMEZONE)
    return parsed.astimezone(EXPORT_TIMEZONE).isoformat(timespec="milliseconds")


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def save_order(self, order: OrderRecord) -> None:
        with self.database.lock:
            self.database.connection.execute("INSERT INTO orders (broker_order_id, symbol, side, quantity, price, created_at) VALUES (?, ?, ?, ?, ?, ?)", (order.broker_order_id, order.symbol, order.side, order.quantity, order.price, order.created_at.isoformat()))
            self.database.connection.commit()

    def save_trade(self, trade: TradeRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO trades (
                    symbol, entry_time, exit_time, entry_price, exit_price, quantity, pnl,
                    side, position_type, strategy_name, exit_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.symbol, trade.entry_time.isoformat(), trade.exit_time.isoformat(),
                    trade.entry_price, trade.exit_price, trade.quantity, trade.pnl,
                    trade.side, trade.position_type, trade.strategy_name, trade.exit_reason,
                ),
            )
            self.database.connection.commit()

    def load_trades(self) -> list[TradeRecord]:
        with self.database.lock:
            rows = self.database.connection.execute("SELECT * FROM trades ORDER BY exit_time DESC").fetchall()
        return [
            TradeRecord(
                symbol=row["symbol"],
                entry_time=datetime.fromisoformat(row["entry_time"]),
                exit_time=datetime.fromisoformat(row["exit_time"]),
                entry_price=float(row["entry_price"]),
                exit_price=float(row["exit_price"]),
                quantity=int(row["quantity"]),
                pnl=float(row["pnl"]),
                side=row["side"] if row["side"] is not None else "BUY",
                position_type=row["position_type"] if row["position_type"] is not None else "INTRADAY",
                strategy_name=row["strategy_name"] if row["strategy_name"] is not None else "",
                exit_reason=row["exit_reason"] if row["exit_reason"] is not None else "",
            )
            for row in rows
        ]

    def save_activity(self, activity: ActivityRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "INSERT INTO activity (event_kind, symbol, timestamp, mode, price, order_id, side, quantity, entry_price, stop_loss, pnl, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    activity.event_kind,
                    activity.symbol,
                    activity.timestamp.isoformat(),
                    activity.mode,
                    activity.price,
                    activity.order_id,
                    activity.side,
                    activity.quantity,
                    activity.entry_price,
                    activity.stop_loss,
                    activity.pnl,
                    activity.reason,
                ),
            )
            self.database.connection.commit()

    def save_notification(self, notification: NotificationRecord) -> bool:
        signal_id = f"{notification.instrument_token or notification.symbol}:{notification.side}:{notification.signal_timestamp.isoformat()}"
        with self.database.lock:
            cursor = self.database.connection.execute(
                """
                INSERT OR IGNORE INTO notifications
                (user_id, signal_id, instrument_token, strategy, universe, sector, symbol, side, score, signal_timestamp, created_at, message, read)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    notification.user_id,
                    signal_id,
                    notification.instrument_token,
                    notification.strategy,
                    notification.universe,
                    notification.sector,
                    notification.symbol,
                    notification.side,
                    notification.score,
                    notification.signal_timestamp.isoformat(),
                    notification.created_at.isoformat(),
                    notification.message,
                    int(notification.read),
                ),
            )
            self.database.connection.commit()
        return cursor.rowcount == 1

    def load_notifications(self, unread_only: bool = False) -> list[NotificationRecord]:
        return self._load_notifications_for_user(None, unread_only)

    def _load_notifications_for_user(self, user_id: str | None, unread_only: bool) -> list[NotificationRecord]:
        query = "SELECT * FROM notifications"
        parameters: list[object] = []
        clauses = []
        if user_id is not None:
            clauses.append("user_id = ?")
            parameters.append(user_id)
        if unread_only:
            clauses.append("read = 0")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC, id DESC"
        with self.database.lock:
            rows = self.database.connection.execute(query, parameters).fetchall()
        return [
            NotificationRecord(
                strategy=row["strategy"],
                universe=row["universe"],
                sector=row["sector"],
                symbol=row["symbol"],
                side=row["side"],
                score=int(row["score"]),
                signal_timestamp=datetime.fromisoformat(row["signal_timestamp"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                message=row["message"],
                read=bool(row["read"]),
                user_id=row["user_id"],
                instrument_token=int(row["instrument_token"]) if row["instrument_token"] is not None else None,
            )
            for row in rows
        ]

    def save_signal(self, signal: SignalRecord) -> bool:
        with self.database.lock:
            cursor = self._insert_signal_locked(signal)
            self.database.connection.commit()
        return cursor.rowcount == 1

    @staticmethod
    def _signal_id(signal: SignalRecord) -> str:
        return signal.signal_key or str(signal.metadata.get("signal_key") or f"{signal.strategy}:{signal.instrument_token}:{signal.side}:{signal.signal_timestamp.isoformat()}")

    def _insert_signal_locked(self, signal: SignalRecord):
        return self.database.connection.execute(
            """
            INSERT OR IGNORE INTO signals
            (user_id, signal_id, instrument_token, symbol, side, signal_timestamp, price, vwap, ema20, stop_loss, reason,
             strategy, score, entry_price, target_1, target_2, metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal.user_id,
                self._signal_id(signal),
                signal.instrument_token,
                signal.symbol,
                signal.side,
                signal.signal_timestamp.isoformat(),
                signal.price,
                signal.vwap,
                signal.ema20,
                signal.stop_loss,
                signal.reason,
                signal.strategy,
                signal.score,
                signal.entry_price,
                signal.target_1,
                signal.target_2,
                json.dumps(signal.metadata, sort_keys=True),
                datetime.now().isoformat(),
            ),
        )

    def load_signals(self, user_id: str, limit: int = 100, strategy: str | None = None) -> list[SignalRecord]:
        with self.database.lock:
            query = "SELECT * FROM signals WHERE user_id = ?"
            parameters: list[object] = [user_id]
            if strategy is not None:
                query += " AND strategy = ?"
                parameters.append(strategy)
            query += " ORDER BY signal_timestamp DESC, id DESC LIMIT ?"
            parameters.append(limit)
            rows = self.database.connection.execute(query, parameters).fetchall()
        return [
            SignalRecord(
                user_id=row["user_id"],
                instrument_token=int(row["instrument_token"]),
                symbol=row["symbol"],
                side=row["side"],
                signal_timestamp=datetime.fromisoformat(row["signal_timestamp"]),
                price=float(row["price"]),
                vwap=float(row["vwap"]) if row["vwap"] is not None else None,
                ema20=float(row["ema20"]) if row["ema20"] is not None else None,
                stop_loss=float(row["stop_loss"]) if row["stop_loss"] is not None else None,
                reason=row["reason"],
                strategy=row["strategy"] if "strategy" in row.keys() else "",
                score=int(row["score"]) if "score" in row.keys() else 0,
                entry_price=float(row["entry_price"]) if "entry_price" in row.keys() and row["entry_price"] is not None else None,
                target_1=float(row["target_1"]) if "target_1" in row.keys() and row["target_1"] is not None else None,
                target_2=float(row["target_2"]) if "target_2" in row.keys() and row["target_2"] is not None else None,
                metadata=json.loads(row["metadata"] or "{}") if "metadata" in row.keys() else {},
                event_id=(json.loads(row["metadata"] or "{}").get("event_id", "") if "metadata" in row.keys() else ""),
                signal_key=row["signal_id"],
            )
            for row in rows
        ]

    def save_pre_spike_event(self, event: PreSpikeEventRecord) -> None:
        with self.database.lock:
            self._save_pre_spike_event_locked(event)
            self.database.connection.commit()

    def _save_pre_spike_event_locked(self, event: PreSpikeEventRecord) -> None:
        self.database.connection.execute(
            """
            INSERT INTO pre_spike_events
            (user_id, instrument_token, symbol, strategy, timeframe, trading_date, event_id, status,
             trigger_time, trigger_price, latest_time, latest_price, latest_score, highest_score,
             last_valid_time, entry_price, cooldown_until, ended_at, invalidation_streak, latest_metadata, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, event_id) DO UPDATE SET
                status=excluded.status, latest_time=excluded.latest_time, latest_price=excluded.latest_price,
                latest_score=excluded.latest_score, highest_score=excluded.highest_score,
                last_valid_time=excluded.last_valid_time, cooldown_until=excluded.cooldown_until,
                ended_at=excluded.ended_at, invalidation_streak=excluded.invalidation_streak,
                latest_metadata=excluded.latest_metadata, reason=excluded.reason
            """,
            (
                event.user_id,
                event.instrument_token,
                event.symbol,
                event.strategy,
                event.timeframe,
                event.trading_date,
                event.event_id,
                event.status,
                event.trigger_time.isoformat(),
                event.trigger_price,
                event.latest_time.isoformat(),
                event.latest_price,
                event.latest_score,
                event.highest_score,
                event.last_valid_time.isoformat() if event.last_valid_time else None,
                event.entry_price,
                event.cooldown_until.isoformat() if event.cooldown_until else None,
                event.ended_at.isoformat() if event.ended_at else None,
                event.invalidation_streak,
                json.dumps(event.latest_metadata, sort_keys=True),
                event.reason,
            ),
        )

    def save_pre_spike_event_and_signal(self, event: PreSpikeEventRecord, signal: SignalRecord) -> bool:
        with self.database.lock:
            self._save_pre_spike_event_locked(event)
            cursor = self._insert_signal_locked(signal)
            self.database.connection.commit()
        return cursor.rowcount == 1

    def next_pre_spike_event_sequence(self, user_id: str, strategy: str, symbol: str, timeframe: str, trading_date: str) -> int:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COUNT(*) AS event_count FROM pre_spike_events WHERE user_id = ? AND strategy = ? AND symbol = ? AND timeframe = ? AND trading_date = ?",
                (user_id, strategy, symbol, timeframe, trading_date),
            ).fetchone()
        return int(row["event_count"]) + 1

    def load_latest_pre_spike_event(self, user_id: str, strategy: str, symbol: str, timeframe: str) -> PreSpikeEventRecord | None:
        with self.database.lock:
            row = self.database.connection.execute(
                """
                SELECT * FROM pre_spike_events
                WHERE user_id = ? AND strategy = ? AND symbol = ? AND timeframe = ?
                ORDER BY latest_time DESC, id DESC LIMIT 1
                """,
                (user_id, strategy, symbol, timeframe),
            ).fetchone()
        return self._pre_spike_event_from_row(row) if row else None

    def load_pre_spike_events(self, user_id: str, strategy: str, active_only: bool = True, limit: int = 100) -> list[PreSpikeEventRecord]:
        query = "SELECT * FROM pre_spike_events WHERE user_id = ? AND strategy = ?"
        parameters: list[object] = [user_id, strategy]
        if active_only:
            query += " AND status = 'ACTIVE'"
        query += " ORDER BY latest_time DESC, id DESC LIMIT ?"
        parameters.append(limit)
        with self.database.lock:
            rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._pre_spike_event_from_row(row) for row in rows]

    @staticmethod
    def _pre_spike_event_from_row(row) -> PreSpikeEventRecord:
        return PreSpikeEventRecord(
            user_id=row["user_id"],
            instrument_token=int(row["instrument_token"]),
            symbol=row["symbol"],
            strategy=row["strategy"],
            timeframe=row["timeframe"],
            trading_date=row["trading_date"],
            event_id=row["event_id"],
            status=row["status"],
            trigger_time=datetime.fromisoformat(row["trigger_time"]),
            trigger_price=float(row["trigger_price"]),
            latest_time=datetime.fromisoformat(row["latest_time"]),
            latest_price=float(row["latest_price"]),
            latest_score=int(row["latest_score"]),
            highest_score=int(row["highest_score"]),
            last_valid_time=datetime.fromisoformat(row["last_valid_time"]) if row["last_valid_time"] else None,
            entry_price=float(row["entry_price"]),
            cooldown_until=datetime.fromisoformat(row["cooldown_until"]) if row["cooldown_until"] else None,
            ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
            invalidation_streak=int(row["invalidation_streak"]),
            latest_metadata=json.loads(row["latest_metadata"] or "{}"),
            reason=row["reason"],
        )

    def export_signal_state(self, user_id: str) -> dict[str, object]:
        with self.database.lock:
            signals = [dict(row) for row in self.database.connection.execute("SELECT * FROM signals WHERE user_id = ? ORDER BY signal_timestamp DESC, id DESC", (user_id,)).fetchall()]
            pre_spike_events = [dict(row) for row in self.database.connection.execute("SELECT * FROM pre_spike_events WHERE user_id = ? ORDER BY latest_time DESC, id DESC", (user_id,)).fetchall()]
            notifications = [dict(row) for row in self.database.connection.execute("SELECT * FROM notifications WHERE user_id = ? ORDER BY created_at DESC, id DESC", (user_id,)).fetchall()]
            progressive_cycles = [dict(row) for row in self.database.connection.execute("SELECT * FROM ema_progressive_cycles WHERE user_id = ? ORDER BY crossover_time DESC, id DESC", (user_id,)).fetchall()]
        return {
            "exported_at": datetime.now(EXPORT_TIMEZONE).isoformat(timespec="milliseconds"),
            "user_id": user_id,
            "signal_engine_response": _normalize_export_timestamps(self.load_progressive_signal_response(user_id)),
            "signals": _normalize_export_timestamps(signals),
            "pre_spike_events": _normalize_export_timestamps(pre_spike_events),
            "progressive_cycles": _normalize_export_timestamps(progressive_cycles),
            "notifications": _normalize_export_timestamps(notifications),
        }

    def clear_signal_state(self, user_id: str) -> dict[str, int]:
        with self.database.lock:
            counts = {}
            for table in ("signals", "pre_spike_events", "ema_progressive_cycles", "notifications"):
                cursor = self.database.connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                counts[table] = cursor.rowcount
            self.database.connection.commit()
        return counts

    def backup_and_clear_signal_state(self, user_id: str) -> tuple[dict[str, object], dict[str, int]]:
        with self.database.lock:
            backup = self.export_signal_state(user_id)
            counts = {}
            for table in ("signals", "pre_spike_events", "ema_progressive_cycles", "notifications"):
                cursor = self.database.connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
                counts[table] = cursor.rowcount
            self.database.connection.commit()
        backup["cleared_counts"] = counts
        return backup, counts

    def save_progressive_cycle(self, cycle: ProgressiveEmaCycleRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO ema_progressive_cycles
                (user_id, instrument_token, symbol, timeframe, cycle_id, bucket, signal_type,
                 crossover_time, crossover_price, strong_signal_time, strong_signal_price,
                 ema9, ema20, ema50, ema100, ema200, current_price, status, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, cycle_id) DO UPDATE SET
                    bucket=excluded.bucket, signal_type=excluded.signal_type,
                    strong_signal_time=excluded.strong_signal_time,
                    strong_signal_price=excluded.strong_signal_price,
                    ema9=excluded.ema9, ema20=excluded.ema20, ema50=excluded.ema50, ema100=excluded.ema100,
                    ema200=excluded.ema200, current_price=excluded.current_price,
                    status=excluded.status, updated_at=excluded.updated_at
                """,
                (
                    cycle.user_id,
                    cycle.instrument_token,
                    cycle.symbol,
                    cycle.timeframe,
                    cycle.cycle_id,
                    cycle.bucket,
                    cycle.signal_type,
                    cycle.crossover_time.isoformat(),
                    cycle.crossover_price,
                    cycle.strong_signal_time.isoformat() if cycle.strong_signal_time else None,
                    cycle.strong_signal_price,
                    cycle.ema9,
                    cycle.ema20,
                    cycle.ema50,
                    cycle.ema100,
                    cycle.ema200,
                    cycle.current_price,
                    cycle.status,
                    cycle.updated_at.isoformat(),
                ),
            )
            self.database.connection.commit()

    def load_latest_progressive_cycle(self, user_id: str, instrument_token: int, timeframe: str) -> ProgressiveEmaCycleRecord | None:
        with self.database.lock:
            row = self.database.connection.execute(
                """
                SELECT * FROM ema_progressive_cycles
                WHERE user_id = ? AND instrument_token = ? AND timeframe = ?
                ORDER BY crossover_time DESC, id DESC LIMIT 1
                """,
                (user_id, instrument_token, timeframe),
            ).fetchone()
        return self._progressive_cycle_from_row(row) if row else None

    def load_progressive_cycles(self, user_id: str, limit: int | None = None) -> list[ProgressiveEmaCycleRecord]:
        query = "SELECT * FROM ema_progressive_cycles WHERE user_id = ? ORDER BY crossover_time DESC, id DESC"
        parameters: list[object] = [user_id]
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self.database.lock:
            rows = self.database.connection.execute(query, parameters).fetchall()
        return [self._progressive_cycle_from_row(row) for row in rows]

    def load_progressive_signal_response(self, user_id: str) -> dict[str, object]:
        latest_by_symbol: dict[tuple[str, str], ProgressiveEmaCycleRecord] = {}
        for cycle in self.load_progressive_cycles(user_id):
            latest_by_symbol.setdefault((cycle.symbol, cycle.timeframe), cycle)

        def as_payload(cycle: ProgressiveEmaCycleRecord) -> dict[str, object]:
            common = {
                "symbol": cycle.symbol,
                "timeframe": cycle.timeframe,
                "bucket": cycle.bucket,
                "signal": cycle.signal_type,
                "ema9": cycle.ema9,
                "ema20": cycle.ema20,
                "ema50": cycle.ema50,
                "ema100": cycle.ema100,
                "ema200": cycle.ema200,
                "current_price": cycle.current_price,
                "status": cycle.status,
            }
            if cycle.bucket == "A":
                return {
                    **common,
                    "crossover_time": cycle.crossover_time.isoformat(),
                    "crossover_price": cycle.crossover_price,
                }
            return {
                **common,
                "initial_crossover_time": cycle.crossover_time.isoformat(),
                "initial_crossover_price": cycle.crossover_price,
                "strong_signal_time": cycle.strong_signal_time.isoformat() if cycle.strong_signal_time else None,
                "strong_signal_price": cycle.strong_signal_price,
            }

        cycles = list(latest_by_symbol.values())
        strong_cycles = [
            cycle
            for cycle in cycles
            if cycle.bucket == "B"
            and cycle.signal_type == "STRONG"
            and cycle.ema100 is not None
            and cycle.ema9 > cycle.ema20 > cycle.ema50 > cycle.ema100
        ]
        return {
            "strategy": "EMA_9_200_PROGRESSIVE",
            "bucket_a": [as_payload(cycle) for cycle in cycles if cycle.bucket == "A"],
            "bucket_b": [as_payload(cycle) for cycle in strong_cycles],
        }

    @staticmethod
    def _progressive_cycle_from_row(row) -> ProgressiveEmaCycleRecord:
        return ProgressiveEmaCycleRecord(
            user_id=row["user_id"],
            instrument_token=int(row["instrument_token"]),
            symbol=row["symbol"],
            timeframe=row["timeframe"],
            cycle_id=row["cycle_id"],
            bucket=row["bucket"],
            signal_type=row["signal_type"],
            crossover_time=datetime.fromisoformat(row["crossover_time"]),
            crossover_price=float(row["crossover_price"]),
            strong_signal_time=datetime.fromisoformat(row["strong_signal_time"]) if row["strong_signal_time"] else None,
            strong_signal_price=float(row["strong_signal_price"]) if row["strong_signal_price"] is not None else None,
            ema9=float(row["ema9"]),
            ema20=float(row["ema20"]),
            ema50=float(row["ema50"]),
            ema100=float(row["ema100"]) if row["ema100"] is not None else None,
            ema200=float(row["ema200"]),
            current_price=float(row["current_price"]),
            status=row["status"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def save_watchlist(self, watchlist: WatchlistRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO watchlists (user_id, name, symbols, selected, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id, name) DO UPDATE SET
                    symbols=excluded.symbols, selected=excluded.selected, updated_at=excluded.updated_at
                """,
                (watchlist.user_id, watchlist.name, json.dumps(watchlist.symbols, sort_keys=True), int(watchlist.selected), (watchlist.updated_at or datetime.now()).isoformat()),
            )
            self.database.connection.commit()

    def load_watchlists(self, user_id: str, selected_only: bool = False) -> list[WatchlistRecord]:
        query = "SELECT * FROM watchlists WHERE user_id = ?"
        parameters: list[object] = [user_id]
        if selected_only:
            query += " AND selected = 1"
        query += " ORDER BY name"
        with self.database.lock:
            rows = self.database.connection.execute(query, parameters).fetchall()
        return [
            WatchlistRecord(
                user_id=row["user_id"],
                name=row["name"],
                symbols={str(symbol): int(token) for symbol, token in json.loads(row["symbols"]).items()},
                selected=bool(row["selected"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def load_selected_watchlist_users(self) -> list[str]:
        with self.database.lock:
            rows = self.database.connection.execute("SELECT DISTINCT user_id FROM watchlists WHERE selected = 1").fetchall()
        return [row["user_id"] for row in rows]

    def set_watchlist_selection(self, user_id: str, name: str, selected: bool) -> None:
        with self.database.lock:
            self.database.connection.execute("UPDATE watchlists SET selected = ?, updated_at = ? WHERE user_id = ? AND name = ?", (int(selected), datetime.now().isoformat(), user_id, name))
            self.database.connection.commit()

    def update_watchlist_symbols(self, user_id: str, name: str, symbols: dict[str, int]) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE watchlists SET symbols = ?, updated_at = ? WHERE user_id = ? AND name = ?",
                (json.dumps(symbols, sort_keys=True), datetime.now().isoformat(), user_id, name),
            )
            self.database.connection.commit()

    def update_dynamic_watchlist_symbols(self, user_id: str, symbols: dict[str, int]) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE dynamic_watchlists SET symbols = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(symbols, sort_keys=True), datetime.now().isoformat(), user_id),
            )
            self.database.connection.commit()

    def rename_watchlist(self, user_id: str, old_name: str, new_name: str) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE watchlists SET name = ?, updated_at = ? WHERE user_id = ? AND name = ?",
                (new_name, datetime.now().isoformat(), user_id, old_name),
            )
            self.database.connection.commit()

    def delete_watchlist(self, user_id: str, name: str) -> None:
        with self.database.lock:
            self.database.connection.execute("DELETE FROM watchlists WHERE user_id = ? AND name = ?", (user_id, name))
            self.database.connection.commit()

    def save_dynamic_watchlist(self, watchlist: DynamicWatchlistRecord) -> None:
        updated_at = watchlist.updated_at or datetime.now()
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO dynamic_watchlists
                (user_id, source_name, symbols, selected, updated_at, refreshed_at, session_date, refresh_slot, require_breakout)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    source_name=excluded.source_name,
                    symbols=excluded.symbols,
                    selected=excluded.selected,
                    updated_at=excluded.updated_at,
                    refreshed_at=excluded.refreshed_at,
                    session_date=excluded.session_date,
                    refresh_slot=excluded.refresh_slot,
                    require_breakout=excluded.require_breakout
                """,
                (
                    watchlist.user_id,
                    watchlist.source_name,
                    json.dumps(watchlist.symbols, sort_keys=True),
                    int(watchlist.selected),
                    updated_at.isoformat(),
                    watchlist.refreshed_at.isoformat() if watchlist.refreshed_at else None,
                    watchlist.session_date,
                    watchlist.refresh_slot,
                    int(watchlist.require_breakout),
                ),
            )
            self.database.connection.commit()

    def load_dynamic_watchlist(self, user_id: str) -> DynamicWatchlistRecord | None:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT * FROM dynamic_watchlists WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return DynamicWatchlistRecord(
            user_id=row["user_id"],
            source_name=row["source_name"],
            symbols={str(symbol): int(token) for symbol, token in json.loads(row["symbols"]).items()},
            selected=bool(row["selected"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            refreshed_at=datetime.fromisoformat(row["refreshed_at"]) if row["refreshed_at"] else None,
            session_date=row["session_date"],
            refresh_slot=int(row["refresh_slot"]) if row["refresh_slot"] is not None else None,
            require_breakout=bool(row["require_breakout"]),
        )

    def set_dynamic_watchlist_selection(self, user_id: str, selected: bool) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE dynamic_watchlists SET selected = ?, updated_at = ? WHERE user_id = ?",
                (int(selected), datetime.now().isoformat(), user_id),
            )
            self.database.connection.commit()

    def set_dynamic_watchlist_breakout(self, user_id: str, require_breakout: bool) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE dynamic_watchlists SET require_breakout = ?, updated_at = ? WHERE user_id = ?",
                (int(require_breakout), datetime.now().isoformat(), user_id),
            )
            self.database.connection.commit()

    def save_signal_engine_status(self, status: SignalEngineStatus) -> None:
        heartbeat_at = status.heartbeat_at or status.last_run_at
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO signal_engine_status (user_id, last_run_at, last_error, heartbeat_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    last_run_at=excluded.last_run_at,
                    last_error=excluded.last_error,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (status.user_id, status.last_run_at.isoformat(), status.last_error, heartbeat_at.isoformat()),
            )
            self.database.connection.commit()

    def load_signal_engine_status(self, user_id: str) -> SignalEngineStatus | None:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT user_id, last_run_at, last_error, heartbeat_at FROM signal_engine_status WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        heartbeat_at = datetime.fromisoformat(row["heartbeat_at"]) if row["heartbeat_at"] else datetime.fromisoformat(row["last_run_at"])
        return SignalEngineStatus(row["user_id"], datetime.fromisoformat(row["last_run_at"]), row["last_error"], heartbeat_at)

    def save_agent_heartbeat(self, status: AgentHeartbeat) -> None:
        heartbeat_at = status.heartbeat_at or status.last_run_at
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO agent_heartbeat (engine_name, last_run_at, last_error, heartbeat_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(engine_name) DO UPDATE SET
                    last_run_at=excluded.last_run_at,
                    last_error=excluded.last_error,
                    heartbeat_at=excluded.heartbeat_at
                """,
                (status.engine_name, status.last_run_at.isoformat(), status.last_error, heartbeat_at.isoformat()),
            )
            self.database.connection.commit()

    def load_agent_heartbeat(self, engine_name: str) -> AgentHeartbeat | None:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT engine_name, last_run_at, last_error, heartbeat_at FROM agent_heartbeat WHERE engine_name = ?",
                (engine_name,),
            ).fetchone()
        if row is None:
            return None
        heartbeat_at = datetime.fromisoformat(row["heartbeat_at"]) if row["heartbeat_at"] else datetime.fromisoformat(row["last_run_at"])
        return AgentHeartbeat(row["engine_name"], datetime.fromisoformat(row["last_run_at"]), row["last_error"], heartbeat_at)

    def set_auto_start_trailing_agent(self, user_id: str, enabled: bool) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO agent_preferences (user_id, auto_start_trailing_agent)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET auto_start_trailing_agent=excluded.auto_start_trailing_agent
                """,
                (user_id, int(enabled)),
            )
            self.database.connection.commit()

    def get_auto_start_trailing_agent(self, user_id: str) -> bool:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT auto_start_trailing_agent FROM agent_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return bool(row["auto_start_trailing_agent"]) if row is not None else False

    def save_position(self, position: PositionRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
            INSERT INTO positions (
                symbol, side, quantity, entry_price, stop_loss, entry_time, target_1, target_2,
                protective_order_id, target_1_hit, instrument_token, position_type, atr_multiplier, strategy_name,
                trading_mode
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                side=excluded.side, quantity=excluded.quantity, entry_price=excluded.entry_price,
                stop_loss=excluded.stop_loss, entry_time=excluded.entry_time, target_1=excluded.target_1,
                target_2=excluded.target_2, protective_order_id=excluded.protective_order_id,
                target_1_hit=excluded.target_1_hit, instrument_token=excluded.instrument_token,
                position_type=excluded.position_type, atr_multiplier=excluded.atr_multiplier,
                strategy_name=excluded.strategy_name, trading_mode=excluded.trading_mode
                """,
                (
                    position.symbol, position.side, position.quantity, position.entry_price, position.stop_loss,
                    position.entry_time.isoformat(), position.target_1, position.target_2,
                    position.protective_order_id, int(position.target_1_hit), position.instrument_token,
                    position.position_type, position.atr_multiplier, position.strategy_name,
                    position.trading_mode,
                ),
            )
            self.database.connection.commit()

    def update_stop_price(self, symbol: str, stop_loss: float) -> None:
        with self.database.lock:
            self.database.connection.execute(
                "UPDATE positions SET stop_loss = ? WHERE symbol = ?",
                (stop_loss, symbol),
            )
            self.database.connection.commit()

    def delete_position(self, symbol: str) -> None:
        with self.database.lock:
            self.database.connection.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            self.database.connection.commit()

    def load_positions(self) -> list[PositionRecord]:
        with self.database.lock:
            rows = self.database.connection.execute("SELECT * FROM positions ORDER BY entry_time").fetchall()
        return [
            PositionRecord(
                symbol=row["symbol"],
                side=row["side"],
                quantity=int(row["quantity"]),
                entry_price=float(row["entry_price"]),
                stop_loss=float(row["stop_loss"]),
                entry_time=datetime.fromisoformat(row["entry_time"]),
                target_1=float(row["target_1"]) if row["target_1"] is not None else None,
                target_2=float(row["target_2"]) if row["target_2"] is not None else None,
                protective_order_id=row["protective_order_id"],
                target_1_hit=bool(row["target_1_hit"]),
                instrument_token=int(row["instrument_token"]) if row["instrument_token"] is not None else None,
                position_type=row["position_type"] if row["position_type"] is not None else "INTRADAY",
                atr_multiplier=float(row["atr_multiplier"]) if row["atr_multiplier"] is not None else None,
                strategy_name=row["strategy_name"] if row["strategy_name"] is not None else "",
                trading_mode=row["trading_mode"] if row["trading_mode"] is not None else "LIVE",
            )
            for row in rows
        ]

    def has_submitted_signal(self, symbol: str, side: str, timestamp: datetime) -> bool:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT 1 FROM activity WHERE event_kind = 'entry_submitted' AND symbol = ? AND side = ? AND timestamp = ? LIMIT 1",
                (symbol, side, timestamp.isoformat()),
            ).fetchone()
        return row is not None

    def load_daily_trade_stats(self, day: str) -> tuple[int, float]:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT COUNT(*) AS trades, COALESCE(SUM(pnl), 0) AS pnl FROM activity WHERE event_kind IN ('exit_submitted', 'broker_exit_detected') AND substr(timestamp, 1, 10) = ?",
                (day,),
            ).fetchone()
        return int(row["trades"]), float(row["pnl"])

    def save_strategy_preset(self, preset: StrategyPresetRecord) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO strategy_presets (name, strategy_type, parameters, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    strategy_type=excluded.strategy_type,
                    parameters=excluded.parameters,
                    updated_at=excluded.updated_at
                """,
                (preset.name, preset.strategy_type, json.dumps(preset.parameters, sort_keys=True), preset.created_at.isoformat(), preset.updated_at.isoformat()),
            )
            self.database.connection.commit()

    def load_strategy_presets(self) -> list[StrategyPresetRecord]:
        with self.database.lock:
            rows = self.database.connection.execute("SELECT * FROM strategy_presets ORDER BY name").fetchall()
        return [
            StrategyPresetRecord(
                name=row["name"],
                strategy_type=row["strategy_type"],
                parameters=json.loads(row["parameters"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                updated_at=datetime.fromisoformat(row["updated_at"]),
            )
            for row in rows
        ]

    def delete_strategy_preset(self, name: str) -> None:
        with self.database.lock:
            self.database.connection.execute("DELETE FROM strategy_presets WHERE name = ?", (name,))
            self.database.connection.commit()

    def save_dashboard_settings(self, user_id: str, settings: dict) -> None:
        with self.database.lock:
            self.database.connection.execute(
                """
                INSERT INTO dashboard_settings (user_id, settings, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    settings=excluded.settings,
                    updated_at=excluded.updated_at
                """,
                (user_id, json.dumps(settings, sort_keys=True), datetime.now().isoformat()),
            )
            self.database.connection.commit()

    def load_dashboard_settings(self, user_id: str) -> dict:
        with self.database.lock:
            row = self.database.connection.execute(
                "SELECT settings FROM dashboard_settings WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return {}
        try:
            settings = json.loads(row["settings"])
        except (TypeError, json.JSONDecodeError):
            return {}
        return settings if isinstance(settings, dict) else {}
