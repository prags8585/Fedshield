"""Correctness check for a multi-ring dataset using the REAL production
resolution path (agents/money_trail_agent.py's _find_convergence_group_for_token),
not the simpler per-ring-isolated check in test_multi_ring_convergence.py.

The difference matters: test_multi_ring_convergence.py hands check_convergence
only one ring's own 3 real sources at a time (using ground truth to pre-group
them), which can never reproduce a cross-ring leak - each call has no other
ring's sources to leak from. The live system does the opposite: every
currently-flagged deposit across every ring sits in Redis's flagged_accounts
set at once (see _gather_live_sources), and check_convergence has to sort out
the ambiguity itself. This script reproduces exactly that: it flags every
ring's real placement deposits together, then asks the real agent-level
resolver to trace one token per ring, the same call agents/money_trail_agent.py
makes for a live investigation.

Run with `docker-compose up -d` already running (Neo4j on localhost:7688,
Redis on localhost:6380):
    python scripts/test_agent_convergence.py --data-dir data/scenario_1500
"""
import argparse
import json
import os
import sys

import redis
from dotenv import load_dotenv
from neo4j import GraphDatabase

from agents.money_trail_agent import _find_convergence_group_for_token
from branch_node.graph_writer import GraphWriter
from branch_node.masking import mask_event, token_for
from graph.schema import setup_schema
from shared.redis_keys import FLAGGED_ACCOUNTS

load_dotenv()

NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")
REDIS_URL = "redis://localhost:6380"


def _wipe_graph(driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def run(events_path: str, ground_truth_path: str) -> bool:
    events = json.load(open(events_path))
    ground_truth = json.load(open(ground_truth_path))

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    r = redis.from_url(REDIS_URL)
    setup_schema(driver)
    _wipe_graph(driver)
    r.delete(FLAGGED_ACCOUNTS)

    masked_events = [mask_event(e) for e in events]
    writer = GraphWriter(driver=driver)
    for masked in masked_events:
        writer.write_edge(masked)

    # Flag every ring's real placement deposits at once - this is the part
    # test_multi_ring_convergence.py never does, and exactly the condition
    # that lets one ring's traced path leak into a neighboring ring's group.
    ring_placement_tokens = {}
    for ring_id, ring_gt in ground_truth["rings"].items():
        tokens = [token_for(chain["placement_account"]) for chain in ring_gt["chains"].values()]
        ring_placement_tokens[ring_id] = tokens
        for tok in tokens:
            r.sadd(FLAGGED_ACCOUNTS, tok)

    total_flagged = r.scard(FLAGGED_ACCOUNTS)
    print(f"Loaded {len(events)} fraud events across {ground_truth['num_rings']} rings into a fresh graph.")
    print(f"Flagged {total_flagged} real placement deposits across all rings simultaneously (live-system condition).")
    print(f"Shared accounts linking rings: {len(ground_truth.get('shared_accounts', []))}")
    for link in ground_truth.get("shared_accounts", []):
        print(f"  - {link['account']} (token {token_for(link['account'])}) shared between {link['shared_between']}")
    print()

    consolidation_tokens = {
        ring_id: token_for(ring_gt["consolidation_account"])
        for ring_id, ring_gt in ground_truth["rings"].items()
    }

    all_ok = True
    correct_accounts, total_accounts = 0, 0
    for ring_id, ring_gt in ground_truth["rings"].items():
        expected_account = consolidation_tokens[ring_id]
        own_tokens = ring_placement_tokens[ring_id]
        probe_token = own_tokens[0]

        result = _find_convergence_group_for_token(driver, r, probe_token)
        resolved_members = {tok for path in result.get("paths", []) for tok in path} if result.get("has_convergence") else set()

        found_right_one = result.get("has_convergence") and result.get("convergence_account") == expected_account
        found_wrong_one = (
            result.get("has_convergence")
            and result.get("convergence_account") in consolidation_tokens.values()
            and result.get("convergence_account") != expected_account
        )
        own_sources_present = all(tok in resolved_members for tok in own_tokens)
        ok = found_right_one and not found_wrong_one and own_sources_present
        all_ok = all_ok and ok

        ring_fraud_accounts = set(ring_gt["fraud_accounts"])
        ring_fraud_tokens = {token_for(a) for a in ring_fraud_accounts}
        matched = ring_fraud_tokens & resolved_members if ok else set()
        correct_accounts += len(matched)
        total_accounts += len(ring_fraud_tokens)

        status = "PASS" if ok else "FAIL"
        print(f"[{ring_id}] {status}  (probed via {probe_token[:10]})")
        print(f"    expected_account={expected_account}  got={result.get('convergence_account')}  "
              f"has_convergence={result.get('has_convergence')}  num_sources={result.get('num_sources')}")
        print(f"    own 3 real sources all present in resolved group: {own_sources_present}")
        if found_wrong_one:
            print(f"    !! converged onto a DIFFERENT ring's consolidation account - leak reproduced")
        print(f"    accounts matched in this ring: {len(matched)}/{len(ring_fraud_tokens)}")

    driver.close()
    print(f"\nOverall: {correct_accounts}/{total_accounts} fraud accounts correctly resolved to their own ring.")
    return all_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent-level (real production path) convergence correctness check")
    parser.add_argument("--data-dir", type=str, default="data/scenario_500")
    args = parser.parse_args()

    ok = run(
        os.path.join(args.data_dir, "multi_ring_events.json"),
        os.path.join(args.data_dir, "multi_ring_ground_truth.json"),
    )
    if not ok:
        sys.exit(1)
    print("\nAll rings correctly resolve to their own consolidation account via the real agent-level resolver.")
