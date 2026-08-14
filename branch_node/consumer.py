"""Wires masking + feature extraction + the local model together: consumes
this branch's Kafka topic, scores every transaction in real time, and writes
the score to Redis (the shared whiteboard - see CLAUDE.md's isolation rule).
Nothing here ever writes a raw account_number, customer_id, or customer_name
to Redis - only token_id and the engineered features.
"""
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta

import redis
from kafka import KafkaConsumer

from branch_node.graph_writer import GraphWriter
from branch_node.masking import mask_event
from branch_node.model import load_trained_model
from graph.schema import setup_schema
from shared.config import CTR_THRESHOLD_USD, KAFKA_BROKER, KAFKA_TOPICS, REDIS_URL
from shared.redis_keys import FLAGGED_ACCOUNTS, FRAUD_EVENTS_CHANNEL, score_key
from shared.schemas import ScoreRecord, TxnFeatures

BRANCH_ID = os.getenv("BRANCH_ID", "unknown")


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")


def _load_local_account_ages(branch_id: str) -> dict:
    """Each branch's own core-banking snapshot of its accounts' ages - looked
    up locally from data it already owns, never sent over Kafka. Any account
    not found here (e.g. a freshly-minted mule/placement account from the
    fraud scenario) defaults to age 0, which doubles as a genuine fraud
    signal rather than a lookup gap.
    """
    path = os.getenv("CUSTOMERS_FILE", "data/customers.json")
    ages = {}
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        for acct in data.get("accounts", []):
            if acct["branch_id"] == branch_id:
                ages[acct["account_number"]] = acct["account_age_days"]
    return ages


class VelocityTracker:
    """Trailing 10-minute transaction count per token, keyed on the
    fabricated business timestamp (transaction.timestamp), not wall-clock
    delivery time - the fraud pattern is reasoned about in business time,
    which is what timestamp compression preserves (see CLAUDE.md).
    """

    def __init__(self, window_minutes: int = 10):
        self.window = timedelta(minutes=window_minutes)
        self._history = defaultdict(deque)

    def bump(self, token_id: str, ts: datetime) -> int:
        hist = self._history[token_id]
        hist.append(ts)
        cutoff = ts - self.window
        while hist and hist[0] < cutoff:
            hist.popleft()
        return len(hist)


def extract_features(masked: dict, account_ages: dict, velocity: VelocityTracker) -> TxnFeatures:
    ts = _parse_ts(masked["timestamp"])
    return TxnFeatures(
        amount_ratio_to_threshold=round(masked["amount"] / CTR_THRESHOLD_USD, 4),
        is_cash=masked["txn_type"] in ("CASH_DEPOSIT", "CASH_WITHDRAWAL"),
        hour_of_day=ts.hour,
        day_of_week=ts.weekday(),
        velocity_10min=velocity.bump(masked["token_id"], ts),
        account_age_days=account_ages.get(masked["local_account_number"], 0),
        is_transfer_out=masked["is_transfer_out"],
    )


def run():
    print(f"[consumer:{BRANCH_ID}] loading trained logistic regression model...")
    scorer = load_trained_model()
    print(f"[consumer:{BRANCH_ID}] model loaded, decision threshold={scorer.threshold}")
    account_ages = _load_local_account_ages(BRANCH_ID)
    velocity = VelocityTracker()
    r = redis.from_url(REDIS_URL)
    graph = GraphWriter()
    # All 3 branches call this concurrently at startup - CREATE CONSTRAINT IF
    # NOT EXISTS is safe under that race. Without the constraint active
    # before any writes, concurrent MERGEs on sentinel nodes (CASH/CASH_OUT,
    # touched by every branch) can each decide the node doesn't exist yet and
    # create duplicates - the constraint is what makes MERGE atomic.
    setup_schema(graph.driver)

    topic = KAFKA_TOPICS[BRANCH_ID]
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=KAFKA_BROKER,
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        auto_offset_reset="latest",
        group_id=f"branch-{BRANCH_ID}-consumer",
    )
    print(f"[consumer:{BRANCH_ID}] listening on {topic} ...")

    for msg in consumer:
        event = msg.value
        masked = mask_event(event)
        # Every transaction becomes a graph edge, flagged or not - convergence
        # tracing needs the whole money trail, not just the flagged legs of it.
        graph.write_edge(masked)
        features = extract_features(masked, account_ages, velocity)
        s = scorer.score(features)

        record = ScoreRecord(
            branch_id=BRANCH_ID,
            token_id=masked["token_id"],
            txn_id=masked["txn_id"],
            score=s,
            features=features,
            timestamp=masked["timestamp"],
        )
        r.set(score_key(BRANCH_ID, masked["token_id"], masked["txn_id"]), record.model_dump_json())

        flagged = s >= scorer.threshold
        marker = " <-- FLAGGED" if flagged else ""
        print(
            f"[consumer:{BRANCH_ID}] token={masked['token_id']} "
            f"${masked['amount']:>10,.2f} {masked['txn_type']:<16} score={s:.3f}{marker}"
        )

        if flagged:
            r.sadd(FLAGGED_ACCOUNTS, masked["token_id"])
            r.publish(
                FRAUD_EVENTS_CHANNEL,
                json.dumps({"branch_id": BRANCH_ID, "token_id": masked["token_id"], "txn_id": masked["txn_id"], "score": s}),
            )


if __name__ == "__main__":
    run()
