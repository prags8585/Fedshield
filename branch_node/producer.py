"""Streams generated event files onto each branch's Kafka topic in
fabricated-timeline order, delivered in compressed real time - the mechanism
CLAUDE.md requires so the demo is watchable without misrepresenting the
time-window logic the agents later reason over.

Scenario files (layering_scenario.py) already carry a precomputed
`_emit_offset_seconds` hint per event, mapping their `span_hours`-wide
fabricated timeline into `compress_seconds` of real delivery time. Files
without that hint (plain background noise) get a delivery offset computed
the same way, spread linearly across --background-window-seconds.
"""
import argparse
import json
import time
from datetime import datetime

from kafka import KafkaProducer

from shared.config import KAFKA_BROKER, KAFKA_TOPICS


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")


def _load_events(paths: list) -> list:
    events = []
    for p in paths:
        with open(p) as f:
            events.extend(json.load(f))
    return events


def _compute_schedule(events: list, background_window_seconds: float) -> list:
    timed = [(e, _parse_ts(e["transaction"]["timestamp"])) for e in events]
    all_ts = [ts for _, ts in timed]
    start_ts, end_ts = min(all_ts), max(all_ts)
    span = (end_ts - start_ts).total_seconds() or 1.0

    scheduled = []
    for event, ts in timed:
        if "_emit_offset_seconds" in event:
            offset = event.pop("_emit_offset_seconds")
        else:
            offset = ((ts - start_ts).total_seconds() / span) * background_window_seconds
        scheduled.append((offset, event))
    scheduled.sort(key=lambda pair: pair[0])
    return scheduled


def run(paths: list, background_window_seconds: float):
    events = _load_events(paths)
    schedule = _compute_schedule(events, background_window_seconds)

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BROKER,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )

    total_span = schedule[-1][0] if schedule else 0.0
    print(f"[producer] streaming {len(schedule)} events over ~{total_span:.0f}s (broker={KAFKA_BROKER}) ...")

    t0 = time.time()
    for offset, event in schedule:
        wait = offset - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)

        branch_id = event["branch_id"]
        topic = KAFKA_TOPICS[branch_id]
        producer.send(topic, value=event)

        txn = event["transaction"]
        print(f"[producer] -> {topic} txn_id={txn['txn_id']} type={txn['txn_type']:<16} amount=${txn['amount']:>10,.2f}")

    producer.flush()
    print("[producer] done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stream event JSON files to Kafka in fabricated-time order")
    parser.add_argument("--files", nargs="+", required=True, help="event JSON files to merge and stream (e.g. data/background.json data/layering_hops4_events.json)")
    parser.add_argument("--background-window-seconds", type=float, default=90.0, help="real-time delivery window for events with no compression hint")
    args = parser.parse_args()

    run(args.files, args.background_window_seconds)
