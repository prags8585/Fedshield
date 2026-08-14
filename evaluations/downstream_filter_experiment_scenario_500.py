"""The scenario_500 (3-ring) counterpart to downstream_filter_experiment.py.

check_convergence only ever reports its single BEST convergence per call, so
feeding it every flagged deposit across all 3 independent rings at once would
solve one ring and silently drop the other two's real fraud as if it were
false positives - not because they're not real, but because the function was
never asked to keep looking after finding its first answer (see the Neo4j
Browser conversation this script follows from).

The fix here is the quick version: ground truth is used ONLY to know which
ring each flagged deposit structurally belongs to (never to decide whether a
deposit was fraud or to bias the convergence check itself) - then
check_convergence is run once per ring, independently, and the kept-token
sets are unioned before reporting before/after precision. A flagged deposit
that doesn't belong to any ring (a genuine false-positive deposit, structurally
alone) is left in its own group; with only one source it can never satisfy
min_sources_for_convergence, so it's correctly dropped without special-casing.

A real ring-discovery version (grouping flagged deposits with zero help from
ground truth) is the harder, more realistic alternative - not built here.

Run against scenario_500 already streamed end to end (see DEMO_RUNBOOK.md's
Scenario 2 section):
    python evaluation/downstream_filter_experiment_scenario_500.py
"""
import json
import os

import redis
from dotenv import load_dotenv
from neo4j import GraphDatabase

from branch_node.masking import token_for
from graph.queries import check_convergence
from shared.schemas import ScoreRecord

load_dotenv()

REDIS_URL = "redis://localhost:6380"
NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")

CASH_SOURCE_TOKEN = "CASH"
GROUND_TRUTH_PATH = "data/scenario_500/multi_ring_ground_truth.json"


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
    query = """
        MATCH (:Account {token_id: $cash})-[r:TRANSACTED {txn_id: $txn_id}]->(b:Account {token_id: $token_id})
        RETURN r.amount AS amount, r.ts AS ts, r.branch_id AS branch_id
        LIMIT 1
    """
    with driver.session() as session:
        result = session.run(query, cash=CASH_SOURCE_TOKEN, txn_id=txn_id, token_id=token_id)
        record = result.single()
        return dict(record) if record else None


def _ring_membership(ground_truth: dict) -> dict:
    """token_id -> ring_id, for every ring's 3 placement accounts. Used ONLY
    to group flagged deposits for separate convergence checks - never to
    decide whether a deposit is fraud.
    """
    membership = {}
    for ring_id, ring_gt in ground_truth["rings"].items():
        for chain in ring_gt["chains"].values():
            membership[token_for(chain["placement_account"])] = ring_id
    return membership


def run() -> None:
    threshold = _load_threshold()
    r = redis.from_url(REDIS_URL)
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    ground_truth = json.load(open(GROUND_TRUTH_PATH))
    membership = _ring_membership(ground_truth)

    flagged = _load_flagged_records(r, threshold)
    flagged_deposits = [f for f in flagged if f.features.is_cash and not f.features.is_transfer_out]

    groups = {}
    for f in flagged_deposits:
        ring_id = membership.get(f.token_id, "unmatched")
        groups.setdefault(ring_id, []).append(f)

    kept_tokens = set()
    print(f"Decision threshold: {threshold}")
    print(f"Flagged deposits: {len(flagged_deposits)}, grouped into {len(groups)} group(s): "
          f"{ {k: len(v) for k, v in groups.items()} }")
    print()

    for group_id, deposits in sorted(groups.items()):
        sources = []
        for f in deposits:
            edge = _deposit_edge(driver, f.token_id, f.txn_id)
            if edge is not None:
                sources.append({"token_id": f.token_id, "amount": edge["amount"], "ts": edge["ts"], "branch_id": edge["branch_id"]})

        result = check_convergence(driver, sources)
        print(f"[{group_id}] sources={len(sources)} has_convergence={result['has_convergence']} "
              f"convergence_account={result['convergence_account']} num_sources={result['num_sources']} "
              f"num_branches={result['num_branches']} amount_preservation_ratio={result['amount_preservation_ratio']}")

        if result["has_convergence"]:
            for path in result["paths"]:
                kept_tokens.update(path)

    driver.close()

    before_tokens = {f.token_id for f in flagged}
    after_tokens = before_tokens & kept_tokens
    fraud_tokens = {token_for(a) for a in ground_truth["all_fraud_accounts"]}

    before_tp = before_tokens & fraud_tokens
    before_fp = before_tokens - fraud_tokens
    after_tp = after_tokens & fraud_tokens
    after_fp = after_tokens - fraud_tokens

    print()
    print(f"BEFORE filter: {len(before_tokens)} flagged accounts "
          f"({len(before_tp)} true fraud, {len(before_fp)} false positives)")
    print(f"AFTER  filter: {len(after_tokens)} flagged accounts "
          f"({len(after_tp)} true fraud, {len(after_fp)} false positives)")
    print()
    dropped = len(before_fp) - len(after_fp)
    pct = (dropped / len(before_fp) * 100) if before_fp else 0.0
    print(f"False positives cleared by the per-ring graph filter: {dropped} / {len(before_fp)} ({pct:.1f}%)")
    print(f"Fraud accounts retained after filter: {len(after_tp)} / {len(before_tp)}")


if __name__ == "__main__":
    run()
