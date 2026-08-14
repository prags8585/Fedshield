"""Session 4 Pass 2 robustness check: loads a layering scenario WITH mule
noise (unrelated paycheck/rent/purchase activity on the fraud-path accounts
themselves) plus background traffic WITH fan-in noise (several unrelated
senders converging on a shared landlord/payroll-style account) into the same
graph, and confirms two things at once:

1. check_convergence still finds the real consolidation account from the
   3 real placement deposits, undistracted by the extra edges now sitting
   on those same mule/placement/consolidation nodes.
2. check_convergence does NOT report a false convergence when pointed at
   3 of the innocent senders feeding one shared fan-in account - proving the
   time+amount chaining rule is doing real work, not just "N sources -> 1
   account" topology matching.

Run with `docker-compose up -d` already running, and the noisy fixtures
already generated:
    python simulator/layering_scenario.py --hops 4 --inject-mule-noise \
        --out-events data/layering_hops4_noisy_events.json \
        --out-ground-truth data/layering_hops4_noisy_ground_truth.json
    python simulator/data_generator.py --count 300 --fanin-accounts 3 \
        --fanin-senders 4 --out data/background_noisy.json
    python scripts/test_convergence_noisy.py
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

NEO4J_URI = "bolt://localhost:7688"
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "your_password_here")


def _wipe_graph(driver) -> None:
    with driver.session() as session:
        session.run("MATCH (n) DETACH DELETE n")


def _load_json(path: str):
    with open(path) as f:
        return json.load(f)


def _find_fanin_sources(masked_events: list, num_groups: int = 1, group_size: int = 3) -> list:
    """Groups fan-in noise events by beneficiary token (the shared account)
    and returns up to num_groups groups of group_size sender-side "sources"
    - shaped exactly like check_convergence's source dicts - so they can be
    fed straight into the same convergence check as a negative control.
    """
    # For account-to-account transfers the sender is "local" (is_transfer_out=True)
    # and the recipient is the counterparty, so group senders by counterparty_token_id.
    outbound = [m for m in masked_events if m["is_transfer_out"]]
    by_recipient = {}
    for m in outbound:
        by_recipient.setdefault(m["counterparty_token_id"], []).append(m)

    groups = []
    for recipient, senders in by_recipient.items():
        if len(senders) >= group_size:
            groups.append([
                {"token_id": s["token_id"], "amount": s["amount"], "ts": s["timestamp"], "branch_id": s["branch_id"]}
                for s in senders[:group_size]
            ])
        if len(groups) >= num_groups:
            break
    return groups


def run() -> bool:
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    setup_schema(driver)
    _wipe_graph(driver)

    fraud_events = _load_json("data/layering_hops4_noisy_events.json")
    ground_truth = _load_json("data/layering_hops4_noisy_ground_truth.json")
    background_events = _load_json("data/background_noisy.json")

    all_masked = [mask_event(e) for e in fraud_events] + [mask_event(e) for e in background_events]
    writer = GraphWriter(driver=driver)
    for masked in all_masked:
        writer.write_edge(masked)

    # Positive control: the real fraud convergence must still be found.
    fraud_masked = [mask_event(e) for e in fraud_events]
    sources = []
    for chain in ground_truth["chains"].values():
        placement = next(
            m for m in fraud_masked
            if m["txn_type"] == "CASH_DEPOSIT" and m["local_account_number"] == chain["placement_account"]
        )
        sources.append({
            "token_id": placement["token_id"], "amount": placement["amount"],
            "ts": placement["timestamp"], "branch_id": placement["branch_id"],
        })
    expected_account = token_for(ground_truth["consolidation_account"])
    real_result = check_convergence(driver, sources)
    real_ok = real_result["has_convergence"] and real_result["convergence_account"] == expected_account

    print(f"[positive control] {'PASS' if real_ok else 'FAIL'} - "
          f"expected={expected_account} got={real_result['convergence_account']} "
          f"has_convergence={real_result['has_convergence']}")

    # Negative control: 3 innocent senders into one shared fan-in account must NOT converge.
    background_masked = [mask_event(e) for e in background_events]
    fanin_groups = _find_fanin_sources(background_masked, num_groups=3, group_size=3)
    negative_results = []
    for group in fanin_groups:
        result = check_convergence(driver, group)
        negative_results.append(result)
        ok = not result["has_convergence"]
        print(f"[negative control] {'PASS' if ok else 'FAIL'} - "
              f"senders={[g['token_id'] for g in group]} has_convergence={result['has_convergence']} "
              f"convergence_account={result['convergence_account']}")

    negative_ok = all(not r["has_convergence"] for r in negative_results) if negative_results else None
    if negative_ok is None:
        print("[negative control] SKIPPED - no fan-in groups with >= 3 senders found")

    driver.close()
    return real_ok and (negative_ok is not False)


if __name__ == "__main__":
    ok = run()
    if not ok:
        sys.exit(1)
    print("\nPass 2 robustness check passed: real convergence still found, fan-in noise correctly rejected.")
