"""Correctness check for the scenario_500 multi-ring dataset: confirms
check_convergence resolves each ring to ITS OWN consolidation account
independently, even though two pairs of rings deliberately share a mule
account with each other (ring1<->ring2, ring2<->ring3 - see
simulator/multi_ring_scenario.py). The existing amount+time chaining rule is
what's actually being tested here: it must follow only the edges whose
amount/timing match the ring currently being traced, and ignore whichever
OTHER ring's unrelated edge happens to sit on that same shared account.

Run with `docker-compose up -d` already running (Neo4j on localhost:7688),
against data/scenario_500/ (already generated):
    python scripts/test_multi_ring_convergence.py
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv
from neo4j import GraphDatabase

from branch_node.graph_writer import GraphWriter
from branch_node.masking import mask_event, token_for
from graph.queries import check_convergence
from graph.schema import setup_schema

load_dotenv()

NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")


def _wipe_graph(driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def _sources_for_ring(masked_events: list, ring_gt: dict) -> list:
    sources = []
    for chain in ring_gt["chains"].values():
        placement = next(
            m for m in masked_events
            if m["txn_type"] == "CASH_DEPOSIT" and m["local_account_number"] == chain["placement_account"]
        )
        sources.append({
            "token_id": placement["token_id"], "amount": placement["amount"],
            "ts": placement["timestamp"], "branch_id": placement["branch_id"],
        })
    return sources


def run(events_path: str, ground_truth_path: str) -> bool:
    events = json.load(open(events_path))
    ground_truth = json.load(open(ground_truth_path))

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    setup_schema(driver)
    _wipe_graph(driver)

    masked_events = [mask_event(e) for e in events]
    writer = GraphWriter(driver=driver)
    for masked in masked_events:
        writer.write_edge(masked)

    print(f"Loaded {len(events)} fraud events across {ground_truth['num_rings']} rings into a fresh graph.")
    print(f"Shared accounts linking rings: {len(ground_truth['shared_accounts'])}")
    for link in ground_truth["shared_accounts"]:
        print(f"  - {link['account']} (token {token_for(link['account'])}) shared between {link['shared_between']}")
    print()

    all_ok = True
    consolidation_tokens = {
        ring_id: token_for(ring_gt["consolidation_account"])
        for ring_id, ring_gt in ground_truth["rings"].items()
    }

    for ring_id, ring_gt in ground_truth["rings"].items():
        sources = _sources_for_ring(masked_events, ring_gt)
        expected_account = consolidation_tokens[ring_id]
        result = check_convergence(driver, sources)

        found_right_one = result["has_convergence"] and result["convergence_account"] == expected_account
        found_wrong_one = (
            result["has_convergence"]
            and result["convergence_account"] in consolidation_tokens.values()
            and result["convergence_account"] != expected_account
        )
        ok = found_right_one and not found_wrong_one
        all_ok = all_ok and ok

        status = "PASS" if ok else "FAIL"
        print(f"[{ring_id}] {status}")
        print(f"    expected_account={expected_account}")
        print(f"    got convergence_account={result['convergence_account']} has_convergence={result['has_convergence']} "
              f"num_sources={result['num_sources']} num_branches={result['num_branches']}")
        print(f"    amount_preservation_ratio={result['amount_preservation_ratio']} "
              f"cycle_detected={result['cycle_detected']}")
        if found_wrong_one:
            print(f"    !! converged onto a DIFFERENT ring's consolidation account - rings got confused")

    driver.close()
    return all_ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Correctness check for a multi-ring dataset's convergence resolution")
    parser.add_argument("--data-dir", type=str, default="data/scenario_500", help="directory containing multi_ring_events.json / multi_ring_ground_truth.json")
    args = parser.parse_args()

    ok = run(
        os.path.join(args.data_dir, "multi_ring_events.json"),
        os.path.join(args.data_dir, "multi_ring_ground_truth.json"),
    )
    if not ok:
        sys.exit(1)
    print("\nAll rings correctly resolve to their own consolidation account, despite the shared mule accounts.")
