"""Session 4 Pass 1 exit-criteria check: confirms check_convergence finds the
consolidation account for the Session 2 layering scenario, independent of
configured hop count, independent of any agent.

Run with `docker-compose up -d` already running (Neo4j on localhost:7688),
against the scenario files already generated in data/:
    python scripts/test_convergence.py

Each hop-count scenario is loaded into a freshly wiped graph, not
accumulated across sub-tests. layering_scenario.py's account-number minter
restarts from the same fixed range on every invocation, so the hops=2,
hops=4, and hops=6 fixture files mint the *same* account numbers (and
therefore the same token_ids, since the token salt is fixed) - loading them
into one shared, un-wiped graph would silently merge unrelated scenarios
into a single (wrong) money trail.
"""
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

# Host-facing address - matches scripts/test_connectivity.py's convention
# (the .env's bolt://neo4j:7687 only resolves inside the compose network).
NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")


def _wipe_graph(driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def _load_scenario(hops: int) -> tuple:
    with open(f"data/layering_hops{hops}_events.json") as f:
        events = json.load(f)
    with open(f"data/layering_hops{hops}_ground_truth.json") as f:
        ground_truth = json.load(f)
    return events, ground_truth


def _sources_from_ground_truth(masked_events: list, ground_truth: dict) -> list:
    sources = []
    for chain in ground_truth["chains"].values():
        placement = next(
            m for m in masked_events
            if m["txn_type"] == "CASH_DEPOSIT" and m["local_account_number"] == chain["placement_account"]
        )
        sources.append({
            "token_id": placement["token_id"],
            "amount": placement["amount"],
            "ts": placement["timestamp"],
            "branch_id": placement["branch_id"],
        })
    return sources


def run_hop_test(driver, hops: int) -> bool:
    _wipe_graph(driver)
    events, ground_truth = _load_scenario(hops)

    masked_events = [mask_event(e) for e in events]
    writer = GraphWriter(driver=driver)
    for masked in masked_events:
        writer.write_edge(masked)

    sources = _sources_from_ground_truth(masked_events, ground_truth)
    expected_account = token_for(ground_truth["consolidation_account"])

    result = check_convergence(driver, sources)

    ok = (
        result["has_convergence"]
        and result["convergence_account"] == expected_account
        and result["num_sources"] == len(sources)
    )

    status = "PASS" if ok else "FAIL"
    print(f"[hops={hops}] {status}")
    print(f"    expected_account={expected_account}")
    print(f"    got convergence_account={result['convergence_account']} "
          f"has_convergence={result['has_convergence']} num_sources={result['num_sources']} "
          f"num_branches={result['num_branches']}")
    print(f"    amount_preservation_ratio={result['amount_preservation_ratio']} "
          f"time_to_convergence_minutes={result['time_to_convergence_minutes']} "
          f"depth(shortest/longest)={result['shortest_depth']}/{result['longest_depth']} "
          f"cycle_detected={result['cycle_detected']}")
    return ok


if __name__ == "__main__":
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    setup_schema(driver)

    results = {hops: run_hop_test(driver, hops) for hops in (2, 4, 6)}
    driver.close()

    failed = [hops for hops, ok in results.items() if not ok]
    if failed:
        print(f"\nFAILED for hop counts: {failed}")
        sys.exit(1)

    print("\nAll hop counts (2, 4, 6) correctly converge. Session 4 Pass 1 exit criteria met.")
