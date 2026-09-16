from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from app.database.database import Database
from app.database.models import DecisionLogRecord
from app.database.repository import Repository


def _record_to_dict(decision: DecisionLogRecord) -> dict:
    return {
        "timestamp": decision.timestamp.isoformat(),
        "symbol": decision.symbol,
        "event_type": decision.event_type,
        "strategy": decision.strategy_name,
        "mode": decision.mode,
        "decision": decision.decision,
        "rationale": decision.rationale,
        "inputs": decision.inputs,
        "outputs": decision.outputs,
        "confidence": decision.confidence,
        "correlation_id": decision.correlation_id,
        "outcome": decision.outcome,
    }


def _to_training_pair(decision: DecisionLogRecord) -> dict:
    """A prompt/completion shape suitable for supervised fine-tuning: the prompt is everything
    that was knowable at decision time (symbol, event, strategy, inputs); the completion is the
    decision actually made, its rationale, and -- when known -- what it led to. Training on the
    outcome text isn't "cheating": the point is a model that, given the same prompt shape in the
    future, both proposes a decision and has seen what similar ones actually returned.
    """
    context_lines = [f"Symbol: {decision.symbol}", f"Event: {decision.event_type}", f"Strategy: {decision.strategy_name or 'n/a'}"]
    if decision.inputs:
        context_lines.append(f"Inputs: {json.dumps(decision.inputs, sort_keys=True, default=str)}")
    prompt = "\n".join(context_lines) + "\nWhat decision should be made, and why?"
    completion_parts = [decision.decision, decision.rationale]
    if decision.outputs:
        completion_parts.append(f"Outputs: {json.dumps(decision.outputs, sort_keys=True, default=str)}")
    if decision.outcome:
        completion_parts.append(f"Actual outcome: {json.dumps(decision.outcome, sort_keys=True, default=str)}")
    return {"prompt": prompt, "completion": "\n".join(part for part in completion_parts if part)}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Export the unified decision log (every signal, entry, stop trail, protective-stop "
            "replacement, target hit, and exit this app has recorded) as JSONL -- for later LLM "
            "fine-tuning, retrieval-augmented generation, or offline strategy analysis. Read-only; "
            "safe to run any time against the live database."
        )
    )
    parser.add_argument("--output", default="data/processed/decision_log.jsonl", help="Path to write JSONL to")
    parser.add_argument("--symbol", default=None, help="Only export this symbol, e.g. NSE:HAPPYFORGE")
    parser.add_argument(
        "--event-type",
        default=None,
        help="Only export this event_type (signal_generated, stop_trailed, protective_stop_replaced, target_hit, exit)",
    )
    parser.add_argument("--since", default=None, help="Only export rows at/after this ISO timestamp, e.g. 2026-09-01")
    parser.add_argument("--limit", type=int, default=1_000_000, help="Maximum rows to export")
    parser.add_argument(
        "--format",
        choices=["raw", "training"],
        default="raw",
        help="'raw': one JSON object per decision, close to the database row -- best for RAG ingestion or ad-hoc analysis. "
        "'training': prompt/completion pairs -- best for supervised fine-tuning.",
    )
    args = parser.parse_args()

    database = Database()
    database.initialize()
    repository = Repository(database)
    since = datetime.fromisoformat(args.since) if args.since else None
    decisions = repository.load_decisions(symbol=args.symbol, event_type=args.event_type, since=since, limit=args.limit)
    database.close()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert = _to_training_pair if args.format == "training" else _record_to_dict
    # load_decisions orders most-recent-first for the dashboard's use case; a training/RAG
    # export reads more naturally oldest-first, so a trade's signal/trail/exit rows appear in
    # the order they actually happened.
    with open(output_path, "w", encoding="utf-8") as handle:
        for decision in reversed(decisions):
            handle.write(json.dumps(convert(decision), sort_keys=True, default=str))
            handle.write("\n")

    print(f"Wrote {len(decisions)} decision(s) to {output_path}")


if __name__ == "__main__":
    main()
