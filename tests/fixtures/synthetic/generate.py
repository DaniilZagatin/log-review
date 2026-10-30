"""Generate synthetic telemetry/oracle from a controlled scenario table only.

Trusted test infrastructure, separate from the analyzed app and agent package.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def generate(table):
    if table.get("data_origin") != "synthetic" or table.get("population_complete") is not True:
        raise ValueError("The fixture requires a complete controlled synthetic population")
    activities = {item["id"]: item for item in table["required_activities"]}
    records, cases = [], []
    case_ids = set()
    for case in table["cases"]:
        case_id = case["case_id"]
        if case_id in case_ids:
            raise ValueError("Duplicate case identity")
        case_ids.add(case_id)
        if type(case.get("cancellation_requested")) is not bool:
            raise ValueError("Controlled cancellation input must be explicit")
        start = datetime.fromisoformat(case["started_at"])
        if start.tzinfo is None or start.utcoffset() != timedelta(0):
            raise ValueError("Fixture event times must be UTC")
        occurrence_ids, attempt_ids, logical = set(), set(), []
        returned, retried = False, []
        activity_counts = Counter()
        for operation in case["operations"]:
            activity = operation["activity"]
            definition = activities[activity]
            occurrence_id = f"{case_id}:{operation['occurrence']}"
            if occurrence_id in occurrence_ids:
                raise ValueError("Duplicate business operation occurrence")
            occurrence_ids.add(occurrence_id)
            activity_counts[activity] += 1
            if len(operation["attempts"]) > 1:
                retried.append(occurrence_id)
            for attempt in operation["attempts"]:
                number = attempt["attempt"]
                if type(number) is not int or number < 1:
                    raise ValueError("Attempt must be a positive integer")
                attempt_id = f"{occurrence_id}:a{number}"
                if attempt_id in attempt_ids:
                    raise ValueError("Duplicate technical attempt identity")
                attempt_ids.add(attempt_id)
                for event in attempt["events"]:
                    key, offset = event["key"], event["offset_seconds"]
                    if key not in definition["event_keys"]:
                        raise ValueError("Event does not belong to its declared activity")
                    if type(offset) is not int or offset < 0:
                        raise ValueError("Event offset must be a nonnegative integer")
                    deliveries = event.get("deliveries", 1)
                    if type(deliveries) is not int or deliveries < 1:
                        raise ValueError("Delivery count must be a positive integer")
                    event_time = start + timedelta(seconds=offset)
                    event_id = f"{case_id}:e{len(logical) + 1:02d}"
                    logical.append((key, offset))
                    returned = returned or key == "review.returned"
                    for delivery in range(1, deliveries + 1):
                        records.append({
                            "data_origin": "synthetic", "case_id": case_id,
                            "occurrence_id": occurrence_id,
                            "attempt_id": attempt_id, "event_id": event_id,
                            "delivery_id": f"{event_id}:d{delivery}", "event_key": key,
                            "occurred_at": utc_text(event_time),
                            "received_at": utc_text(event_time + timedelta(seconds=2 if delivery == 1 else 15)),
                        })
        terminals = [offset for key, offset in logical if key == "fulfil.completed"]
        if case["outcome"] != "completed" or len(terminals) != 1:
            raise ValueError("This initial fixture supports one completion per case")
        cases.append({
            "case_id": case_id, "outcome": case["outcome"],
            "cancellation_requested": case["cancellation_requested"],
            "operation_occurrences": len(occurrence_ids), "technical_attempts": len(attempt_ids),
            "logical_events": len(logical), "duration_seconds": terminals[0],
            "returned_for_revision": returned, "retried_occurrences": retried,
            "activity_occurrences": dict(sorted(activity_counts.items())),
        })
    cancellation_count = sum(case["cancellation_requested"] for case in cases)
    oracle = {
        "schema_version": "synthetic-oracle/1", "data_origin": "synthetic",
        "oracle_for_tests_only": True, "basis": "complete_controlled_scenario_table",
        "required_activities": table["required_activities"], "cases": cases,
        "totals": {
            "cases": len(cases),
            "operation_occurrences": sum(c["operation_occurrences"] for c in cases),
            "technical_attempts": sum(c["technical_attempts"] for c in cases),
            "logical_events": sum(c["logical_events"] for c in cases),
            "delivered_records": len(records),
        },
        "cancellation_opportunities": {
            "state": "known_zero" if cancellation_count == 0 else "known_n",
            "count": cancellation_count,
            "basis": "Explicit cancellation inputs in the independently specified complete controlled population",
        },
    }
    return records, oracle


def write_artifacts(output_dir):
    table = json.loads((ROOT / "scenarios.json").read_text(encoding="utf-8"))
    records, oracle = generate(table)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "logs.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True, allow_nan=False) + "\n" for row in records),
        encoding="utf-8", newline="\n",
    )
    (output_dir / "ground_truth.json").write_text(
        json.dumps(oracle, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8", newline="\n",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    write_artifacts(parser.parse_args().output_dir)
