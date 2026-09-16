from __future__ import annotations

import argparse
import json
from datetime import datetime

from app.database.database import Database
from app.database.models import DecisionLogRecord
from app.database.repository import Repository

ENTRY_EVENTS = {"entry_submitted", "swing_entry_submitted"}
REJECTED_EVENTS = {"entry_rejected", "entry_skipped", "swing_entry_rejected", "swing_entry_skipped"}
EXIT_EVENTS = {"exit_submitted", "broker_exit_detected"}
TRAIL_EVENTS = {"stop_trailed"}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "One-time import of the app's existing activity ledger into decision_log, so "
            "history recorded before the decision-log wiring existed (entries, rejections, "
            "stop trails, exits) shows up in the LLM-facing decision log too, not just "
            "whatever gets logged from here on. Safe to re-run -- rows already imported "
            "(tracked by source_table='activity' + source_id=activity.id) are skipped."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Print what would be inserted without writing to the database")
    args = parser.parse_args()

    database = Database()
    database.initialize()
    repository = Repository(database)

    existing_source_ids = {
        row["source_id"]
        for row in database.connection.execute(
            "SELECT source_id FROM decision_log WHERE source_table = 'activity'"
        ).fetchall()
    }

    rows = database.connection.execute("SELECT * FROM activity ORDER BY id").fetchall()

    # Threads a stop-trail/exit row back to the entry that opened its position, by remembering
    # each symbol's most recent entry timestamp seen so far (in the same id order the events
    # actually happened in) -- entry_time isn't recorded directly on those later rows.
    open_entry_timestamp: dict[str, str] = {}

    inserted = 0
    for row in rows:
        source_id = str(row["id"])
        if source_id in existing_source_ids:
            continue
        event_kind = row["event_kind"]
        symbol = row["symbol"]
        timestamp = row["timestamp"]

        if event_kind in ENTRY_EVENTS:
            open_entry_timestamp[symbol] = timestamp
            correlation_id = f"{symbol}:{timestamp}"
            event_type, decision = "entry_placed", row["side"] or "BUY"
        elif event_kind in REJECTED_EVENTS:
            correlation_id = f"{symbol}:{timestamp}"
            event_type, decision = "entry_rejected", "SKIP"
        elif event_kind in TRAIL_EVENTS:
            correlation_id = f"{symbol}:{open_entry_timestamp.get(symbol, timestamp)}"
            event_type, decision = "stop_trailed", "TRAIL_STOP"
        elif event_kind in EXIT_EVENTS:
            correlation_id = f"{symbol}:{open_entry_timestamp.pop(symbol, timestamp)}"
            event_type, decision = "exit", "EXIT"
        else:
            continue

        record = DecisionLogRecord(
            timestamp=datetime.fromisoformat(timestamp),
            symbol=symbol,
            event_type=event_type,
            mode=row["mode"] or "LIVE",
            decision=decision,
            rationale=row["reason"] or "",
            inputs={
                "original_event_kind": event_kind,
                **{key: row[key] for key in ("price", "quantity", "entry_price") if row[key] is not None},
            },
            outputs={key: row[key] for key in ("stop_loss", "pnl") if row[key] is not None},
            correlation_id=correlation_id,
            source_table="activity",
            source_id=source_id,
        )
        if args.dry_run:
            print(repr(record).encode("ascii", errors="backslashreplace").decode("ascii"))
        else:
            repository.save_decision(record)
        inserted += 1

    linked = 0
    if not args.dry_run:
        # Backfill outcome onto every earlier decision (entry, each stop trail) that shares a
        # now-closed trade's correlation_id, mirroring what the live agent does going forward --
        # otherwise history imported here would permanently read as "outcome unknown".
        exit_rows = database.connection.execute(
            "SELECT correlation_id, outputs FROM decision_log WHERE event_type = 'exit' AND source_table = 'activity'"
        ).fetchall()
        for exit_row in exit_rows:
            outcome = json.loads(exit_row["outputs"] or "{}")
            if outcome:
                linked += repository.link_decision_outcome(exit_row["correlation_id"], outcome)

    database.close()
    print(f"{'Would insert' if args.dry_run else 'Inserted'} {inserted} decision(s) from {len(rows)} activity row(s).")
    if not args.dry_run:
        print(f"Linked outcome onto {linked} earlier decision(s) for now-closed trades.")


if __name__ == "__main__":
    main()
