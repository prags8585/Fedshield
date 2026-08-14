"""Session 4 exit criteria: before touching the LR model at all, measure how
much of the live demo's false-positive problem a plain graph filter clears
up on its own. Takes every transaction the model already flagged (score >=
its own tuned threshold, read from shared/lr_model.json - never the .env
FRAUD_THRESHOLD, see CLAUDE.md's "Session 3 Update"), runs check_convergence
seeded from the flagged CASH_DEPOSIT transactions, and keeps only the
flagged transactions whose account actually sits on a real convergence path
- dropping everything else. Ground truth is read here ONLY to report honest
before/after precision - never fed into the filter itself.

Run against a live demo already streamed end to end (see DEMO_RUNBOOK.md):
    python evaluation/downstream_filter_experiment.py
"""
import json
import os

import redis
from dotenv import load_dotenv
from neo4j import GraphDatabase

from graph.queries import check_convergence
from shared.schemas import ScoreRecord

load_dotenv()

REDIS_URL = "redis://localhost:6380"
NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")

CASH_SOURCE_TOKEN = "CASH"


def _load_threshold() -> float:
    with open("shared/lr_model.json") as f:
        return json.load(f)["threshold"]


def _load_flagged_records(r: redis.Redis, threshold: float) -> list:
    records = []
    for key in r.scan_iter("score:*"):
        record = ScoreRecord.model_validate_json(r.get(key))
        if record.score >= threshold:
            records.append(record)
    return records


def _deposit_edge(driver, token_id: str, txn_id: str) -> dict:
    # LIMIT 1: Neo4j Community has no relationship uniqueness constraint, so
    # a rare Kafka redelivery can leave two identical edges for one txn_id
    # (see graph/queries.py's _dedupe_by_txn_id) - either is equally valid here.
    query = """
        MATCH (:Account {token_id: $cash})-[r:TRANSACTED {txn_id: $txn_id}]->(b:Account {token_id: $token_id})
        RETURN r.amount AS amount, r.ts AS ts, r.branch_id AS branch_id
        LIMIT 1
    """
    with driver.session() as session:
        result = session.run(query, cash=CASH_SOURCE_TOKEN, txn_id=txn_id, token_id=token_id)
        record = result.single()
        return dict(record) if record else None


def run() -> None:
    threshold = _load_threshold()
    r = redis.from_url(REDIS_URL)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    flagged = _load_flagged_records(r, threshold)
    flagged_deposits = [f for f in flagged if f.features.is_cash and not f.features.is_transfer_out]

    sources = []
    for f in flagged_deposits:
        edge = _deposit_edge(driver, f.token_id, f.txn_id)
        if edge is not None:
            sources.append({"token_id": f.token_id, "amount": edge["amount"], "ts": edge["ts"], "branch_id": edge["branch_id"]})

    result = check_convergence(driver, sources)
    driver.close()

    kept_tokens = set()
    if result["has_convergence"]:
        for path in result["paths"]:
            kept_tokens.update(path)

    before_tokens = {f.token_id for f in flagged}
    after_tokens = before_tokens & kept_tokens

    ground_truth = json.load(open("data/layering_hops4_ground_truth.json"))
    from branch_node.masking import token_for
    fraud_tokens = {token_for(a) for a in ground_truth["fraud_accounts"]}

    before_tp = before_tokens & fraud_tokens
    before_fp = before_tokens - fraud_tokens
    after_tp = after_tokens & fraud_tokens
    after_fp = after_tokens - fraud_tokens

    print(f"Decision threshold: {threshold}")
    print(f"Flagged deposits used as convergence sources: {len(sources)} / {len(flagged_deposits)}")
    print(f"check_convergence: has_convergence={result['has_convergence']} "
          f"convergence_account={result['convergence_account']} "
          f"num_sources={result['num_sources']} num_branches={result['num_branches']} "
          f"amount_preservation_ratio={result['amount_preservation_ratio']}")
    print()
    print(f"BEFORE filter: {len(before_tokens)} flagged accounts "
          f"({len(before_tp)} true fraud, {len(before_fp)} false positives)")
    print(f"AFTER  filter: {len(after_tokens)} flagged accounts "
          f"({len(after_tp)} true fraud, {len(after_fp)} false positives)")
    print()
    dropped = len(before_fp) - len(after_fp)
    pct = (dropped / len(before_fp) * 100) if before_fp else 0.0
    print(f"False positives cleared by the graph filter alone: {dropped} / {len(before_fp)} ({pct:.1f}%)")
    print(f"Fraud accounts retained after filter: {len(after_tp)} / {len(before_tp)}")


if __name__ == "__main__":
    run()
